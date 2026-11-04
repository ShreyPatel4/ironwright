"""The jobs API (contract 4) and the enqueue behind POST /workspaces/{name}/runs.

Every lookup is scoped to the caller's org in the store, so another org's job id
reads as 404 on every route. Until T1 merges the org is DEV_ORG; request_org is
the one line T1 swaps for ctx().
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from .. import auth as auth_mod
from ..config import SAFE_NAME
from . import handlers
from .store import DEV_ORG, TERMINAL, JobStore
from .worker import Worker

MAX_PARAMS_BYTES = 64 * 1024
RUN_WAIT_SECONDS = 600.0

router = APIRouter()


class JobBody(BaseModel):
    kind: str
    workspace: str
    params: dict[str, Any] = {}


def request_org(request: Request) -> tuple[str, str]:
    return DEV_ORG, auth_mod.current_principal(request)


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


@router.post("/jobs")
def create_job(body: JobBody, who: tuple[str, str] = Depends(request_org)) -> dict[str, str]:
    _validate(body.kind, body.workspace, body.params)
    org_id, principal = who
    return {"id": JobStore().enqueue(org_id, body.workspace, body.kind, principal, body.params)}


@router.get("/jobs")
def list_jobs(workspace: str | None = None, who: tuple[str, str] = Depends(request_org)) -> list[dict[str, Any]]:
    return [_public(j) for j in JobStore().list(who[0], workspace)]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, who: tuple[str, str] = Depends(request_org)) -> dict[str, Any]:
    job = JobStore().get(who[0], job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return _public(job)


@router.get("/jobs/{job_id}/log")
def job_log(job_id: str, offset: int = Query(0, ge=0),
            who: tuple[str, str] = Depends(request_org)) -> dict[str, Any]:
    tail = JobStore().read_log(who[0], job_id, offset=offset)
    if tail is None:
        raise HTTPException(404, "no such job")
    return tail


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, who: tuple[str, str] = Depends(request_org)) -> dict[str, str]:
    state = JobStore().cancel(who[0], job_id)
    if state is None:
        raise HTTPException(404, "no such job")
    return {"id": job_id, "state": state}


def _waits(asked: bool | None) -> bool:
    """A local install has no worker running, and the console reads the report
    from the response, so by default the API drives the job itself. Hosted sets
    IRONWRIGHT_RUNS_WAIT=0. The body can only opt out: a client must not be able
    to pull a pipeline run into the hosted API process."""
    return os.environ.get("IRONWRIGHT_RUNS_WAIT", "1") != "0" and asked is not False


def submit_run(request: Request, response: Response, workspace: str, seed: int | None,
               stages: list[str] | None, wait: bool | None) -> dict[str, Any]:
    """Always through the queue. Waiting uses the same claim and execute a worker
    does, so one running job per org and the cap hold here too."""
    params = {k: v for k, v in (("seed", seed), ("stages", stages)) if v is not None}
    _validate("run", workspace, params)
    org_id, principal = request_org(request)
    store = JobStore()
    job_id = store.enqueue(org_id, workspace, "run", principal, params)
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
