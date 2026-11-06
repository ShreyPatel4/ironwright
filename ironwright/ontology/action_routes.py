"""Authoring routes for a tenant's governed actions. The app gate scopes {name}.

The app includes this router before POST /workspaces/{name}/actions/{action}, or that
route would take "propose" and "accept" as action names. Both are reserved for that
reason. Requesting, approving and denying go through the platform routes and act().
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from .. import workspaces_tenant as wt
from ..auth import ctx
from ..packs import list_packs
from ..tenancy import Ctx
from . import actions_propose as ap
from .propose import StoreBusy

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(ctx)]

NOT_TENANT = "actions are authored on your own workspaces; a demo pack ships its own"
NO_ONTOLOGY = "accept an ontology first: an action acts on an accepted object type"


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str
    object_type: str
    request_role: str = "member"
    requires_approval: bool = True
    values: list[str] | None = None
    allowed_statuses: list[str] | None = None
    name: str | None = None


class AcceptBody(DraftBody):
    draft_id: str


def _pack(name: str, c: Ctx):
    if name in list_packs():
        raise HTTPException(404, NOT_TENANT)
    try:
        return wt.load_workspace(c.org_id, name)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e


def _choices(body: DraftBody) -> dict[str, Any]:
    return body.model_dump(exclude={"draft_id"}, exclude_none=True)


@router.get("/workspaces/{name}/actions")
def list_actions(name: str, c: CtxDep) -> dict[str, Any]:
    pack = _pack(name, c)
    types = sorted(pack.ontology.get("object_types") or {})
    props = {t: sorted(ap._properties(pack.ontology, t)) for t in types}
    return {"templates": ap.catalog(), "object_types": [{"name": t, "properties": props[t]} for t in types],
            "accepted": [{k: d[k] for k in ("name", "template", "object_type", "description", "requires_approval",
                                            "request_roles", "policy_rule", "plain", "params")}
                         for d in ap.accepted(pack)]}


@router.post("/workspaces/{name}/actions/propose")
def propose(name: str, body: DraftBody, c: CtxDep) -> dict[str, Any]:
    pack = _pack(name, c)
    if not pack.ontology.get("object_types"):
        raise HTTPException(409, NO_ONTOLOGY)
    try:
        return ap.draft(pack.ontology, _choices(body))
    except ap.DraftError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/workspaces/{name}/actions/accept")
def accept(name: str, body: AcceptBody, c: CtxDep) -> dict[str, Any]:
    _pack(name, c)
    try:
        return ap.accept(c.org_id, name, _choices(body), body.draft_id, c.principal)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except ap.StaleDraft as e:
        raise HTTPException(409, "these choices do not make that draft; propose again and read it") from e
    except StoreBusy as e:
        raise HTTPException(409, "a run is using this workspace; try again when it is done") from e
    except (ap.DraftError, wt.WorkspaceError) as e:
        raise HTTPException(422, str(e)) from e
