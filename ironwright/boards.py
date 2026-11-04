"""Boards, the built-in BI. Sketch 03, live.

A board is tiles over the lakehouse: each tile carries its SQL, its viz
shape and its last run time. Definitions are user data under the
workspace's data root, never in the repo. Every tile goes through the
same SELECT-only guard as the Explorer, rejected at save, not at run;
stale tiles say when they ran instead of posing as current.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import sqlguard
from .config import UnsafePath, private_dir, safe_child, write_private
from .lake import Lakehouse
from .packs import Pack

_SLUG = re.compile(r"[a-z][a-z0-9-]{1,39}")
VIZ = {"table", "number", "bar", "line"}


class BoardError(sqlguard.SqlRejected):
    pass


def _dir(pack: Pack) -> Path:
    return safe_child(pack.paths.root, "boards")


def _file(pack: Pack, slug: str) -> Path:
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise UnsafePath(f"unsafe board slug: {slug!r}")
    return safe_child(_dir(pack), f"{slug}.json")


def _existing(pack: Pack, slug: str) -> Path:
    """The board file for a read or delete; a refused slug looks like a missing board."""
    try:
        f = _file(pack, slug)
    except UnsafePath as e:
        raise FileNotFoundError(slug) from e
    if not f.exists():
        raise FileNotFoundError(slug)
    return f


def guard_sql(sql: str) -> str:
    try:
        return sqlguard.guard_select(sql)
    except sqlguard.SqlRejected as e:
        raise BoardError(f"every tile must be a single SELECT: {e}") from e


def validate(board: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(board.get("title"), str) or not board["title"].strip():
        raise BoardError("a board needs a title")
    tiles = board.get("tiles")
    if not isinstance(tiles, list) or not tiles:
        raise BoardError("a board needs at least one tile")
    clean = []
    for i, t in enumerate(tiles):
        if t.get("viz", "table") not in VIZ:
            raise BoardError(f"tile {i}: viz must be one of {sorted(VIZ)}")
        clean.append({"id": t.get("id") or f"t{i+1}",
                      "title": str(t.get("title") or f"Tile {i+1}"),
                      "sql": guard_sql(str(t.get("sql", ""))),
                      "viz": t.get("viz", "table")})
    return {"title": board["title"].strip(), "tiles": clean}


def list_boards(pack: Pack) -> list[dict[str, Any]]:
    try:
        d = _dir(pack)
    except UnsafePath:
        return []
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        if f.is_symlink():
            continue
        b = json.loads(f.read_text())
        out.append({"slug": f.stem, "title": b["title"], "tiles": len(b["tiles"])})
    return out


def read(pack: Pack, slug: str) -> dict[str, Any]:
    return {"slug": slug, **json.loads(_existing(pack, slug).read_text())}


def write(pack: Pack, slug: str, board: dict[str, Any]) -> dict[str, Any]:
    try:
        f = _file(pack, slug)
    except UnsafePath as e:
        raise BoardError("slug: lowercase letter first, then letters, digits or dashes") from e
    clean = validate(board)
    private_dir(f.parent)
    write_private(f, json.dumps(clean, indent=1, sort_keys=True))
    return {"slug": slug, **clean}


def delete(pack: Pack, slug: str) -> None:
    _existing(pack, slug).unlink()


def run(pack: Pack, slug: str, row_cap: int = 200) -> dict[str, Any]:
    board = read(pack, slug)
    con = Lakehouse(pack).connect(capped=True)
    results = []
    try:
        for tile in board["tiles"]:
            t0 = time.perf_counter()
            try:
                # Re-guard at run time: the file on disk is writable by the
                # local user, so saved SQL is not trusted just for being saved.
                # The connection itself also has external access disabled.
                cap = 1 if tile["viz"] == "number" else 50 if tile["viz"] in ("bar", "line") else row_cap
                cols, rows = sqlguard.run_capped(con, guard_sql(tile["sql"]), cap)
                results.append({"id": tile["id"], "ok": True, "columns": cols, "rows": rows,
                                "seconds": round(time.perf_counter() - t0, 3)})
            except Exception as e:  # a broken tile reports; the board still runs
                results.append({"id": tile["id"], "ok": False, "error": sqlguard.safe_error(e),
                                "seconds": round(time.perf_counter() - t0, 3)})
    finally:
        con.close()
    return {"slug": slug, "ran_at": round(time.time(), 3), "tiles": results}
