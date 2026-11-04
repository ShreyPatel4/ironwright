"""Read legacy systems the way a migration team would: copybook fixed-width files,
old operational databases, CSV drops and JSON event logs. Everything comes out
as Arrow tables of strings plus a few typed columns, exactly as received."""
from __future__ import annotations

import csv
import glob
import io
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import pyarrow as pa


def read_fixed_width(path: Path, layout: list[list[Any]]) -> list[dict[str, Any]]:
    """layout: [[field, width], ...] like a COBOL copybook PIC clause list."""
    rows = []
    for line in path.read_text(encoding="latin-1").splitlines():
        if not line.strip():
            continue
        rec, pos = {}, 0
        for name, width in layout:
            rec[name] = line[pos:pos + int(width)].strip()
            pos += int(width)
        rows.append(rec)
    return rows


def rows_to_table(rows: list[dict[str, Any]]) -> pa.Table:
    """The one road into bronze: everything lands as strings, in first-seen
    column order. Every source kind, local or connector, converges here."""
    if not rows:
        return pa.table({})
    cols = list(dict.fromkeys(k for r in rows for k in r))
    # land everything as strings in bronze: never trust legacy types on the way in
    return pa.table({c: pa.array([None if r.get(c) in (None, "") else str(r.get(c)) for r in rows], pa.string())
                     for c in cols})


# ---------- tenant uploads ----------
# Untrusted bytes. Errors here name the source and the column, never a server
# path or a cell, because they travel back to whoever uploaded the file.

UPLOAD_ENCODINGS = frozenset({"utf-8", "utf-8-sig", "utf-16", "latin-1", "iso-8859-1", "cp1252", "ascii"})


class IngestError(ValueError):
    """An upload does not match its source spec. The message is safe to show."""


def _column_name(raw: str, position: int) -> str:
    """A header as a plain SQL name. Leading underscores go, so a header can
    never spell an engine column such as _run_id."""
    name = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    if not name:
        return f"col_{position + 1}"
    return f"c_{name}" if name[0].isdigit() else name


def _upload_text(src: dict[str, Any]) -> str:
    try:
        fd = os.open(src["upload_path"], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        raise IngestError(f"source {src['name']}: the upload cannot be read") from e
    encoding = src["layout"].get("encoding", "utf-8")
    try:  # utf-8 exports often open with a BOM that would land in the first header
        return raw.decode("utf-8-sig" if encoding == "utf-8" else encoding)
    except UnicodeError as e:
        raise IngestError(f"source {src['name']}: the file is not valid {encoding}") from e


def _read_upload_csv(src: dict[str, Any], text: str) -> list[dict[str, Any]]:
    layout = src["layout"]
    delimiter = layout.get("delimiter", ",")
    try:
        table = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
                 if any(c.strip() for c in r)]
    except csv.Error as e:
        raise IngestError(f"source {src['name']}: the file is not readable as csv") from e
    if layout.get("header", True):
        names = [_column_name(c, i) for i, c in enumerate(table[0])] if table else []
        table = table[1:]
    else:
        names = [c["name"] for c in src["columns"]]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise IngestError(f"source {src['name']}: more than one column reads as {', '.join(dupes)}")
    rows = []
    for cells in table:
        rec: dict[str, Any] = dict(zip(names, cells))
        if len(cells) > len(names):  # never drop what the file said
            rec["_extra"] = delimiter.join(cells[len(names):])
        rows.append(rec)
    return rows


def _read_upload_fixed_width(src: dict[str, Any], text: str) -> list[dict[str, Any]]:
    # Split on line ends only: str.splitlines also breaks on bytes like 0x85.
    rows = []
    for line in text.split("\n"):
        if line.strip():
            rows.append({f["name"]: line.rstrip("\r")[f["start"]:f["start"] + f["width"]].strip()
                         for f in src["layout"]["fields"]})
    return rows


def read_upload(src: dict[str, Any]) -> pa.Table:
    """One tenant upload as bronze strings, through the same rows_to_table road."""
    text = _upload_text(src)
    reader = _read_upload_fixed_width if src["format"] == "fixed_width" else _read_upload_csv
    rows = reader(src, text)
    if not rows:
        raise IngestError(f"source {src['name']}: the file has no data rows")
    seen = {k for r in rows for k in r}
    missing = [c["name"] for c in src["columns"] if c["name"] not in seen]
    if missing:
        raise IngestError(f"source {src['name']}: the file has no column {', '.join(missing)}")
    file_name = Path(src["upload_path"]).name
    for r in rows:
        r["_source_file"] = file_name
    return rows_to_table(rows)


def read_source(src: dict[str, Any], legacy_dir: Path) -> pa.Table:
    kind = src["kind"]
    if kind == "upload":
        return read_upload(src)
    if kind == "fixed_width":
        rows = read_fixed_width(legacy_dir / src["file"], src["layout"])
    elif kind == "csv":
        rows = []
        for f in sorted(glob.glob(str(legacy_dir / src["glob"]))):
            with open(f, newline="") as fh:
                for r in csv.DictReader(fh):
                    r["_source_file"] = Path(f).name
                    rows.append(r)
    elif kind == "sqlite":
        con = sqlite3.connect(legacy_dir / src["db"])
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(src["query"]).fetchall()]
        con.close()
    elif kind == "jsonl":
        rows = []
        for f in sorted(glob.glob(str(legacy_dir / src["glob"]))):
            rows += [json.loads(line) for line in Path(f).read_text().splitlines() if line.strip()]
    else:
        # connector-ladder kinds (s3, adbc, idoc, stream_window, cdc) live in
        # ironwright.connectors; it raises on a kind nobody implements.
        from .connectors import read_connector
        return read_connector(src, legacy_dir)
    return rows_to_table(rows)
