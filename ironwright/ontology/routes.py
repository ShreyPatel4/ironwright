"""The ontology review routes. POST /jobs {kind: ontology_propose} makes the proposal.

The path param is {name} on purpose: the app gate scopes every template that starts
/workspaces/{name}, so another org gets the same 404 it gets for nothing at all. The
org and the principal come from ctx(), never from the request.

Past the gate an org may learn its own state, so "no proposal yet" reads differently
from the gate's answer.

Accept queues a run, which builds the objects. A client must not be able to pull that
work into the hosted API process, so it goes to the worker like every other run.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from .. import workspaces_tenant as wt
from ..auth import ctx
from ..jobs.store import JobStore
from ..tenancy import Ctx, quotas
from . import propose as proposer

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(ctx)]

NOT_TENANT = "this is not one of your workspaces, so it has no proposal"
NO_PROPOSAL = "no proposal yet: queue an ontology_propose job after a run"


class AcceptBody(BaseModel):
    proposal_id: str
    edits: list[Any] = []


@router.get("/workspaces/{name}/ontology/proposal")
def get_proposal(name: str, c: CtxDep) -> dict[str, Any]:
    try:
        found = proposer.read_proposal(c.org_id, name)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e
    if found is None:
        raise HTTPException(404, NO_PROPOSAL)
    return found


@router.post("/workspaces/{name}/ontology/accept")
def accept(name: str, body: AcceptBody, c: CtxDep) -> dict[str, Any]:
    quotas.check(c.org_id, "runs", auth._now())
    try:
        done = proposer.accept(c.org_id, name, body.proposal_id, body.edits, c.principal)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except proposer.StaleProposal as e:
        raise HTTPException(409, "that is not the current proposal; read it again") from e
    except proposer.StoreBusy as e:
        raise HTTPException(409, "a run is using this workspace; try again when it is done") from e
    except (proposer.BadEdit, wt.WorkspaceError) as e:
        raise HTTPException(422, str(e)) from e
    quotas.spend(c.org_id, "runs", auth._now())
    return {**done, "job_id": JobStore().enqueue(c.org_id, name, "run", c.principal, {})}
