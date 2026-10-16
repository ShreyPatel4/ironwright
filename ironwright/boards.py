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

from .lake import Lakehouse
from .packs import Pack

_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
VIZ = {"table", "number", "bar", "line"}


class BoardError(ValueError):
    pass


def _dir(pack: Pack) -> Path:
    return pack.paths.root / "boards"


def guard_sql(sql: str) -> str:
    body = sql.strip().rstrip(";").strip()
    if ";" in body or not _SELECT_ONLY.match(body):
        raise BoardError("every tile must be a single SELECT; rejected at save, not at run")
    return body


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
    d = _dir(pack)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        b = json.loads(f.read_text())
        out.append({"slug": f.stem, "title": b["title"], "tiles": len(b["tiles"])})
    return out


def read(pack: Pack, slug: str) -> dict[str, Any]:
    f = _dir(pack) / f"{slug}.json"
    if not f.exists():
        raise FileNotFoundError(slug)
    return {"slug": slug, **json.loads(f.read_text())}


def write(pack: Pack, slug: str, board: dict[str, Any]) -> dict[str, Any]:
    if not _SLUG.match(slug):
        raise BoardError("slug: lowercase letter first, then letters, digits or dashes")
    clean = validate(board)
    d = _dir(pack)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(json.dumps(clean, indent=1, sort_keys=True))
    return {"slug": slug, **clean}


def delete(pack: Pack, slug: str) -> None:
    f = _dir(pack) / f"{slug}.json"
    if not f.exists():
        raise FileNotFoundError(slug)
    f.unlink()


def run(pack: Pack, slug: str, row_cap: int = 200) -> dict[str, Any]:
    board = read(pack, slug)
    con = Lakehouse(pack).connect()
    results = []
    try:
        for tile in board["tiles"]:
            t0 = time.perf_counter()
            try:
                cur = con.execute(tile["sql"])
                cols = [d[0] for d in cur.description]
                cap = 1 if tile["viz"] == "number" else 50 if tile["viz"] in ("bar", "line") else row_cap
                rows = [[None if v is None else str(v) for v in r] for r in cur.fetchmany(cap)]
                results.append({"id": tile["id"], "ok": True, "columns": cols, "rows": rows,
                                "seconds": round(time.perf_counter() - t0, 3)})
            except Exception as e:  # a broken tile reports; the board still runs
                results.append({"id": tile["id"], "ok": False, "error": str(e),
                                "seconds": round(time.perf_counter() - t0, 3)})
    finally:
        con.close()
    return {"slug": slug, "ran_at": round(time.time(), 3), "tiles": results}
