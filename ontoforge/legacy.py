"""Read legacy systems the way a migration team would: copybook fixed-width files,
old operational databases, CSV drops and JSON event logs. Everything comes out
as Arrow tables of strings plus a few typed columns, exactly as received."""
from __future__ import annotations

import csv
import glob
import json
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


def read_source(src: dict[str, Any], legacy_dir: Path) -> pa.Table:
    kind = src["kind"]
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
        raise ValueError(f"unknown source kind {kind}")
    if not rows:
        return pa.table({})
    cols = list(dict.fromkeys(k for r in rows for k in r))
    # land everything as strings in bronze: never trust legacy types on the way in
    return pa.table({c: pa.array([None if r.get(c) in (None, "") else str(r.get(c)) for r in rows], pa.string())
                     for c in cols})
