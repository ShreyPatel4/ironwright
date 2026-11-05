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
from collections import Counter
from collections.abc import Callable, Iterable
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


def empty_trailing_columns(header: list[str], rows: Iterable[list[str]]) -> int:
    """How many columns at the end of a file have no name and no value in any row. A
    header that ends in a delimiter makes one. The profiler and the reader both ask
    this, so a draft never declares a column bronze does not have."""
    rows = list(rows)
    count = 0
    for i in range(len(header) - 1, -1, -1):
        if header[i].strip() or any(i < len(r) and r[i].strip() for r in rows):
            break
        count += 1
    return count


# ---------------------------------------------------------------- records that are not data
# One rule for the profiler that proposes a skip and the reader that honours it.

MAX_TRAILER = 3
TRAILER_CELL = re.compile(r"\s*([*=#-]{2,}|total\b|grand total\b|end of\b|eof\b|trailer\b|trl\b|record count\b)", re.IGNORECASE)
RECORD_TRAILER = re.compile(r"(TRL|TRAILER|EOF|END)([^A-Za-z]|$)")
RECORD_HEADER = re.compile(r"(HDR|HEADER)([^A-Za-z]|$)")
_NUMBER = re.compile(r"[+-]?\d*\.?\d+")
_COUNT = r"(\d[\d,]*)(?![\d,./-])"  # the lookahead keeps a date or an amount from reading as a count
_DECLARED = (re.compile(rf"(?:records?|rows?|count)\W{{0,3}}{_COUNT}", re.IGNORECASE),
             re.compile(rf"{_COUNT}\s*(?:records?|rows?)\b", re.IGNORECASE),
             re.compile(rf"\s*(?:TRL|TRAILER|EOF|END)\W{{0,3}}{_COUNT}\s*$"))


def csv_header_record(cells: list[str], width: int) -> bool:
    """A lone note above the table, or HDR with data beside it. A column that happens
    to be called header has a name next to it, so it stays the names row."""
    if width < 2:
        return False
    rest = [c.strip() for c in cells[1:]]
    if sum(bool(c.strip()) for c in cells) == 1:
        return True
    return len(cells) == width and bool(RECORD_HEADER.match(cells[0].strip())) and any(
        not c or _NUMBER.fullmatch(c) for c in rest)


def csv_trailer_record(cells: list[str], width: int) -> bool:
    return len(cells) != width or bool(TRAILER_CELL.match(cells[0]))


def fixed_header_record(line: str, length: int) -> bool:
    return len(line) != length or bool(RECORD_HEADER.match(line))


def fixed_trailer_record(line: str, length: int) -> bool:
    return len(line) != length or bool(RECORD_TRAILER.match(line))


def declared_rows(lines: Iterable[str]) -> int | None:
    """The count a trailer states about the file, if it states one."""
    for line in lines:
        for pattern in _DECLARED:
            m = pattern.search(line)
            if m:
                return int(m.group(1).replace(",", ""))
    return None


def _trim(src: dict[str, Any], records: list[Any], header_like: Callable[[Any], bool], trailer_like: Callable[[Any], bool],
          text: Callable[[Any], str]) -> tuple[list[Any], dict[str, Any]]:
    """Honour the decision, but only on records that still look like what was decided.
    The decision outlives the file it was made on, and the next extract may come without
    a trailer: a blind cut would drop a real row from every refresh."""
    decided = src.get("records") or {}
    start = 0
    while start < min(decided.get("skip_header", 0), len(records)) and header_like(records[start]):
        start += 1
    end = len(records)
    while len(records) - end < decided.get("skip_footer", 0) and end > start and trailer_like(records[end - 1]):
        end -= 1
    left = 0
    while left < MAX_TRAILER and end - left > start and trailer_like(records[end - left - 1]):
        left += 1
    tail = records[end - left:]
    return records[start:end], {"skipped_header": start, "skipped_footer": len(records) - end, "left_in": left,
                                "declared_rows": declared_rows(text(r) for r in reversed(tail)), "lines": len(records)}


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
    return _csv_with_records(src, text)[0]


def _csv_with_records(src: dict[str, Any], text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    layout = src["layout"]
    delimiter = layout.get("delimiter", ",")
    try:
        table = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
                 if any(c.strip() for c in r)]
    except csv.Error as e:
        raise IngestError(f"source {src['name']}: the file is not readable as csv") from e
    width = Counter(len(r) for r in table).most_common(1)[0][0] if table else 0
    table, report = _trim(src, table, lambda r: csv_header_record(r, width), lambda r: csv_trailer_record(r, width), " ".join)
    if layout.get("header", True):
        header = table[0] if table else []
        table = table[1:]
        width = len(header)
        names = [_column_name(c, i) for i, c in enumerate(header[:width - empty_trailing_columns(header, table)])]
    else:
        names = [c["name"] for c in src["columns"]]
        width = len(names)
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise IngestError(f"source {src['name']}: more than one column reads as {', '.join(dupes)}")
    rows = []
    for cells in table:
        rec: dict[str, Any] = dict(zip(names, cells))
        if len(cells) > width:  # never drop what the file said
            rec["_extra"] = delimiter.join(cells[width:])
        rows.append(rec)
    report["data_rows"] = len(rows) - report["left_in"]
    return rows, report


def fixed_body(src: dict[str, Any], text: str) -> tuple[list[str], dict[str, Any]]:
    """The records to load, and what happened to the rest."""
    # Split on line ends only: str.splitlines also breaks on bytes like 0x85.
    lines = [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]
    length = Counter(len(ln) for ln in lines).most_common(1)[0][0] if lines else 0
    fields = src["layout"]["fields"]

    def header_like(line: str) -> bool:  # a row of names holds no number, a record usually does
        cells = [line[f["start"]:f["start"] + f["width"]].strip() for f in fields]
        return fixed_header_record(line, length) or not any(_NUMBER.fullmatch(c) for c in cells)

    body, report = _trim(src, lines, header_like, lambda ln: fixed_trailer_record(ln, length), str)
    report["data_rows"] = len(body) - report["left_in"]
    return body, report


def _fixed_rows(src: dict[str, Any], lines: list[str]) -> list[dict[str, Any]]:
    return [{f["name"]: line[f["start"]:f["start"] + f["width"]].strip() for f in src["layout"]["fields"]}
            for line in lines]


def _read_upload_fixed_width(src: dict[str, Any], text: str) -> list[dict[str, Any]]:
    return _fixed_with_records(src, text)[0]


def _fixed_with_records(src: dict[str, Any], text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    body, report = fixed_body(src, text)
    return _fixed_rows(src, body), report


def read_upload(src: dict[str, Any]) -> pa.Table:
    """One tenant upload as bronze strings, through the same rows_to_table road."""
    return read_upload_with_records(src)[0]


def read_upload_with_records(src: dict[str, Any]) -> tuple[pa.Table, dict[str, Any]]:
    """The table, plus what the run says about the records that are not data. Counts only."""
    text = _upload_text(src)
    reader = _fixed_with_records if src["format"] == "fixed_width" else _csv_with_records
    rows, report = reader(src, text)
    if not rows:
        raise IngestError(f"source {src['name']}: the file has no data rows")
    seen = {k for r in rows for k in r}
    missing = [c["name"] for c in src["columns"] if c["name"] not in seen]
    if missing:
        raise IngestError(f"source {src['name']}: the file has no column {', '.join(missing)}")
    file_name = Path(src["upload_path"]).name
    for r in rows:
        r["_source_file"] = file_name
    return rows_to_table(rows), report


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
