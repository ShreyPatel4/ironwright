"""The bridge routes: the run board, the approval cards, a silver run, and the decision.

They sit under /workspaces/{name}/..., so the app gate scopes every one to the caller's
org before a handler runs. The store lives in the workspace folder, which only this org
can name. A card or run id from another org answers what an id nobody has answers.

The decision takes the approver from the session, never from the body. The gate then
refuses an agent, the requester and a stale hash on its own, so a dev open server with
no sessions still cannot approve its own change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import auth
from .. import workspaces_tenant as wt
from ..config import private_dir
from ..ingest import silver_draft as sd
from ..legacy import IngestError, read_upload
from ..packs import list_packs
from ..tenancy.models import Ctx
from . import gate, supervisor

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(auth.ctx)]
NO_CARD = "no such card"
NOT_HERE = "the bridge runs on a workspace built from your own uploads"


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_rows: int = Field(default=gate.SAMPLE_ROWS, ge=1, le=5000)


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    content_hash: str = Field(max_length=64)
    reason: str = Field(default="", max_length=500)


def _folder(c: Ctx, name: str) -> Path | None:
    """The folder of one of this org's uploaded workspaces, or None. The gate already
    answered for every name this org cannot see, so None here is a demo pack or a
    generated organization, which have no bridge yet."""
    if name in list_packs():
        return None
    try:
        return wt._existing(c.org_id, name)
    except FileNotFoundError:
        return None


def _store(c: Ctx, name: str) -> gate.BridgeStore | None:
    d = _folder(c, name)
    if d is None:
        return None
    if not (d / "bridge").exists():
        return None
    return gate.BridgeStore(d / "bridge")


def _writer(d: Path):
    def write(layer: str, files: dict[str, str]) -> None:
        wt._models({layer: files})              # the same checks a workspace create runs
        folder = private_dir(d / "models" / layer)
        for name, sql in files.items():
            wt._replace(folder / name, sql)
    return write


def _supervisor(c: Ctx, name: str, store: gate.BridgeStore, sample_rows: int) -> supervisor.Supervisor:
    d = _folder(c, name)
    if d is None:
        raise HTTPException(422, NOT_HERE)
    pack = wt.load_workspace(c.org_id, name)
    saved = sd.read_saved(c.org_id, name)
    if saved is None:
        raise HTTPException(422, "this workspace has no silver draft yet: queue a silver_draft job first")
    spec = wt._normalize(c.org_id, wt._spec_of(d), wt._read_models(d))
    try:
        agent = supervisor.SilverBuilder(saved["draft"], spec, saved["declined"], saved["renames"])
        bronze = {s["name"]: read_upload(s) for s in spec["sources"]}
    except (sd.DraftError, IngestError) as e:
        raise HTTPException(422, str(e)) from e

    def live() -> dict[str, str]:
        folder = d / "models" / "silver"
        return {p.name: p.read_text() for p in sorted(folder.glob("*.sql"))} if folder.is_dir() else {}

    return supervisor.Supervisor(store, agent, bronze, policy=pack.policies, writer=_writer(d),
                                 live_models=live, sample_rows=sample_rows,
                                 source={"kind": "silver_draft", "workspace": name})


@router.get("/workspaces/{name}/bridge/board")
def get_board(name: str, c: CtxDep) -> dict[str, Any]:
    return supervisor.board(_store(c, name))


@router.get("/workspaces/{name}/bridge/cards")
def get_cards(name: str, c: CtxDep) -> list[dict[str, Any]]:
    store = _store(c, name)
    if store is None:
        return []
    return [{k: v for k, v in card.items() if k not in ("samples", "diff", "staged")} for card in store.cards()]


@router.get("/workspaces/{name}/bridge/cards/{card_id}")
def get_card(name: str, card_id: str, c: CtxDep) -> dict[str, Any]:
    store = _store(c, name)
    try:
        if store is None:
            raise gate.GateError("no such card")
        return store.card(card_id)
    except gate.GateError as e:
        raise HTTPException(404, NO_CARD) from e


@router.post("/workspaces/{name}/bridge/runs", status_code=201)
def post_run(name: str, body: RunBody, c: CtxDep) -> dict[str, Any]:
    d = _folder(c, name)
    if d is None:
        raise HTTPException(422, NOT_HERE)
    store = gate.BridgeStore(d / "bridge")
    sup = _supervisor(c, name, store, body.sample_rows)
    try:
        return sup.start(requested_by=c.principal)
    except gate.GateError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/workspaces/{name}/bridge/cards/{card_id}/decide")
def post_decide(name: str, card_id: str, body: DecideBody, c: CtxDep) -> dict[str, Any]:
    store = _store(c, name)
    try:
        if store is None:
            raise gate.GateError("no such card")
        card = store.card(card_id)
    except gate.GateError as e:
        raise HTTPException(404, NO_CARD) from e
    try:
        rec = gate.decide(store, card_id, by=c.principal, role=c.role, decision=body.decision,
                          content_hash=body.content_hash, reason=body.reason)
    except gate.GateError as e:
        raise HTTPException(422, str(e)) from e
    run = store.run(card["run_id"])
    sup = _supervisor(c, name, store, run["sample_rows"])
    return {"decision": rec, "run": sup.resume(card["run_id"])}
