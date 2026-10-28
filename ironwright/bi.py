"""BI embed, the honest slice: a postgres mirror plus a framed URL.

External BI tools (Looker, Power BI, Metabase) speak SQL to a warehouse,
not to a local Iceberg catalog. mirror_gold() copies every silver and
gold table from the pack's lakehouse into a postgres schema, one table
per lake table named <layer>_<name>, then reads each copy back and
verifies it row for row. That schema is what a BI tool points at.

The embed side stores one http(s) URL per workspace in bi.json under the
workspace's data directory (the boards.py pattern, never inside
ironwright/packs/). The console renders it in a sandboxed iframe. We
claim the mirror and the frame; the dashboard inside is the user's own
BI tool, not a Looker or Power BI integration.

Round-trip verification compares canonical forms, never bytes, because a
postgres round trip legitimately changes physical types. The documented
normalization, applied identically to both sides before a canonical row
sort: None stays None, booleans become 'true'/'false', floats use repr
(exact for a float8 round trip), Decimals drop trailing zeros, tz-aware
datetimes convert to UTC, naive datetimes and dates use ISO format,
bytes become hex, everything else str(). A table matches only when the
column names, the row count and every normalized row agree.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pyarrow as pa
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import data_root
from .lake import Lakehouse
from .packs import Pack, list_packs, load_pack

SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
MIRROR_LAYERS = ("silver", "gold")
MAX_URL = 2048
BI_FILE = "bi.json"


class BIError(ValueError):
    pass


# ----------------------------------------------------------------- mirror

def _norm(v: Any) -> str | None:
    """One value's canonical form. See the module docstring; this is the
    whole normalization, there is no hidden tolerance."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc)
        return v.isoformat()
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    return str(v)


def _canonical(data: pa.Table) -> tuple[list[str], list[tuple]]:
    """(column names, rows) with every value normalized and the rows in a
    canonical sort, so physical row order never decides a match."""
    cols = list(data.column_names)
    rows = [tuple(_norm(r[c]) for c in cols) for r in data.to_pylist()]
    rows.sort(key=lambda r: tuple((v is None, v or "") for v in r))
    return cols, rows


def _check_schema(schema: str) -> str:
    if not SCHEMA_RE.match(schema or ""):
        raise BIError("schema must match ^[a-z][a-z0-9_]{0,40}$: a lowercase "
                      "letter first, then lowercase letters, digits or underscores")
    return schema


def _mirror_idents(lake: Lakehouse) -> list[str]:
    return sorted(i for i in lake.tables() if i.split(".")[0] in MIRROR_LAYERS)


def _read_back(con, schema: str, target: str) -> pa.Table:
    with con.cursor() as cur:
        cur.execute(f'select * from "{schema}"."{target}"')
        return cur.fetch_arrow_table()


def mirror_gold(pack: Pack, uri: str, schema: str) -> dict[str, Any]:
    """Copy every silver and gold table into postgres under schema, then
    read each copy back and verify it. The report never asserts more than
    the read-back showed: {table, rows, match} per table, where match is
    the canonical comparison's verdict, not the write's."""
    import adbc_driver_postgresql.dbapi as pg

    _check_schema(schema)
    lake = Lakehouse(pack)
    idents = _mirror_idents(lake)
    if not idents:
        raise BIError(f"{pack.name} has no silver or gold tables; "
                      "run the pipeline before mirroring")
    tables = []
    with pg.connect(uri) as con:
        with con.cursor() as cur:
            cur.execute(f'create schema if not exists "{schema}"')
        con.commit()
        for ident in idents:
            layer, name = ident.split(".")
            target = f"{layer}_{name}"
            data = lake.read(ident)
            with con.cursor() as cur:
                cur.execute(f'drop table if exists "{schema}"."{target}"')
                cur.adbc_ingest(target, data, mode="create", db_schema_name=schema)
            con.commit()
            back = _read_back(con, schema, target)
            cols_lake, rows_lake = _canonical(data)
            cols_pg, rows_pg = _canonical(back)
            tables.append({"table": target, "rows": data.num_rows,
                           "match": cols_lake == cols_pg and rows_lake == rows_pg})
    return {"workspace": pack.name, "schema": schema, "tables": tables,
            "matched": all(t["match"] for t in tables),
            "mirrored_at": round(time.time(), 3)}


def verify_mirror(pack: Pack, uri: str, schema: str) -> dict[str, Any]:
    """Re-run only the comparison against an existing mirror: read each
    silver and gold table's postgres copy back and compare it to the lake.
    This is the same verdict mirror_gold reports, without rewriting, so a
    drifted or corrupted copy shows up as match false."""
    import adbc_driver_postgresql.dbapi as pg

    _check_schema(schema)
    lake = Lakehouse(pack)
    idents = _mirror_idents(lake)
    tables = []
    with pg.connect(uri) as con:
        for ident in idents:
            layer, name = ident.split(".")
            target = f"{layer}_{name}"
            data = lake.read(ident)
            try:
                back = _read_back(con, schema, target)
            except Exception:
                tables.append({"table": target, "rows": data.num_rows, "match": False})
                con.rollback()
                continue
            cols_lake, rows_lake = _canonical(data)
            cols_pg, rows_pg = _canonical(back)
            tables.append({"table": target, "rows": data.num_rows,
                           "match": cols_lake == cols_pg and rows_lake == rows_pg})
    return {"workspace": pack.name, "schema": schema, "tables": tables,
            "matched": all(t["match"] for t in tables)}


# ------------------------------------------------------------------ embed

def _file(pack: Pack) -> Path:
    return pack.paths.root / BI_FILE


def check_url(url: Any) -> str:
    """Only an absolute http(s) URL passes, length-capped. javascript:,
    data:, file: and relative paths are rejected loudly: the console puts
    this URL straight into an iframe src."""
    if not isinstance(url, str) or not url.strip():
        raise BIError("the embed URL must be a non-empty string")
    url = url.strip()
    if len(url) > MAX_URL:
        raise BIError(f"embed URL is longer than {MAX_URL} characters; "
                      "that is not a dashboard link")
    if any(c in url for c in " \t\n\r\"'<>"):
        raise BIError("embed URL contains whitespace or quote characters; rejected")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise BIError(f"embed URL must be absolute http(s); got scheme "
                      f"{parts.scheme or 'none'!r}. javascript:, data: and file: "
                      "URLs never reach the frame")
    return url


def get_embed(pack: Pack) -> dict[str, Any]:
    f = _file(pack)
    if not f.exists():
        return {"url": None}
    return json.loads(f.read_text())


def set_embed(pack: Pack, url: str) -> dict[str, Any]:
    clean = check_url(url)
    cfg = {"url": clean, "saved_at": round(time.time(), 3)}
    pack.paths.root.mkdir(parents=True, exist_ok=True)
    _file(pack).write_text(json.dumps(cfg, indent=1, sort_keys=True))
    return cfg


# ----------------------------------------------------------------- router

router = APIRouter()


class MirrorBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_name: str | None = Field(default=None, alias="schema")


class EmbedBody(BaseModel):
    url: str


def _pack_or_404(name: str) -> Pack:
    if name in list_packs():
        return load_pack(name)
    if (data_root() / name / "org.json").exists():
        raise HTTPException(404, f"{name} is a created organization and has no "
                                 "lakehouse; the BI mirror reads silver and gold "
                                 "tables, which exist only after a pack migration")
    raise HTTPException(404, f"no workspace named {name}")


@router.post("/workspaces/{name}/bi/mirror")
def bi_mirror(name: str, body: MirrorBody | None = None) -> dict[str, Any]:
    pack = _pack_or_404(name)
    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        raise HTTPException(503, "POSTGRES_URI is not set. The mirror copies "
                                 "silver and gold tables into a real postgres "
                                 "server; without one there is nothing to point "
                                 "a BI tool at. deploy/bi has a recipe.")
    schema = (body.schema_name if body else None) or f"iw_bi_{uuid.uuid4().hex[:8]}"
    try:
        return mirror_gold(pack, uri, schema)
    except BIError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        # the server's own words, not a bare 500; the URI never echoes back
        raise HTTPException(502, f"mirror failed against the postgres server: {e}")


@router.get("/workspaces/{name}/bi")
def bi_get(name: str) -> dict[str, Any]:
    pack = _pack_or_404(name)
    return {"workspace": name, **get_embed(pack)}


@router.put("/workspaces/{name}/bi")
def bi_put(name: str, body: EmbedBody) -> dict[str, Any]:
    pack = _pack_or_404(name)
    try:
        return {"workspace": name, **set_embed(pack, body.url)}
    except BIError as e:
        raise HTTPException(422, str(e))
