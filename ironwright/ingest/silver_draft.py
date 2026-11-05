"""From raw uploads to silver SQL: rules decide, and a human unticks.

Nothing else in the journey cleans a customer's export, and the ontology needs clean
rows: a duplicate leaves a table with no key, and 'p-100 ' never meets 'P-100'. So the
drafter proposes one model per source, as DuckDB SQL, with a decision list beside it.

```mermaid
flowchart LR
  P[profile + proposed spec] --> D[draft, in the profile job]
  B[bronze rows, as the run reads them] --> D
  D -->|decisions + counts| M{Map step: human unticks}
  M -->|declined ids, renames| A[assemble, no data read]
  A --> S[models/silver/*.sql]
  S --> R[run builds silver]
  R --> O[ontology proposer]
```

The draft reads tenant rows, so it runs in a job. Assembling reads no data, so a route may
do it. Each stage of the SQL belongs to one decision, which is what makes unticking cheap
and what lets the counts come from running the draft's own SQL on the real rows.

What the SQL may hold is narrow on purpose: quoted names that passed a plain identifier
check, and literals from the fixed lists in this file and the profiler. Tenant text never
becomes a literal. A draft holds no cell value either: reasons carry counts and names.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from .. import sqlguard
from .. import workspaces_tenant as wt
from ..config import FILE_NAME, private_dir, safe_child
from ..legacy import IngestError, read_upload
from .profiler import (
    _COMPACT_DATE,
    _DATE_FORMATS,
    _RECORD_HEADER,
    _RECORD_TRAILER,
    _TIMESTAMP_FORMATS,
    _TRAILER,
    NULL_TOKENS,
    profile_files,
)

DRAFT_FILE = "silver_draft.json"
MAX_DECLINED = 2000

_IDENT = re.compile(r"[a-z][a-z0-9_]{0,62}")
_KEY_LIKE = re.compile(r"(^|_)(id|ref|key|nbr|no|num|number|code)$")
_RECENT = re.compile(r"(^|_)(updated|modified|changed|latest|last|as_of)(_|$)")
_TEXT = ("string", "varchar")
_TEMPORAL = ("date", "timestamp")
_WHOLE = ("int", "integer", "bigint")
_CASTS = {"currency", "date", "timestamp", "boolean", "plain"}
_FORMATS = (*_DATE_FORMATS, _COMPACT_DATE, *_TIMESTAMP_FORMATS)
_NULLS = ", ".join(f"'{t}'" for t in sorted(NULL_TOKENS))
_MONEY_NOISE = r"'[$€£,\s]'"
_MIN_KEY_SHARE = 0.5


class DraftError(ValueError):
    """A choice that cannot apply. The message is ours and is safe to show."""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------- one column, as SQL

def _cleaned_text(raw: str, column: dict[str, Any], declined: set[str]) -> str:
    folds = column.get("normalize_id") and column["normalize_id"] not in declined
    trims = column.get("trim_id") and column["trim_id"] not in declined
    out = f"upper(trim({raw}))" if folds else f"trim({raw})" if trims else raw
    if column.get("null_id") and column["null_id"] not in declined:
        out = f"case when lower(trim({raw})) in ({_NULLS}) then null else {out} end"
    return out


def _formats(column: dict[str, Any]) -> list[str]:
    formats = column.get("formats") or []
    if any(f not in _FORMATS for f in formats):
        raise DraftError(f"{column['name']}: that date format is not one I know")
    return list(formats)


def _cast_kind(column: dict[str, Any], spec_type: str) -> str | None:
    """A type the human chose in the Map step wins over the one the profile saw."""
    if spec_type in _TEXT:
        return None
    if spec_type == column["type"]:
        kind = column["cast"]
        if kind is not None and kind not in _CASTS:
            raise DraftError(f"{column['name']}: that cast is not one I know")
        return kind
    return spec_type if spec_type in (*_TEMPORAL, "boolean") else "plain"


def _sql_type(spec_type: str) -> str:
    if not wt._TYPE.fullmatch(spec_type):
        raise DraftError(f"{spec_type!r} is not a type I know")
    # The profile says int for anything up to 64 bits, and a DuckDB int is 32.
    return "bigint" if spec_type in ("int", "integer") else spec_type


def _cast(text: str, kind: str, spec_type: str, formats: list[str]) -> str:
    x = f"trim({text})"
    if kind == "currency":
        return f"try_cast(regexp_replace({x}, {_MONEY_NOISE}, '', 'g') as {_sql_type(spec_type)})"
    if kind in _TEMPORAL and formats:
        parsed = ", ".join(f"try_strptime({x}, {_literal(f)})" for f in formats)
        return f"coalesce({parsed})::{kind}"
    if kind == "boolean":
        return (f"case when lower({x}) in ('y', 'yes', 't', 'true') then true "
                f"when lower({x}) in ('n', 'no', 'f', 'false') then false end")
    return f"try_cast({x} as {_sql_type(spec_type)})"


def _column_sql(column: dict[str, Any], spec_type: str, declined: set[str]) -> tuple[str, bool]:
    """(the expression, whether it comes out as a date or a timestamp)."""
    _sql_type(spec_type)
    text = _cleaned_text(_quote(column["name"]), column, declined)
    kind = _cast_kind(column, spec_type)
    if kind is None or column["cast_id"] in declined:
        return text, False
    return _cast(text, kind, spec_type, _formats(column)), kind in _TEMPORAL


# ---------------------------------------------------------------- one source, as stages

def _trailer_filter(source: dict[str, Any]) -> str | None:
    found = source.get("trailer")
    if not found:
        return None
    pattern, flags = _TRAILER_PATTERNS[found["pattern"]]
    if not _IDENT.fullmatch(found["column"]):
        raise DraftError("the trailer check names a bad column")
    return f"not regexp_matches(coalesce({_quote(found['column'])}, ''), {_literal(pattern)}, {_literal(flags)})"


_TRAILER_PATTERNS = {
    "delimited": ("^" + _TRAILER.pattern, "i"),
    "fixed": (f"^{_RECORD_TRAILER.pattern}|^{_RECORD_HEADER.pattern}", "c"),
}


def _chosen(source: dict[str, Any], spec: dict[str, Any], renames: dict[str, str]) -> list[tuple[dict[str, Any], str, str]]:
    """(plan, the spec's type, the name it leaves under), in the spec's order."""
    plan = {c["name"]: c for c in source["columns"]}
    chosen = [(plan[c["name"]], c["type"], renames.get(c["name"], c["name"]))
              for c in spec["columns"] if c["name"] in plan]
    if not chosen:
        raise DraftError(f"{source['name']}: no column of the file is left to select")
    unknown = sorted(set(renames) - {p["name"] for p, _, _ in chosen})
    if unknown:
        raise DraftError(f"{source['name']}: no column {unknown[0]} to rename")
    out = [name for _, _, name in chosen]
    for name in out:
        if not isinstance(name, str) or not _IDENT.fullmatch(name):
            raise DraftError(f"{source['name']}: a column name is lowercase letters, digits and underscores")
    if len(set(out)) != len(out):
        raise DraftError(f"{source['name']}: two columns would have the same name")
    return chosen


def _key_of(source: dict[str, Any], spec: dict[str, Any], chosen: list[tuple[dict[str, Any], str, str]]) -> str | None:
    """A key the human marked in the Map step wins. The mark the proposal came with does
    not: that one only says some column happens to be unique."""
    names = [p["name"] for p, _, _ in chosen]
    marked = [c["name"] for c in spec["columns"] if c.get("key") and c["name"] in names]
    if marked and marked[0] != source.get("marked"):
        return marked[0]
    return source["key"] if source["key"] in names else None


def _order_for_latest(key: str, exprs: list[tuple[dict[str, Any], str, bool]]) -> list[str]:
    """Latest date first, then every other column: bronze keeps no row position, so a
    tie has to break on what the row says or the pick changes between runs."""
    dates = [p["name"] for p, _, is_date in exprs if is_date]
    dates.sort(key=lambda n: not _RECENT.search(n))
    latest = dates[:1]
    rest = [p["name"] for p, _, _ in exprs if p["name"] != key and p["name"] not in latest]
    return [f"{_quote(n)} desc nulls last" for n in latest] + [f"{_quote(n)} asc nulls last" for n in rest]


def _stages(source: dict[str, Any], spec: dict[str, Any], declined: set[str],
            renames: dict[str, str]) -> tuple[list[tuple[str, str]], list[tuple[dict[str, Any], str, str]]]:
    name = source["name"]
    if not _IDENT.fullmatch(name):
        raise DraftError("a source name is lowercase letters, digits and underscores")
    chosen = _chosen(source, spec, renames)
    exprs = [(p, *_column_sql(p, spec_type, declined)) for p, spec_type, _ in chosen]
    raw = ", ".join(_quote(p["name"]) for p, _, _ in chosen)
    filtered = _trailer_filter(source) if f"{name}.drop_trailer" not in declined else None
    if filtered:  # a subquery, because the check reads a column the Map step may have dropped
        raw_from = f"(select * from bronze_{name} where {filtered})"
    else:
        raw_from = f"bronze_{name}"
    stages = [("source", f"select {raw} from {raw_from}"),
              ("cleaned", "select " + ", ".join(f"{sql} as {_quote(p['name'])}" for p, sql, _ in exprs) + " from source")]
    last = "cleaned"
    if f"{name}.drop_exact_duplicates" not in declined:
        stages.append(("unique_rows", f"select distinct * from {last}"))
        last = "unique_rows"
    key = _key_of(source, spec, chosen)
    if key is not None:
        quoted = _quote(key)
        if f"{name}.drop_null_keys" not in declined:
            stages.append(("with_keys", f"select * from {last} where {quoted} is not null"))
            last = "with_keys"
        if f"{name}.collapse_on_key" not in declined:
            order = _order_for_latest(key, exprs)
            over = f"partition by {quoted}" + (f" order by {', '.join(order)}" if order else "")
            stages.append(("collapsed", f"select * from {last} qualify row_number() over ({over}) = 1"))
    return stages, chosen


def _with(stages: list[tuple[str, str]], tail: str) -> str:
    return "with " + ",\n".join(f"{n} as (\n  {sql}\n)" for n, sql in stages) + f"\n{tail}\n"


def assemble(source: dict[str, Any], spec: dict[str, Any], declined: set[str], renames: dict[str, str]) -> str:
    """The model's SQL for these choices. Reads no data, so a route may call it."""
    stages, chosen = _stages(source, spec, declined, renames)
    select = ", ".join(f"{_quote(p['name'])} as {_quote(out)}" if p["name"] != out else _quote(out)
                       for p, _, out in chosen)
    return _with(stages, f"select {select} from {stages[-1][0]}")


def _draftable(made: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in made["sources"] if "error" not in s]


def known_ids(made: dict[str, Any]) -> set[str]:
    return {d["id"] for s in _draftable(made) for d in s["decisions"]}


def models(made: dict[str, Any], workspace: dict[str, Any], declined: Iterable[str],
           renames: dict[str, dict[str, str]]) -> dict[str, str]:
    """{file name: SQL} for every source the draft could read and the spec still has."""
    declined = set(declined)
    unknown = sorted(declined - known_ids(made))
    if unknown:
        raise DraftError(f"no decision named {unknown[0]}")
    specs = {s["name"]: s for s in workspace["sources"]}
    drafted = {s["name"]: s for s in _draftable(made) if s["name"] in specs}
    strays = sorted(set(renames) - set(drafted))
    if strays:
        raise DraftError(f"no source named {strays[0]} to rename in")
    return {s["file"]: assemble(s, specs[n], declined, renames.get(n, {})) for n, s in drafted.items()}


# ---------------------------------------------------------------- drafting, on real rows

def _engine(name: str, table: pa.Table) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("_raw", table)
    con.execute(f"create view bronze_{name} as select * from _raw")
    sqlguard.apply_limits(con)
    con.execute("set enable_external_access = false")
    con.execute("set lock_configuration = true")
    return con


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return con.execute(sql).fetchone()[0]


def _decision(source: str, kind: str, confidence: float, reason: str, rows: int, of: int,
              column: str | None = None, **more: Any) -> dict[str, Any]:
    made = f"{source}.{kind}" + (f".{column}" if column else "")
    return {"id": made, "kind": kind, **({"column": column} if column else {}),
            "confidence": confidence, "reason": reason, "rows": rows, "of": of, **more}


def _singular_words(stem: str) -> set[str]:
    words = stem.split("_")
    return set(words) | {re.sub(r"ies$", "y", w) if w.endswith("ies") else w.removesuffix("s") for w in words}


def _named_for_source(column: str, source: str) -> bool:
    m = re.fullmatch(r"(?:(.+)_)?(id|ref|key|nbr|no|num|number|code)", column)
    return bool(m) and (m.group(1) is None or m.group(1) in _singular_words(source))


def _choose_key(con: duckdb.DuckDBPyConnection, name: str, profile_file: dict[str, Any], spec: dict[str, Any],
                plan: list[dict[str, Any]], cleaned: dict[str, str]) -> tuple[str | None, float, str]:
    """A name that says it is this table's key comes first. Duplicates hide a real key from
    the profile, which is the whole reason this exists, while a note column can be unique
    by luck. So the name only has to show that most of its values differ. After that, what
    the spec marks and what the profile proved."""
    usable = {c["name"] for c in plan}
    candidates = [c["name"] for c in plan if _named_for_source(c["name"], name)
                  and (c["type"] in _TEXT or c["type"] in _WHOLE)]
    for column in candidates:
        total, filled, distinct = con.execute(
            f"select count(*), count(v), count(distinct v) from (select {cleaned[column]} as v from bronze_{name})").fetchone()
        if filled and filled / total >= _MIN_KEY_SHARE and distinct / filled >= _MIN_KEY_SHARE:
            return column, 0.85, (f"The name {column} reads as this table's key. It holds {distinct} distinct "
                                  f"values in {filled} rows.")
    for column in [c["name"] for c in spec["columns"] if c.get("key") and c["name"] in usable]:
        return column, 0.7, f"The spec marks {column} as the key."
    for k in profile_file.get("candidate_keys") or []:
        if k["column"] in usable:
            return k["column"], k["confidence"], f"The profile found {k['column']} unique and never empty."
    if candidates:
        return None, 0.0, (f"I found no key. {candidates[0]} reads like one, but its values repeat too often, "
                           "so these rows may be lines and I will not collapse them.")
    return None, 0.0, "I found no key. No column is unique, and no column name reads as this table's own key."


def _plan_column(con: duckdb.DuckDBPyConnection, name: str, column: str, kind: str, key_like: bool,
                 rows: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = _quote(column)
    plan: dict[str, Any] = {"name": column, "type": kind, "cast": None, "cast_id": f"{name}.cast.{column}"}
    decisions = []
    tokens = _count(con, f"select count(*) from bronze_{name} where lower(trim({raw})) in ({_NULLS})")
    if tokens:
        plan["null_id"] = f"{name}.null_tokens.{column}"
        decisions.append(_decision(name, "null_tokens", 0.95, f"{tokens} rows in {column} hold a word that means empty, "
                                   "like N/A or NULL. I read those as null.", tokens, rows, column))
    if kind in _TEXT:
        changed = _count(con, f"select count(*) from bronze_{name} where upper(trim({raw})) is distinct from {raw}")
        trimmed = _count(con, f"select count(*) from bronze_{name} where trim({raw}) is distinct from {raw}")
        sensitive = _count(con, f"select count(*) from bronze_{name} where regexp_matches({raw}, '[a-z]') "
                                f"and regexp_matches({raw}, '[A-Z]')")
        if key_like and not sensitive:
            plan["normalize_id"] = f"{name}.normalize_key.{column}"
            decisions.append(_decision(name, "normalize_key", 0.9, f"{column} reads like a key, so I trim it and upper case it. "
                                       "The same rule runs on every key, so the two ends of a link still meet.",
                                       changed, rows, column))
        elif trimmed:
            plan["trim_id"] = f"{name}.trim.{column}"
            why = (f"{column} reads like a key, but some values mix upper and lower case, so case may mean something. "
                   "I only trim it." if key_like and sensitive else f"I trim the spaces around {column}.")
            decisions.append(_decision(name, "trim", 0.95, why, trimmed, rows, column))
    return plan, decisions


def _plan_cast(kind: str, profile_col: dict[str, Any] | None) -> tuple[str | None, list[str]]:
    seen = (profile_col or {}).get("formats") or {}
    single = (profile_col or {}).get("format")
    formats = [f for f in _FORMATS if f in seen or f == single]
    if kind in _TEXT:
        return None, []
    if kind in _TEMPORAL:
        return kind, formats
    if kind == "boolean":
        return "boolean", []
    cleaning = (profile_col or {}).get("cleaning") or {}
    return ("currency" if cleaning.get("kind") == "currency" else "plain"), []


def _cast_decision(con: duckdb.DuckDBPyConnection, name: str, plan: dict[str, Any], rows: int, confidence: float) -> dict[str, Any]:
    column, kind = plan["name"], plan["type"]
    text = _cleaned_text(_quote(column), plan, set())
    casted = _cast(text, plan["cast"], kind, _formats(plan))
    filled, lost = con.execute(f"select count(v), count(*) filter (where v is not null and c is null) "
                               f"from (select {text} as v, {casted} as c from bronze_{name})").fetchone()
    how = {"currency": f"I drop the currency symbols and commas in {column} and read it as {kind}.",
           "boolean": f"I read the yes and no spellings in {column} as true and false."}.get(plan["cast"], f"I read {column} as {kind}.")
    if plan.get("formats"):
        how = f"I read {column} as {kind}, using the {len(plan['formats'])} formats I saw."
    if lost:
        how += f" {lost} of {filled} values do not fit and read as null. The text stays in bronze."
    extra = {"formats": plan["formats"]} if plan.get("formats") else {}
    return _decision(name, "cast", round(min(confidence, 1 - lost / filled) if filled else confidence, 4), how,
                     filled, rows, column, lost=lost, to=kind, **extra)


def _find_trailer(con: duckdb.DuckDBPyConnection, name: str, f: dict[str, Any], table: pa.Table) -> dict[str, str] | None:
    expected = f.get("trailer_rows", 0) + f.get("leader_rows", 0)
    if not expected or not table.column_names:
        return None
    found = {"column": table.column_names[0], "pattern": "fixed" if f["format"] == "fixed_width" else "delimited"}
    hits = _count(con, f"select count(*) from bronze_{name} where not ({_trailer_filter({'trailer': found})})")
    # More hits than the profile saw at the ends of the file means real rows look like this too.
    return found if 0 < hits <= expected else None


def _draft_source(name: str, f: dict[str, Any], spec: dict[str, Any], table: pa.Table, file_name: str, joined: set[str]) -> dict[str, Any]:
    con = _engine(name, table)
    try:
        rows = table.num_rows
        present = set(table.column_names)
        profiled = {c["name"]: c for c in f["columns"]}
        plan, decisions = [], []
        for col in spec["columns"]:
            if col["name"] not in present:
                continue
            key_like = bool(_KEY_LIKE.search(col["name"])) or col["name"] in joined
            one, more = _plan_column(con, name, col["name"], col["type"], key_like, rows)
            one["cast"], one["formats"] = _plan_cast(col["type"], profiled.get(col["name"]))
            plan.append(one)
            decisions += more
        cleaned = {p["name"]: _cleaned_text(_quote(p["name"]), p, set()) for p in plan}
        key, key_confidence, key_reason = _choose_key(con, name, f, spec, plan, cleaned)
        for p in plan:
            if p["cast"] is not None:
                confidence = (profiled.get(p["name"]) or {}).get("confidence", 0.8)
                decisions.append(_cast_decision(con, name, p, rows, confidence))
        marked = next((c["name"] for c in spec["columns"] if c.get("key")), None)
        source = {"name": name, "file": file_name, "key": key, "key_reason": key_reason, "marked": marked, "columns": plan,
                  "rows_in": rows, "decisions": decisions}
        trailer = _find_trailer(con, name, f, table)
        if trailer:
            source["trailer"] = trailer
            hits = _count(con, f"select count(*) from bronze_{name} where not ({_trailer_filter(source)})")
            decisions.insert(0, _decision(name, "drop_trailer", 0.9, f"{hits} rows look like a header or trailer record, "
                                          "not data. Bronze keeps them and silver leaves them out.", hits, rows))
        stages, _ = _stages(source, spec, set(), {})
        by_stage = {n: _count(con, _with(stages[: i + 1], f"select count(*) from {n}")) for i, (n, _) in enumerate(stages)}
        before = by_stage["cleaned"]
        decisions.append(_decision(name, "drop_exact_duplicates", 0.99, "Rows that match in every column once they are "
                                   "clean are the same row. I keep one.", before - by_stage["unique_rows"], rows))
        if key is not None:
            decisions.append(_decision(name, "drop_null_keys", 0.8, f"A row with no {key} cannot become an object, "
                                       "so silver leaves it out. It stays in bronze.",
                                       by_stage["unique_rows"] - by_stage["with_keys"], rows))
            dates = [p["name"] for p in plan if p["cast"] in _TEMPORAL]
            dates.sort(key=lambda n: not _RECENT.search(n))
            keep = (f"I keep the latest by {dates[0]}, then a fixed order of the other columns." if dates else
                    "There is no date to go by and bronze keeps no row position, so I keep the first in a fixed order of the other columns.")
            decisions.append(_decision(name, "collapse_on_key", min(key_confidence, 0.85 if dates else 0.7),
                                       f"{key_reason} Silver keeps one row per {key}. {keep}",
                                       by_stage["with_keys"] - by_stage["collapsed"], rows, key_column=key))
        source["sql"] = assemble(source, spec, set(), {})
        source["rows_out"] = _count(con, f"select count(*) from ({source['sql']})")
        return source
    finally:
        con.close()


def _join_columns(profile: dict[str, Any], file_name: str) -> set[str]:
    out = set()
    for j in profile.get("candidate_joins") or []:
        for side in (j["left"], j["right"]):
            f, _, column = side.rpartition(".")
            if f == file_name:
                out.add(column)
    return out


def draft(profile: dict[str, Any], workspace: dict[str, Any], read: Callable[[dict[str, Any]], pa.Table]) -> dict[str, Any]:
    """{sources: [...]}. `read` returns a source's rows as the run would land them. A source
    it cannot read or draft gets an error, and the others go on. The same rows give the
    same draft."""
    files = {f["name"]: f for f in profile["files"] if "error" not in f}
    out = []
    for i, spec in enumerate(workspace["sources"], 1):
        name = spec["name"]
        file_name = spec["upload"].rsplit("/", 1)[-1]
        try:
            if file_name not in files:
                raise IngestError(f"source {name}: there is no profile for this file")
            table = read(spec)
            out.append(_draft_source(name, files[file_name], spec, table, f"{i:02d}_{name}.sql", _join_columns(profile, file_name)))
        except IngestError as e:
            out.append({"name": name, "error": str(e)})
        except (duckdb.Error, DraftError):  # DuckDB's own text can quote a cell, so it stays here
            out.append({"name": name, "error": f"source {name}: I could not draft a silver model for this file."})
    return {"sources": out}


def read_from_folder(folder: Path) -> Callable[[dict[str, Any]], pa.Table]:
    """How the profile job reads a proposed source: the upload is right there."""
    def read(spec: dict[str, Any]) -> pa.Table:
        path = safe_child(folder, spec["upload"].rsplit("/", 1)[-1], pattern=FILE_NAME)
        return read_upload({**spec, "kind": "upload", "upload_path": str(path)})
    return read


# ---------------------------------------------------------------- a workspace's draft

def _saved_path(org_id: str, slug: str) -> Path:
    return wt._existing(org_id, slug) / DRAFT_FILE


def save(org_id: str, slug: str, made: dict[str, Any], declined: Iterable[str], renames: dict[str, dict[str, str]]) -> dict[str, Any]:
    record = {"draft": made, "declined": sorted(set(declined)), "renames": renames}
    wt._replace(_saved_path(org_id, slug), json.dumps(record, indent=1, sort_keys=True))
    return record


def read_saved(org_id: str, slug: str) -> dict[str, Any] | None:
    """None when no draft was kept. FileNotFoundError when this org has no such workspace."""
    path = _saved_path(org_id, slug)
    try:
        return json.loads(wt._read_small(path, DRAFT_FILE))
    except FileNotFoundError:
        return None


def _write_models(d: Path, files: dict[str, str]) -> None:
    """Replaces the model of each drafted table. A model it did not draft stays."""
    wt._models({"silver": files})
    folder = private_dir(d / "models" / "silver")
    tables = {wt._model_table(n) for n in files}
    for old in folder.iterdir():
        if old.name not in files and old.suffix == ".sql" and wt._model_table(old.name) in tables:
            old.unlink()
    for name, sql in files.items():
        wt._replace(folder / name, sql)


def apply_choices(org_id: str, slug: str, declined: list[str], renames: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Regenerate: the kept draft with these choices becomes models/silver. Reads no rows."""
    saved = read_saved(org_id, slug)
    if saved is None:
        raise DraftError("this workspace has no silver draft yet: queue a silver_draft job first")
    d = wt._existing(org_id, slug)
    spec = wt._normalize(org_id, wt._spec_of(d), wt._read_models(d))
    files = models(saved["draft"], spec, declined, renames)
    _write_models(d, files)
    return {**save(org_id, slug, saved["draft"], declined, renames), "models": files}


def redraft(org_id: str, slug: str, log: Callable[[str], Any]) -> dict[str, int]:
    """The job behind regenerate. It reads the latest upload of each source and keeps a new
    draft, with the human's earlier choices where they still mean something. It changes no
    model, so starting it, which a member may do, changes nothing a run executes."""
    d = wt._existing(org_id, slug)
    spec = wt._normalize(org_id, wt._spec_of(d), wt._read_models(d))
    saved = read_saved(org_id, slug) or {"declined": [], "renames": {}}
    sources = []
    for i, src in enumerate(spec["sources"], 1):
        name = src["upload"].rsplit("/", 1)[-1]
        profile = profile_files([(name, Path(src["upload_path"]).read_bytes())])
        sources += draft(profile, {"sources": [src]}, read_upload)["sources"]
        if "file" in sources[-1]:
            sources[-1]["file"] = f"{i:02d}_{src['name']}.sql"
    made = {"sources": sources}
    keep = set(saved["declined"]) & known_ids(made)
    save(org_id, slug, made, keep, saved["renames"])
    log(f"[{slug}] drafted silver for {len(_draftable(made))} of {len(sources)} sources")
    return {"sources": len(sources), "drafted": len(_draftable(made))}
