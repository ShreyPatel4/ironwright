"""Engine swap rung (lake_engines): the same model SQL on DuckDB and on postgres.

The map node claims transforms are SQL that can move engines without
rewriting models. This module proves the portable half on a second real
engine. It loads a pack's bronze tables (latest run only, the exact frames
the DuckDB bronze_<name> views show) into a postgres schema under the same
bronze_<name> names, runs every silver then gold model file UNCHANGED on
postgres, reads each result back as Arrow, and compares it to the DuckDB
result of the same model on the same bronze frames.

What "equal" means here, exactly:

- column names compare lowercased, in select order. Postgres folds unquoted
  identifiers to lower case and DuckDB matches case insensitively, so lower
  case is the one spelling both engines resolve the same way. Bronze columns
  are lowercased on load for the same reason; two columns differing only in
  case would be an error.
- integer widths are unified: every integer type compares as a Python int.
- DuckDB DOUBLE and postgres numeric compare as floats with exact equality
  expected, because both engines compute from identical inputs. ADBC hands
  an unparameterized numeric back as text; when the other engine typed the
  column numeric, that text is read as a float. NaN compares equal to NaN.
- dates stay dates; timestamps normalize to UTC.
- rows compare after a canonical sort on all columns.

A model whose SQL does not run on postgres is reported honestly: match is
false and the error says why. Its DuckDB result is then loaded into postgres
under the model's name so downstream models still run and still compare,
instead of one failure cascading into noise.

The APIRouter here is not mounted anywhere; the integrator mounts it:

    from ironwright.engines import router as engines_router
    app.include_router(engines_router)
"""
from __future__ import annotations

import math
import os
import re
import uuid
from datetime import timezone
from typing import Any

import duckdb
import pyarrow as pa
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .lake import Lakehouse
from .packs import Pack, list_packs, load_pack

ENGINES = ("duckdb", "postgresql")

router = APIRouter()

_PREFIX = re.compile(r"^[a-z][a-z0-9_]{0,24}$")


# ---------- the frames both engines see ----------

def bronze_frames(pack: Pack) -> dict[str, pa.Table]:
    """The pack's bronze tables, latest run only, column names lowercased.

    Read through the same views lake.connect() gives the models, so this is
    exactly what DuckDB transforms see."""
    lake = Lakehouse(pack)
    con = lake.connect()
    try:
        frames: dict[str, pa.Table] = {}
        for ident in lake.tables():
            ns, name = ident.split(".")
            if ns != "bronze":
                continue
            frame = con.execute(f"select * from bronze_{name}").fetch_arrow_table()
            lowered = [c.lower() for c in frame.column_names]
            if len(set(lowered)) != len(lowered):
                raise ValueError(f"bronze.{name} has columns that differ only in case; "
                                 f"postgres would fold them onto each other")
            frames[name] = frame.rename_columns(lowered)
        return frames
    finally:
        con.close()


def run_models_duckdb(pack: Pack, frames: dict[str, pa.Table]) -> list[dict[str, Any]]:
    """Every silver then gold model on DuckDB over exactly these bronze frames.

    Returns [{layer, name, sql, data}] in run order. Each result is exposed
    as a view so later models can read it, the same way migrate() does."""
    con = duckdb.connect()
    try:
        for name, frame in frames.items():
            con.register(f"_bronze_{name}", frame)
            con.execute(f"create view bronze_{name} as select * from _bronze_{name}")
        out: list[dict[str, Any]] = []
        for layer in ("silver", "gold"):
            for name, sql in pack.models(layer):
                data = con.execute(sql).fetch_arrow_table()
                con.register(f"_{layer}_{name}", data)
                con.execute(f"create or replace view {layer}_{name} as select * from _{layer}_{name}")
                out.append({"layer": layer, "name": name, "sql": sql, "data": data})
        return out
    finally:
        con.close()


def load_bronze_to_postgres(frames: dict[str, pa.Table], uri: str, schema: str) -> None:
    """Ingest the bronze frames into <schema>.bronze_<name> over ADBC."""
    from adbc_driver_postgresql import dbapi

    with dbapi.connect(uri) as con:
        with con.cursor() as cur:
            for name, frame in frames.items():
                cur.adbc_ingest(f"bronze_{name}", frame, mode="create", db_schema_name=schema)
        con.commit()
        # fresh tables have no statistics and the planner picks disastrous
        # plans without them; analyze keeps the check seconds, not minutes
        with con.cursor() as cur:
            for name in frames:
                cur.execute(f"analyze {schema}.bronze_{name}")
        con.commit()


# ---------- canonical comparison ----------

def _kind(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_integer(t):
        return "int"
    if pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "float"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "ts"
    return "str"


def _common_kind(a: pa.DataType, b: pa.DataType) -> str:
    ka, kb = _kind(a), _kind(b)
    if ka == kb:
        return ka
    kinds = {ka, kb}
    # one engine typed it numeric: unify there. Postgres numeric can arrive
    # as text through ADBC; integer widths and int vs float also land here.
    if kinds & {"float", "int"}:
        return "float"
    return "str"


def _norm(v: Any, kind: str) -> Any:
    if v is None:
        return None
    if kind == "int":
        return int(v)
    if kind == "float":
        f = float(v)
        return "NaN" if math.isnan(f) else f
    if kind == "bool":
        return bool(v)
    if kind == "ts":
        return v.astimezone(timezone.utc) if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if kind == "date":
        return v
    return str(v)


def _canonical(table: pa.Table, kinds: list[str]) -> list[tuple]:
    cols = [[_norm(v, k) for v in col.to_pylist()] for col, k in zip(table.columns, kinds)]
    rows = list(zip(*cols)) if cols else []
    return sorted(rows, key=lambda r: tuple((v is None, str(v)) for v in r))


def compare_frames(duck: pa.Table, other: pa.Table) -> tuple[bool, str | None]:
    """Same columns, same row count, same values after the documented
    normalization and a canonical sort. Returns (match, why_not)."""
    a_cols = [c.lower() for c in duck.column_names]
    b_cols = [c.lower() for c in other.column_names]
    if a_cols != b_cols:
        return False, f"columns differ: duckdb {a_cols} vs postgresql {b_cols}"
    if duck.num_rows != other.num_rows:
        return False, f"row counts differ: duckdb {duck.num_rows} vs postgresql {other.num_rows}"
    kinds = [_common_kind(a.type, b.type) for a, b in zip(duck.schema, other.schema)]
    try:
        a_rows = _canonical(duck, kinds)
        b_rows = _canonical(other, kinds)
    except (ValueError, TypeError) as e:
        return False, f"values do not normalize to one type: {e}"
    for i, (ra, rb) in enumerate(zip(a_rows, b_rows)):
        if ra != rb:
            return False, f"first differing row after sort (#{i}): duckdb {ra} vs postgresql {rb}"
    return True, None


# ---------- the cross-engine run ----------

def compare_models(pack: Pack, frames: dict[str, pa.Table], uri: str, schema: str) -> list[dict[str, Any]]:
    """Run every model unchanged on postgres in <schema> (bronze must already
    be loaded there) and compare each result to DuckDB's. Returns the
    per-model report entries."""
    import psycopg
    from adbc_driver_postgresql import dbapi

    duck_runs = run_models_duckdb(pack, frames)
    report: list[dict[str, Any]] = []
    with psycopg.connect(uri, autocommit=True) as pc, dbapi.connect(uri) as ac:
        pc.execute(f"set search_path to {schema}")
        for run in duck_runs:
            table = f"{run['layer']}_{run['name']}"
            want: pa.Table = run["data"]
            error: str | None = None
            match = False
            try:
                pc.execute(f"create table {table} as {run['sql']}")
                pc.execute(f"analyze {table}")
            except psycopg.Error as e:
                error = f"postgres cannot run this model unchanged: {type(e).__name__}: {e}".strip()
                # keep downstream models comparable: stand in the DuckDB result
                with ac.cursor() as cur:
                    cur.adbc_ingest(table, want, mode="create", db_schema_name=schema)
                ac.commit()
            if error is None:
                with ac.cursor() as cur:
                    cur.execute(f'select * from {schema}.{table}')
                    got = cur.fetch_arrow_table()
                match, why = compare_frames(want, got)
                error = why
            report.append({"model": f"{run['layer']}.{run['name']}",
                           "rows": want.num_rows,
                           "columns": [c.lower() for c in want.column_names],
                           "match": match,
                           "error": error})
    return report


def run_on_postgres(pack: Pack, uri: str, schema: str) -> dict[str, Any]:
    """The whole rung for one pack: load bronze, run every model on both
    engines, compare. The schema must exist; the caller owns its lifecycle."""
    frames = bronze_frames(pack)
    load_bronze_to_postgres(frames, uri, schema)
    models = compare_models(pack, frames, uri, schema)
    return {"engines": list(ENGINES), "models": models,
            "match": all(m["match"] for m in models)}


# ---------- REST ----------

class CheckBody(BaseModel):
    schema_prefix: str | None = None


@router.post("/workspaces/{name}/engines/check")
def engines_check(name: str, body: CheckBody | None = None) -> dict[str, Any]:
    if name not in list_packs():
        from .workspaces import list_organizations

        if any(o["name"] == name for o in list_organizations()):
            raise HTTPException(404, f"{name} is a generated organization and has no lake yet; "
                                     f"the engine check runs on built pack workspaces")
        raise HTTPException(404, f"no workspace named {name}")
    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        raise HTTPException(503, "POSTGRES_URI is not set. I need a reachable postgres server "
                                 "to run the cross engine check and I will not fake one.")
    pack = load_pack(name)
    if not (pack.paths.lake / "catalog.db").exists():
        raise HTTPException(404, f"{name} has no lake yet; run the pipeline first")
    prefix = body.schema_prefix if body and body.schema_prefix else "iw_engines"
    if not _PREFIX.match(prefix):
        raise HTTPException(422, "schema_prefix must be a short lowercase slug: "
                                 "a letter first, then letters, digits or _")

    import psycopg

    schema = f"{prefix}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(uri, connect_timeout=5, autocommit=True) as pc:
            pc.execute(f"create schema {schema}")
    except psycopg.Error as e:
        raise HTTPException(503, f"postgres at POSTGRES_URI is not reachable: {e}")
    try:
        report = run_on_postgres(pack, uri, schema)
    finally:
        # the schema is throwaway; drop it even when the run blew up
        with psycopg.connect(uri, connect_timeout=5, autocommit=True) as pc:
            pc.execute(f"drop schema if exists {schema} cascade")
    report["workspace"] = name
    report["schema"] = schema
    report["schema_dropped"] = True
    return report
