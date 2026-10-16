"""As-of reads: an object resolved to its state at a past time (sketch 08, sem_asof).

Object-level only. object_history already versions every object, so this
slice answers "what did this object look like at time t" straight from the
store. Function-level as-of is OUT of this slice: the sketch's full gate
needs lake snapshots (Iceberg time travel) before a function evaluated as-of
a past run can honestly match that run's report, and this module does not
pretend to have that.

The router mounts next to the other platform reads:

    from ironwright.asof import router as asof_router
    app.include_router(asof_router)
"""
from __future__ import annotations

import contextlib
import datetime
from typing import Any, Iterator

from fastapi import APIRouter, Header, HTTPException

from .ontology.runtime import Ontology
from .packs import list_packs, load_pack
from .tracing import Tracer

CONSOLE_PRINCIPAL = "user:console"

router = APIRouter()


def parse_ts(raw: str) -> float:
    """A unix timestamp (seconds) or an ISO 8601 datetime. A naive ISO value
    is read as UTC, because every stored ts is epoch seconds."""
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, "ts must be a unix timestamp in seconds or an ISO 8601 datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _pack(name: str):
    if name not in list_packs():
        raise HTTPException(404, f"no workspace named {name}")
    return load_pack(name)


@contextlib.contextmanager
def _ontology(pack) -> Iterator[Ontology]:
    ont = Ontology(pack, Tracer(pack.paths.ensure().traces), attach_lake=False)
    try:
        yield ont
    finally:
        with contextlib.suppress(Exception):
            ont.store.con.close()


@router.get("/workspaces/{name}/objects/{type_}/{id_}/asof")
def object_asof(name: str, type_: str, id_: str, ts: str,
                x_principal: str = Header(CONSOLE_PRINCIPAL)) -> dict[str, Any]:
    """The object as of ts, masked like every other object read, plus which
    version answered and when that version was written."""
    at = parse_ts(ts)
    with _ontology(_pack(name)) as ont:
        obj = ont.store.get_asof(type_, id_, at)
        if obj is None:
            raise HTTPException(404, f"no {type_} {id_} as of {ts}; it did not exist yet or never did")
        version = obj.get("_version")
        version_ts = obj.pop("_asof_ts", None)
        return {"as_of": at, "version": version, "version_ts": version_ts,
                "object": ont.masked(obj, x_principal), "_as": x_principal}
