"""The jobs API (contract 4) and the enqueue behind POST /workspaces/{name}/runs.

Every lookup is scoped to the caller's org in the store, so another org's job id
reads as 404 on every route. The org and principal come from ctx(), never from
the request body.
"""
from __future__ import annotations

import json
import os
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from .. import auth
from .. import workspaces_tenant as wt
from ..auth import ctx
from ..config import SAFE_NAME
from ..packs import list_packs
from ..tenancy import DEV_ORG_ID, Ctx, quotas
from ..tenancy import service as tenancy
from . import handlers
from .store import TERMINAL, JobStore
from .worker import Worker

# The same bytes the app gate answers, so a job id reads like everything else
# a tenant cannot see. tests/test_isolation_routes.py compares them.
NOT_FOUND = "no such resource"
MAX_PARAMS_BYTES = 64 * 1024
RUN_WAIT_SECONDS = 600.0

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(ctx)]


class JobBody(BaseModel):
    kind: str
    workspace: str
    params: dict[str, Any] = {}


def _public(job: dict[str, Any]) -> dict[str, Any]:
    """log_path is a server detail. The tail route is how a client reads it."""
    return {k: v for k, v in job.items() if k != "log_path"}


def _validate(kind: str, workspace: str, params: dict[str, Any]) -> None:
    if kind not in handlers.HANDLERS:
        raise HTTPException(422, f"unknown job kind {kind!r}")
    if not SAFE_NAME.fullmatch(workspace):
        raise HTTPException(422, "workspace is not a safe name")
    if len(json.dumps(params, default=str)) > MAX_PARAMS_BYTES:
        raise HTTPException(422, "params are too large")
    try:
        handlers.validate_params(kind, params)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _check_workspace(c: Ctx, workspace: str) -> None:
    """The app gate cannot see a body, so this asks the same two questions it would."""
    if not tenancy.can_see_workspace(c, workspace):
        raise HTTPException(404, NOT_FOUND)
    if c.org_id != DEV_ORG_ID and workspace in list_packs():
        raise HTTPException(403, "demo packs are read only; work in your own workspace")


@router.post("/jobs")
def create_job(body: JobBody, c: CtxDep) -> dict[str, str]:
    _validate(body.kind, body.workspace, body.params)
    _check_workspace(c, body.workspace)
    if body.kind == "run":
        quotas.spend(c.org_id, "runs", auth._now())
    return {"id": JobStore().enqueue(c.org_id, body.workspace, body.kind, c.principal, body.params)}


@router.get("/jobs")
def list_jobs(c: CtxDep, workspace: str | None = None) -> list[dict[str, Any]]:
    return [_public(j) for j in JobStore().list(c.org_id, workspace)]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, c: CtxDep) -> dict[str, Any]:
    job = JobStore().get(c.org_id, job_id)
    if job is None:
        raise HTTPException(404, NOT_FOUND)
    return _public(job)


@router.get("/jobs/{job_id}/log")
def job_log(job_id: str, c: CtxDep, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    tail = JobStore().read_log(c.org_id, job_id, offset=offset)
    if tail is None:
        raise HTTPException(404, NOT_FOUND)
    return tail


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, c: CtxDep) -> dict[str, str]:
    state = JobStore().cancel(c.org_id, job_id)
    if state is None:
        raise HTTPException(404, NOT_FOUND)
    return {"id": job_id, "state": state}


def _waits(asked: bool | None) -> bool:
    """A local install has no worker running, and the console reads the report
    from the response, so by default the API drives the job itself. Hosted sets
    IRONWRIGHT_RUNS_WAIT=0. The body can only opt out: a client must not be able
    to pull a pipeline run into the hosted API process."""
    return os.environ.get("IRONWRIGHT_RUNS_WAIT", "1") != "0" and asked is not False


def submit_run(c: Ctx, response: Response, workspace: str, seed: int | None,
               stages: list[str] | None, wait: bool | None) -> dict[str, Any]:
    """Always through the queue. Waiting uses the same claim and execute a worker
    does, so one running job per org and the cap hold here too."""
    params = {k: v for k, v in (("seed", seed), ("stages", stages)) if v is not None}
    _validate("run", workspace, params)
    if workspace not in list_packs():
        try:
            wt.load_workspace(c.org_id, workspace)
        except FileNotFoundError as e:  # past the gate, so this org may know: not runnable
            raise HTTPException(404, f"no workspace named {workspace}") from e
        except wt.WorkspaceError as e:
            raise HTTPException(422, str(e)) from e
    org_id = c.org_id
    quotas.spend(org_id, "runs", auth._now())
    store = JobStore()
    job_id = store.enqueue(org_id, workspace, "run", c.principal, params)
    if _waits(wait):
        worker = Worker(store)
        deadline = time.monotonic() + RUN_WAIT_SECONDS
        while time.monotonic() < deadline:
            if store.get(org_id, job_id)["state"] in TERMINAL:
                break
            if worker.run_once(job_id) is None:
                time.sleep(min(0.2, RUN_WAIT_SECONDS))
    job = store.get(org_id, job_id)
    if job["state"] == "failed":
        raise HTTPException(500, {"id": job_id, "state": "failed", "error": job["error"]})
    if job["state"] != "succeeded":
        response.status_code = 202
        return {"id": job_id, "state": job["state"], "workspace": workspace}
    return {**(job["result"] or {}), "id": job_id, "state": job["state"]}
