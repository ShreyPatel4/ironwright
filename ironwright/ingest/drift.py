"""Did the next extract change shape? A profile diff with a severity on each item.

Three rules keep this honest:

  It reads a file the way bronze does. Names come from legacy's reader, not the
  profiler, because they differ (`date` against `date_`). A check on names the run
  never sees would report drift that is not there.

  It compares against what was accepted, which is the spec plus the last file. A
  real export starts out messy: a trailing comma column, a duplicate key, an amount
  that does not cast. All of that was on the table when a human accepted v1, so the
  question is what got worse, not whether the file is clean.

  It never carries a cell. Reports travel back to the UI and may be narrated. They
  hold counts, plus column names that went through the reader's sanitizer. Values
  stay in memory on the Observation and are never written.

Ontology impact matches by name, because there is no SQL parser here. It follows a
column through each model by its name and through `<column> ... as <alias>`. A column
that only reaches the ontology through a subquery rename can be missed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pyarrow as pa

from ..legacy import (
    IngestError,
    _read_upload_csv,
    _read_upload_fixed_width,
    _upload_text,
)
from . import profiler

MAX_TRAILER = 3
ROW_SWING = 0.5
# A column that already had bad values only counts as changed once it gets this much worse.
DIRTY_TOLERANCE = 0.05
TEXT_TYPES = ("string", "varchar")
NUMERIC = re.compile(r"(int|integer|bigint|double|float|decimal\(.*\))")
_RANK = {"none": 0, "info": 1, "warn": 2, "block": 3}


@dataclass
class Observation:
    """One file, as the run would read it. In memory only: `cells` is tenant data."""
    rows: int = 0                          # data rows, without trailer records
    rows_read: int = 0                     # what bronze would hold
    cells: dict[str, list[str]] = field(default_factory=dict)
    present: list[str] = field(default_factory=list)
    past_layout: int = 0                   # fixed width: the last byte in use, at least the layout's end
    error: str | None = None


def _data(rows: list[dict[str, Any]], first_cell: Any, trailer: Any) -> list[dict[str, Any]]:
    """Trailer records stay in bronze and leave the comparison. One TRL line would
    otherwise read as a type change on the column under it."""
    end = len(rows)
    while end and len(rows) - end < MAX_TRAILER and trailer(first_cell(rows[end - 1])):
        end -= 1
    return rows[:end]


def _observe_csv(src: dict[str, Any], text: str) -> Observation:
    rows = _read_upload_csv(src, text)
    data = _data(rows, lambda r: next(iter(r.values()), ""), profiler._TRAILER.match)
    names = list(dict.fromkeys(k for r in rows for k in r))
    return Observation(rows=len(data), rows_read=len(rows), present=names,
                       cells={n: [r[n] for r in data if n in r] for n in names})


def _observe_fixed(src: dict[str, Any], text: str) -> Observation:
    fields = src["layout"]["fields"]
    lines = [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]
    start = 0
    while start < min(len(lines), MAX_TRAILER) and profiler._RECORD_HEADER.match(lines[start]):
        start += 1
    data = _data([{"line": ln} for ln in lines[start:]], lambda r: r["line"], profiler._RECORD_TRAILER.match)
    body = [r["line"] for r in data]
    rows = _read_upload_fixed_width(src, "\n".join(body))
    end = max(f["start"] + f["width"] for f in fields)
    cells = {f["name"]: [r[f["name"]] for r in rows] for f in fields}
    tail = max((len(ln.rstrip()) for ln in body), default=0)
    return Observation(rows=len(rows), rows_read=len(lines), cells=cells,
                       present=[n for n, vals in cells.items() if any(vals)],
                       past_layout=max(tail, end))


def observe(src: dict[str, Any]) -> Observation:
    """A file that cannot be read is an answer, not a crash. The message is IngestError's,
    which names the source and never a cell."""
    try:
        text = _upload_text(src)
        out = _observe_fixed(src, text) if src["format"] == "fixed_width" else _observe_csv(src, text)
    except IngestError as e:
        return Observation(error=str(e))
    if not out.rows_read:
        return Observation(error=f"source {src['name']}: the file has no data rows")
    return out


def _is_null(value: str) -> bool:
    return value.strip().lower() in profiler.NULL_TOKENS


def _values(obs: Observation, column: str) -> list[str]:
    return [v.strip() for v in obs.cells.get(column, []) if not _is_null(v)]


def _engine() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("set enable_external_access = false")
    return con


def _uncastable(obs: Observation, column: str, sql_type: str) -> float:
    """The share of present values that do not cast. It uses try_cast, the same test
    as the run's type_castable check. sql_type comes from a validated spec."""
    values = _values(obs, column)
    if not values:
        return 0.0
    con = _engine()
    con.register("_v", pa.table({"v": pa.array(values, pa.string())}))
    bad = con.execute(f"select count(*) from _v where try_cast(v as {sql_type}) is null").fetchone()[0]
    return bad / len(values)


def _duplicates(obs: Observation, column: str) -> tuple[int, int]:
    values = [v.strip() for v in obs.cells.get(column, [])]
    present = [v for v in values if not _is_null(v)]
    return len(present) - len(set(present)), len(values) - len(present)


def _item(kind: str, severity: str, message: str, **more: Any) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "message": message, "column": "", "ontology": [], **more}


def _missing(src: dict[str, Any], new: Observation, base: Observation | None, loaded_before: bool) -> list[dict[str, Any]]:
    declared = src["columns"]
    fixed = src["format"] == "fixed_width"
    gone = [c for c in declared if c["name"] not in new.present
            # A fixed width field that was always blank was accepted that way. It has no header to lose.
            and not (fixed and base is not None and c["name"] not in base.present)]
    whole = len(gone) == len(declared)
    out = []
    for c in gone:
        name = c["name"]
        if c.get("key"):
            severity, why = "block", f"The key column {name} is gone."
        elif whole:
            severity, why = "block", f"None of the declared columns are here, {name} included."
        elif not loaded_before and not fixed:
            severity, why = "block", f"The file has no column {name}, and bronze has no earlier run to keep it."
        else:
            severity, why = "warn", f"The column {name} is gone. Its rows read as null in this version."
        out.append(_item("missing_column", severity, why, column=name))
    return out


def _new_columns(src: dict[str, Any], new: Observation, base: Observation | None) -> list[dict[str, Any]]:
    if base is None:
        return []
    if src["format"] == "fixed_width":
        if new.past_layout <= base.past_layout:
            return []
        name = f"bytes_{base.past_layout}_{new.past_layout}"
        return [_item("new_column", "info", f"Records now run past the layout, to byte {new.past_layout}. "
                      "Bronze does not load those bytes until the layout names them.", column=name,
                      start=base.past_layout, width=new.past_layout - base.past_layout)]
    declared = {c["name"] for c in src["columns"]}
    return [_item("new_column", "info", f"The file has a new column {n}. Bronze keeps it, and older runs read as null.", column=n)
            for n in new.present if n not in declared and n not in base.present]


def _type_changes(src: dict[str, Any], new: Observation, base: Observation | None) -> list[dict[str, Any]]:
    if base is None:
        return []
    out = []
    for c in src["columns"]:
        name, kind = c["name"], c["type"]
        if kind in TEXT_TYPES or name not in new.present or name not in base.present:
            continue
        before, after = _uncastable(base, name, kind), _uncastable(new, name, kind)
        if after and after > before + (DIRTY_TOLERANCE if before else 0):
            severity = "block" if c.get("key") else "warn"
            out.append(_item("type_change", severity,
                             f"{round(after * 100)}% of {name} no longer fits {kind}, up from {round(before * 100)}%.",
                             column=name, declared=kind, uncastable_from=round(before, 4), uncastable_to=round(after, 4)))
    return out


def _uniqueness(src: dict[str, Any], new: Observation, base: Observation | None) -> list[dict[str, Any]]:
    if base is None:
        return []
    out = []
    for c in src["columns"]:
        name = c["name"]
        if not c.get("key") or name not in new.present or name not in base.present:
            continue
        if any(_duplicates(base, name)):
            continue  # accepted with duplicates, so nothing was lost
        dupes, nulls = _duplicates(new, name)
        if dupes or nulls:
            out.append(_item("key_uniqueness_loss", "warn",
                             f"The key {name} was unique and now has {dupes} repeats and {nulls} nulls.",
                             column=name, duplicates=dupes, nulls=nulls))
    return out


def _swing(new: Observation, base: Observation | None) -> list[dict[str, Any]]:
    if base is None or not base.rows or abs(new.rows - base.rows) / base.rows < ROW_SWING:
        return []
    return [_item("row_count_swing", "warn", f"The row count went from {base.rows} to {new.rows}. "
                  "If this is a partial extract, models on the latest run only see these rows.",
                  **{"from": base.rows, "to": new.rows})]


def diff(src: dict[str, Any], new: Observation, base: Observation | None, *, version: int,
         against: int | None, loaded_before: bool) -> dict[str, Any]:
    """The report for `new`, against the accepted spec and the file before it."""
    if new.error:
        items = [_item("unreadable", "block", new.error)]
    else:
        items = (_missing(src, new, base, loaded_before) + _new_columns(src, new, base)
                 + _type_changes(src, new, base) + _uniqueness(src, new, base) + _swing(new, base))
    severity = max((i["severity"] for i in items), key=_RANK.__getitem__, default="none")
    before = set(base.present) if base else set(new.present)
    return {"source": src["name"], "version": version, "against_version": against, "severity": severity,
            "blocking": severity == "block", "items": items, "ontology_impact": False,
            "profile_diff": {
                "rows": {"from": base.rows if base else None, "to": new.rows},
                "columns": {"added": [i["column"] for i in items if i["kind"] == "new_column"],
                            "removed": sorted(before - set(new.present))}}}


def blocking_message(report: dict[str, Any]) -> str:
    why = " ".join(i["message"] for i in report["items"] if i["severity"] == "block")
    return (f"source {report['source']} version {report['version']}: {why} "
            "Update the spec or upload a corrected file, then run again.")


def compare(src: dict[str, Any], old: Observation, new: Observation) -> dict[str, Any]:
    """The new version against the last: rows, keys in and out, measure totals.
    Totals and counts only."""
    key = next((c["name"] for c in src["columns"] if c.get("key") and c["name"] in old.present and c["name"] in new.present), None)
    keys = None
    if key:
        a, b = set(_values(old, key)), set(_values(new, key))
        keys = {"key": key, "added": len(b - a), "removed": len(a - b), "kept": len(a & b)}
    measures = {}
    for c in src["columns"]:
        if NUMERIC.fullmatch(c["type"]) and not c.get("key") and c["name"] in old.present and c["name"] in new.present:
            measures[c["name"]] = {"from": _total(old, c["name"]), "to": _total(new, c["name"])}
    return {"rows": {"from": old.rows, "to": new.rows}, "keys": keys, "measures": measures}


def _total(obs: Observation, column: str) -> float:
    values = _values(obs, column)
    if not values:
        return 0.0
    con = _engine()
    con.register("_v", pa.table({"v": pa.array(values, pa.string())}))
    return round(float(con.execute("select coalesce(sum(try_cast(v as double)), 0) from _v").fetchone()[0]), 6)


# ---------------------------------------------------------------- ontology impact

def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", re.IGNORECASE)


def _carried(sql: str, names: set[str]) -> set[str]:
    """The names a column has after this model: its own where the SQL mentions it or
    selects *, and any alias on the same projection."""
    out = set()
    for n in names:
        mentioned = _word(n).search(sql) is not None
        if mentioned or re.search(r"select\s+(\w+\.)?\*", sql, re.IGNORECASE):
            out.add(n)
        if mentioned:
            out |= {m.lower() for m in re.findall(rf"(?<![a-z0-9_]){re.escape(n)}(?![a-z0-9_])[^,\n]*\bas\s+([a-z_]\w*)", sql, re.IGNORECASE)}
    return out


def _lineage(source: str, column: str, models: list[tuple[str, str, str]]) -> dict[str, set[str]]:
    """table (layer.name) -> the names this bronze column goes by there."""
    tables = {f"bronze.{source}": {column}}
    views = {f"bronze_{source}": f"bronze.{source}", f"bronze_{source}_all": f"bronze.{source}"}
    for layer, name, sql in models:  # silver before gold, file name order, as the run does
        names: set[str] = set()
        for view, table in views.items():
            if table in tables and _word(view).search(sql):
                names |= _carried(sql, tables[table])
        if names:
            tables[f"{layer}.{name}"] = names
            views[f"{layer}_{name}"] = f"{layer}.{name}"
    return tables


def ontology_uses(source: str, column: str, ontology: dict[str, Any], models: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    """Which accepted objects and links read this bronze column. Objects, then links, by name."""
    tables = _lineage(source, column, models)
    out = []
    for name, tdef in sorted((ontology.get("object_types") or {}).items()):
        for s in tdef.get("sources") or []:
            names = tables.get(s.get("table"), set())
            used = {s.get("key"), *(s.get("fields") or {}).values()}
            if names & used or any(_word(n).search(s.get("where", "")) for n in names):
                out.append({"kind": "object", "name": name})
                break
    for name, ldef in sorted((ontology.get("link_types") or {}).items()):
        sql = ldef.get("sql", "")
        for table, names in tables.items():
            if _word(table.replace(".", "_")).search(sql) and any(_word(n).search(sql) for n in names):
                out.append({"kind": "link", "name": name})
                break
    return out


def annotate(report: dict[str, Any], ontology: dict[str, Any], models: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Mark what touches the accepted ontology. A new column cannot: nothing reads it yet."""
    for item in report["items"]:
        touches = item["column"] and item["kind"] != "new_column"
        item["ontology"] = ontology_uses(report["source"], item["column"], ontology, models) if touches else []
    report["ontology_impact"] = any(i["ontology"] for i in report["items"])
    return report
