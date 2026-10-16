"""Connector ladder rungs: remote and structured sources landing through the
same bronze contract as local files. Everything returns the same Arrow shape
``legacy.read_source`` produces: strings in bronze, never trust source types.

Every rung here is fixture-proven offline or it is not claimed. The honest
state of each rung, as of this file:

- ``s3`` (rung 1, object-store drops): PROVEN against an in-process moto S3
  fixture. Objects are downloaded to a temp dir and read through the exact
  same local-file path in ``legacy.read_source``, so bronze is byte-identical
  to a local-file run by construction, and a contract test asserts it anyway.
- ``adbc`` (rung 2, warehouses): the ``sqlite`` driver is PROVEN by an
  offline fixture (byte-identical to the native ``sqlite`` kind on the same
  db and query). The ``postgresql`` driver is code-present but NOT
  fixture-proven here; its test skips unless POSTGRES_URI points at a real
  server. Until it is fixtured, postgresql must not be listed in Sources.
  Row order is whatever the query returns: use ORDER BY if you need
  deterministic bronze.
- ``idoc`` (rung 3, SAP): flat-file IDocs ONLY. Fixed-width records, the
  first ``control_width`` chars carry the segment name, control record
  (EDI_DC40) first. One table out, rows tagged with ``_segment``; unknown
  segments are never dropped, they land with ``_segment`` set and the raw
  payload preserved in ``_raw``. OData extractors and RFC/BAPI are later
  rungs and are NOT claimed.
- ``stream_window`` (rung 4, streams): recorded jsonl segments ONLY, windowed
  into bronze with a deterministic ``_window_id`` (window start, UTC ISO).
  Events exactly on a window boundary belong to the later window. A live
  Kafka-protocol consumer is a later rung and is NOT claimed.

Credentials are read from the environment by the underlying drivers (boto3,
ADBC); nothing here stores them. Runtime deps live in the ``connectors``
optional extra; moto is dev-only.
"""
from __future__ import annotations

import fnmatch
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa


def read_connector(src: dict[str, Any], legacy_dir: Path) -> pa.Table:
    """Dispatch for source kinds beyond the four local legacy kinds."""
    from .legacy import rows_to_table

    kind = src["kind"]
    if kind == "s3":
        return _read_s3(src, legacy_dir)
    if kind == "adbc":
        return rows_to_table(_read_adbc(src, legacy_dir))
    if kind == "idoc":
        return rows_to_table(_read_idoc(src, legacy_dir))
    if kind == "stream_window":
        return rows_to_table(_read_stream_window(src, legacy_dir))
    raise ValueError(f"unknown source kind {kind}")


# ---------------------------------------------------------------- rung 1: s3

def _read_s3(src: dict[str, Any], legacy_dir: Path) -> pa.Table:
    """Object-store drop. Spec: {kind: s3, bucket, key | prefix+glob,
    inner: {kind: fixed_width|csv|jsonl, ...its params}, endpoint_url?, anon?}.

    Objects are fetched into a temp dir (basenames preserved) and read through
    ``legacy.read_source`` on that dir, so the bronze bytes are the local-file
    path's bytes. Keys are selected in sorted order, matching local glob order.
    """
    import boto3

    client_kwargs: dict[str, Any] = {}
    if src.get("endpoint_url"):
        client_kwargs["endpoint_url"] = src["endpoint_url"]
    if src.get("anon"):
        from botocore import UNSIGNED
        from botocore.config import Config
        client_kwargs["config"] = Config(signature_version=UNSIGNED)
    s3 = boto3.client("s3", **client_kwargs)

    bucket = src["bucket"]
    if "key" in src:
        keys = [src["key"]]
    else:
        prefix, pattern = src.get("prefix", ""), src.get("glob", "*")
        keys, token = [], None
        while True:
            page = s3.list_objects_v2(Bucket=bucket, Prefix=prefix,
                                      **({"ContinuationToken": token} if token else {}))
            keys += [o["Key"] for o in page.get("Contents", [])
                     if fnmatch.fnmatch(o["Key"], pattern) or fnmatch.fnmatch(Path(o["Key"]).name, pattern)]
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
        keys.sort()
    if not keys:
        return pa.table({})

    from .legacy import read_source

    with tempfile.TemporaryDirectory(prefix="ironwright-s3-") as td:
        tdir = Path(td)
        names = []
        for key in keys:
            name = Path(key).name
            if name in names:
                raise ValueError(f"s3 source {bucket}: key basenames collide on {name}")
            names.append(name)
            s3.download_file(bucket, key, str(tdir / name))
        inner = dict(src["inner"])
        ikind = inner["kind"]
        if ikind == "fixed_width":
            if len(names) != 1:
                raise ValueError("s3 fixed_width inner kind takes exactly one key")
            inner["file"] = names[0]
        elif ikind in ("csv", "jsonl"):
            inner["glob"] = "*"
        else:
            raise ValueError(f"s3 inner kind {ikind} not supported")
        return read_source(inner, tdir)


# -------------------------------------------------------------- rung 2: adbc

def _read_adbc(src: dict[str, Any], legacy_dir: Path) -> list[dict[str, Any]]:
    """Warehouse read over ADBC. Spec: {kind: adbc, driver: sqlite|postgresql,
    uri, query}. A relative sqlite uri resolves against the legacy dir, same
    as the native sqlite kind's ``db``.
    """
    driver, uri = src["driver"], src["uri"]
    if driver == "sqlite":
        import adbc_driver_sqlite.dbapi as dbapi
        if "://" not in uri and not Path(uri).is_absolute():
            uri = str(legacy_dir / uri)
    elif driver == "postgresql":
        import adbc_driver_postgresql.dbapi as dbapi
    else:
        raise ValueError(f"unknown adbc driver {driver}")
    with dbapi.connect(uri) as con, con.cursor() as cur:
        cur.execute(src["query"])
        return cur.fetch_arrow_table().to_pylist()


# -------------------------------------------------------------- rung 3: idoc

def _read_idoc(src: dict[str, Any], legacy_dir: Path) -> list[dict[str, Any]]:
    """SAP IDoc flat file. Spec: {kind: idoc, file, segments:
    {SEGNAME: [[field, width], ...]}, control_width?}.

    Every record is fixed width; the first ``control_width`` chars (default
    30, the real EDI_DC40 tabnam width) carry the segment name. Known
    segments parse by their layout; a truncated last record parses what is
    there and leaves the rest null. Unknown segments are never dropped: they
    land with ``_segment`` set and the raw payload in ``_raw``.
    """
    name_width = int(src.get("control_width", 30))
    segments: dict[str, list[list[Any]]] = src["segments"]
    rows: list[dict[str, Any]] = []
    for line in (legacy_dir / src["file"]).read_text(encoding="latin-1").splitlines():
        if not line.strip():
            continue
        seg = line[:name_width].strip()
        payload = line[name_width:]
        if seg in segments:
            rec: dict[str, Any] = {"_segment": seg}
            pos = 0
            for name, width in segments[seg]:
                rec[name] = payload[pos:pos + int(width)].strip()
                pos += int(width)
            rows.append(rec)
        else:
            rows.append({"_segment": seg, "_raw": payload})
    return rows


# ----------------------------------------------------- rung 4: stream_window

_DURATION = re.compile(
    r"^P?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?$"
)


def _window_seconds(window: Any) -> float:
    if isinstance(window, (int, float)):
        return float(window)
    m = _DURATION.match(str(window).strip().upper())
    if m and any(m.groups()):
        return (int(m.group("h") or 0) * 3600
                + int(m.group("m") or 0) * 60
                + float(m.group("s") or 0))
    return float(window)  # plain number of seconds as a string


def _epoch(value: Any, field: str) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        ts = datetime.fromisoformat(text)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    raise ValueError(f"stream_window event has no usable {field}: {value!r}")


def _read_stream_window(src: dict[str, Any], legacy_dir: Path) -> list[dict[str, Any]]:
    """Recorded stream segments, windowed. Spec: {kind: stream_window, glob,
    window: seconds | ISO duration (PT5M), window_id_from?: ts}.

    Reads jsonl event files in sorted order, assigns each event a
    ``_window_id``: the UTC ISO start of its window. Windows are half open
    [start, start + window); an event exactly on a boundary starts the later
    window. Deterministic, so a re-run on the same segments is identical.
    An event missing the timestamp field is an error, never dropped.
    """
    import glob as _glob

    seconds = _window_seconds(src["window"])
    if seconds <= 0:
        raise ValueError(f"stream_window needs a positive window, got {src['window']!r}")
    field = src.get("window_id_from", "ts")
    rows: list[dict[str, Any]] = []
    for f in sorted(_glob.glob(str(legacy_dir / src["glob"]))):
        for line in Path(f).read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if field not in event:
                raise ValueError(f"stream_window event in {Path(f).name} missing {field}: {line[:80]}")
            start = (int(_epoch(event[field], field) // seconds)) * seconds
            event["_window_id"] = datetime.fromtimestamp(start, tz=timezone.utc) \
                .isoformat().replace("+00:00", "Z")
            rows.append(event)
    return rows
