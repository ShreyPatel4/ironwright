"""An org's people, its audit chain, and its data on the way out. All under /orgs/{slug}.

The app gate has already answered 404 to anyone outside the org in the path, and it
has checked the role for a write. A write it does not list needs owner, which is what
export and delete want. Reads pass the gate for any member, so these check their own.

A missing member or export answers its own words, never the gate's. The isolation
sweep reads the gate's exact bytes as "this caller was stopped at the door".
"""
from __future__ import annotations

import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth
from ..jobs import routes as job_routes
from ..jobs.store import TERMINAL, JobStore
from ..jobs.worker import Worker
from . import quotas, service, usage
from .models import DEV_ORG_ID, Ctx, at_least

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(auth.ctx)]


class RoleBody(BaseModel):
    role: str


class DeleteBody(BaseModel):
    confirm: str


def _org(slug: str, c: Ctx, needed: str) -> str:
    if not at_least(service.role_in(c.user_id, slug), needed):
        raise HTTPException(403, f"only an org {needed} can do this")
    return quotas.org_id_of(slug)


def _drive(store: JobStore, org_id: str, job_id: str) -> None:
    """A local install has no worker, so the API drives the job itself, the way a run
    does. Hosted sets IRONWRIGHT_RUNS_WAIT=0 and this leaves it to the worker."""
    if not job_routes._waits(None):
        return
    worker = Worker(store)
    deadline = time.monotonic() + job_routes.RUN_WAIT_SECONDS
    while time.monotonic() < deadline:
        job = store.get(org_id, job_id)
        if job is None or job["state"] in TERMINAL:
            return
        if worker.run_once(job_id) is None:
            time.sleep(0.2)


@router.get("/orgs/{slug}/members")
def list_members(slug: str, c: CtxDep) -> dict[str, Any]:
    return service.list_members(_org(slug, c, "admin"), auth._now())


def _member_change(change: Any) -> dict[str, bool]:
    try:
        change()
    except service.Invalid as e:
        raise HTTPException(422, str(e)) from None
    except service.NoSuchMember:
        raise HTTPException(404, "no such member in this org") from None
    except service.Forbidden as e:
        raise HTTPException(403, str(e)) from None
    except service.LastOwner:
        raise HTTPException(409, "this is the last owner: make someone else an owner first") from None
    return {"ok": True}


@router.put("/orgs/{slug}/members/{user_id}")
def set_member_role(slug: str, user_id: str, body: RoleBody, c: CtxDep) -> dict[str, bool]:
    org_id = quotas.org_id_of(slug)
    return _member_change(lambda: service.set_role(org_id, user_id, body.role, c.user_id, c.principal, auth._now()))


@router.delete("/orgs/{slug}/members/{user_id}")
def remove_member(slug: str, user_id: str, c: CtxDep) -> dict[str, bool]:
    org_id = quotas.org_id_of(slug)
    return _member_change(lambda: service.remove_member(org_id, user_id, c.user_id, c.principal, auth._now()))


@router.get("/orgs/{slug}/audit/chain")
def audit_chain(slug: str, c: CtxDep, actor: str | None = None, action: str | None = None,
                limit: Annotated[int, Query(ge=1, le=1000)] = 500, prefix: str | None = None) -> dict[str, Any]:
    return usage.audit_chain(_org(slug, c, "admin"), actor or None, action or None, limit, prefix or None)


@router.get("/orgs/{slug}/audit/export")
def audit_export(slug: str, c: CtxDep) -> Response:
    """The whole chain as JSON lines, hashes included, for an auditor or a SIEM."""
    lines = usage.export_chain(_org(slug, c, "admin"))
    body = "".join(json.dumps(x, sort_keys=True) + "\n" for x in lines)
    return Response(body, media_type="application/x-ndjson",
                    headers={"content-disposition": f'attachment; filename="{slug}-audit.jsonl"'})


@router.post("/orgs/{slug}/exports", status_code=202)
def create_export(slug: str, c: CtxDep) -> dict[str, str]:
    org_id = quotas.org_id_of(slug)
    if org_id == DEV_ORG_ID:
        raise HTTPException(403, "the dev org holds no tenant data to export")
    store = JobStore()
    job_id = store.enqueue(org_id, service.ORG_JOBS, "org_export", c.principal, {})
    _drive(store, org_id, job_id)
    return {"id": job_id}


@router.get("/orgs/{slug}/exports")
def list_exports(slug: str, c: CtxDep) -> list[dict[str, Any]]:
    org_id = _org(slug, c, "owner")
    return [{k: j[k] for k in ("id", "state", "created_at", "finished_at", "error", "result")}
            for j in JobStore().list(org_id, service.ORG_JOBS) if j["kind"] == "org_export"]


@router.get("/orgs/{slug}/exports/{job_id}/download")
def download_export(slug: str, job_id: str, c: CtxDep) -> FileResponse:
    org_id = _org(slug, c, "owner")
    job = JobStore().get(org_id, job_id)  # scoped: another org's job id reads as no such job
    path = service.export_file(org_id, job_id) if job and job["kind"] == "org_export" and job["state"] == "succeeded" else None
    if path is None:
        raise HTTPException(404, "no such export")
    return FileResponse(path, media_type="application/zip", filename=f"{slug}-export.zip")


@router.post("/orgs/{slug}/delete")
def delete_org(slug: str, body: DeleteBody, response: Response, c: CtxDep) -> dict[str, str]:
    if body.confirm != slug:
        raise HTTPException(422, "type the org's slug to confirm")
    org_id = quotas.org_id_of(slug)
    try:
        first = service.mark_deleting(org_id, c.principal, auth._now())
    except service.Forbidden as e:
        raise HTTPException(403, str(e)) from None
    store = JobStore()
    if first:
        job_id = service.enqueue_deletion(org_id, c.principal, store)
        _drive(store, org_id, job_id)
    gone = quotas.org_id_of(slug) != org_id
    if not gone:
        response.status_code = 202
    return {"state": "deleted" if gone else "deleting"}
