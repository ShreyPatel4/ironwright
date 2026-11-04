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
- ``cdc`` (rung 5): postgres logical decoding through the built-in
  test_decoding plugin, polled over plain SQL (``ironwright/cdc.py``).
  PROVEN against a real postgres named by POSTGRES_URI; the tests skip
  honestly without one. The connection uri comes from the environment
  variable the spec names, never from the spec itself. Debezium, MySQL,
  Oracle, and streaming protocol consumption are NOT claimed.
- ``local_folder`` (L1, a user's own files): PROVEN against real symlinks, a
  real FIFO and a real device node in tests/test_paths.py. Files are opened
  read only through ``read_source_bytes``, copied into a private temp dir and
  read through the same local-file path as the s3 rung. The user's folder is
  never written to.

Credentials are read from the environment by the underlying drivers (boto3,
ADBC); nothing here stores them. Runtime deps live in the ``connectors``
optional extra; moto is dev-only.
"""
from __future__ import annotations

import errno
import fnmatch
import glob as _glob
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa

from .config import FILE_NAME, private_dir, safe_child, setting, write_private

_MAX_FILE_BYTES = 1024 ** 3
_MAX_FILES = 10_000


class SourceRefused(ValueError):
    """A source path a load must not touch. Loud on purpose: a dropped file
    would look like a clean load with fewer rows."""


# ------------------------------------------- the guard on a user's own files

def _inside(root: Path, rel: str | Path) -> tuple[Path, Path]:
    """(resolved root, resolved target). Unlike safe_child, a source path may
    name a subfolder and may be a symlink, as long as it lands inside root."""
    base = Path(root).resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise SourceRefused(f"{rel} resolves outside the source root")
    return base, target


def read_source_bytes(root: str | Path, rel: str | Path, max_bytes: int | None = None) -> bytes:
    """One source file, read only. O_NONBLOCK so a FIFO cannot hang the open;
    fstat on the open descriptor, so what gets checked is what gets read."""
    cap = int(setting("L1_MAX_FILE_BYTES", _MAX_FILE_BYTES)) if max_bytes is None else max_bytes
    _, target = _inside(Path(root), rel)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise SourceRefused(f"{rel} changed to a symlink during the load") from e
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SourceRefused(f"{rel} is not a regular file")
        if info.st_size > cap:
            raise SourceRefused(f"{rel} is {info.st_size} bytes, over the {cap} byte cap")
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read(cap + 1)  # it may have grown since the fstat
    finally:
        os.close(fd)
    if len(data) > cap:
        raise SourceRefused(f"{rel} grew past the {cap} byte cap during the load")
    return data


def source_files(root: str | Path, pattern: str, max_files: int | None = None) -> list[str]:
    """Sorted relative names a glob selects under root, every one of them
    checked. Refuses the whole load on one bad match, and never opens a file."""
    cap = int(setting("L1_MAX_FILES", _MAX_FILES)) if max_files is None else max_files
    base = Path(root).resolve()
    names = []
    for rel in sorted(_glob.glob(pattern, root_dir=base)):
        _, target = _inside(base, rel)
        if not stat.S_ISREG(target.stat().st_mode):
            raise SourceRefused(f"{rel} is not a regular file")
        names.append(rel)
        if len(names) > cap:
            raise SourceRefused(f"more than {cap} files match {pattern}")
    return names


def copy_source_files(root: str | Path, dest: Path, pattern: str) -> list[str]:
    """Copy the selected files flat into dest: dest 0700, copies 0600 whatever
    the source mode was. Returns the copied basenames."""
    private_dir(dest)
    names: list[str] = []
    for rel in source_files(root, pattern):
        name = Path(rel).name
        if name in names:
            raise SourceRefused(f"{pattern}: basenames collide on {name}")
        write_private(safe_child(dest, name, pattern=FILE_NAME), read_source_bytes(root, rel))
        names.append(name)
    return names


def _inner_spec(src: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """The local-file spec for files already sitting flat in a temp dir."""
    inner = dict(src["inner"])
    ikind = inner["kind"]
    if ikind == "fixed_width":
        if len(names) != 1:
            raise ValueError(f"{src['kind']} fixed_width inner kind takes exactly one file")
        inner["file"] = names[0]
    elif ikind in ("csv", "jsonl"):
        inner["glob"] = "*"
    else:
        raise ValueError(f"{src['kind']} inner kind {ikind} not supported")
    return inner


def read_connector(src: dict[str, Any], legacy_dir: Path) -> pa.Table:
    """Dispatch for source kinds beyond the four local legacy kinds."""
    from .legacy import rows_to_table

    kind = src["kind"]
    if kind == "local_folder":
        return _read_local_folder(src, legacy_dir)
    if kind == "s3":
        return _read_s3(src, legacy_dir)
    if kind == "adbc":
        return rows_to_table(_read_adbc(src, legacy_dir))
    if kind == "idoc":
        return rows_to_table(_read_idoc(src, legacy_dir))
    if kind == "stream_window":
        return rows_to_table(_read_stream_window(src, legacy_dir))
    if kind == "cdc":
        return rows_to_table(_read_cdc(src))
    raise ValueError(f"unknown source kind {kind}")


# ---------------------------------------------------------- L1: local folder

def _read_local_folder(src: dict[str, Any], legacy_dir: Path) -> pa.Table:
    """A user's own folder. Spec: {kind: local_folder, root?, inner: {kind:
    fixed_width|csv|jsonl, file|glob, ...its params}}. ``root`` defaults to the
    legacy dir. sqlite is not an inner kind: its reader opens read-write.
    """
    from .legacy import read_source

    inner = src["inner"]
    if inner["kind"] not in ("fixed_width", "csv", "jsonl"):
        raise ValueError(f"local_folder inner kind {inner['kind']} not supported")
    pattern = inner["file"] if inner["kind"] == "fixed_width" else inner["glob"]
    with tempfile.TemporaryDirectory(prefix="ironwright-l1-") as td:  # mkdtemp: 0700
        tdir = Path(td)
        names = copy_source_files(src.get("root", legacy_dir), tdir, pattern)
        return read_source(_inner_spec(src, names), tdir)


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
            # A key is remote input: "drop/.." has the basename "..".
            s3.download_file(bucket, key, str(safe_child(tdir, name, pattern=FILE_NAME)))
        return read_source(_inner_spec(src, names), tdir)


# -------------------------------------------------------------- rung 2: adbc

def _read_adbc(src: dict[str, Any], legacy_dir: Path) -> list[dict[str, Any]]:
    """Warehouse read over ADBC. Spec: {kind: adbc, driver: sqlite|postgresql,
    uri, query}. A relative sqlite uri resolves against the legacy dir, same
    as the native sqlite kind's ``db``, stays inside it, and opens read only.
    """
    driver, uri = src["driver"], src["uri"]
    if driver == "sqlite":
        from adbc_driver_sqlite import dbapi
        if "://" not in uri and not uri.startswith("file:") and not Path(uri).is_absolute():
            _, target = _inside(legacy_dir, uri)
            uri = f"{target.as_uri()}?mode=ro"
    elif driver == "postgresql":
        from adbc_driver_postgresql import dbapi
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
    for line in read_source_bytes(legacy_dir, src["file"]).decode("latin-1").splitlines():
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

# ISO 8601 time-part durations only, PT required when units appear: in ISO,
# "P5M" is five MONTHS and "PT5M" five minutes, so the M is accepted only
# after the T. Calendar durations have no place in a stream window.
_DURATION = re.compile(
    r"^PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?$"
)


def _window_seconds(window: Any) -> float:
    if isinstance(window, (int, float)):
        return float(window)
    text = str(window).strip().upper()
    m = _DURATION.match(text)
    if m and any(m.groups()):
        return (int(m.group("h") or 0) * 3600
                + int(m.group("m") or 0) * 60
                + float(m.group("s") or 0))
    try:
        return float(text)  # plain number of seconds as a string
    except ValueError:
        raise ValueError(
            f"window {window!r} is not a number of seconds or an ISO time "
            "duration like PT5M or PT1H30M (calendar units like P5M, five "
            "months, are rejected on purpose)")


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
    seconds = _window_seconds(src["window"])
    if seconds <= 0:
        raise ValueError(f"stream_window needs a positive window, got {src['window']!r}")
    field = src.get("window_id_from", "ts")
    rows: list[dict[str, Any]] = []
    for f in source_files(legacy_dir, src["glob"]):
        for line in read_source_bytes(legacy_dir, f).decode().splitlines():
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


# --------------------------------------------------------------- rung 5: cdc

def _read_cdc(src: dict[str, Any]) -> list[dict[str, Any]]:
    """Change capture from postgres logical decoding. Spec: {kind: cdc,
    uri_env: NAME, slot, table?}.

    The connection uri comes from the environment variable the spec names,
    never from the spec itself, so no credentials live in pack data. One
    capture consumes the slot's pending changes (see ``ironwright/cdc.py``).
    ``table`` filters to one table: either the schema-qualified name as
    test_decoding prints it, or the bare table name.
    """
    import os

    from . import cdc

    env = src["uri_env"]
    uri = os.environ.get(env)
    if not uri:
        raise ValueError(
            f"cdc source needs the {env} environment variable set; "
            "the connection uri never lives in the spec")
    rows = cdc.capture(uri, src["slot"]).to_pylist()
    want = src.get("table")
    if want:
        rows = [r for r in rows
                if r["_table"] == want or r["_table"].rsplit(".", 1)[-1] == want]
    return rows
