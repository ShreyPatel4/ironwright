"""The upload routes. They inherit the app-wide session gate from platform_api.

The org comes from ctx() and nowhere else. Nothing in a request can name one, so a
tenant can only ever reach its own prefix. An id from another org answers exactly
what an id nobody has answers.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from .. import auth
from .. import workspaces_tenant as wt
from ..config import UnsafePath
from ..jobs.store import JobStore
from ..tenancy import Ctx, quotas
from . import journey_routes, uploads

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(auth.ctx)]
# platform_api.NOT_FOUND, which imports this module. The isolation sweep holds them equal.
NOT_FOUND = "no such resource"

# Multipart framing around each file. Only feeds the early Content-Length check.
_PART_OVERHEAD = 32 * 1024


@router.post("/uploads", status_code=201)
async def post_upload(request: Request, c: CtxDep) -> dict[str, Any]:
    session: uploads.UploadSession | None = None
    spent_at: float | None = None
    try:
        limit = uploads.max_files() * (uploads.max_file_bytes() + _PART_OVERHEAD)
        if int(request.headers.get("content-length") or 0) > limit:
            raise uploads.UploadRefused(413, "the request is over the upload caps")
        now = auth._now()
        await run_in_threadpool(quotas.start_upload, c.org_id, request.headers.get("content-length"), now)
        spent_at = now
        session = uploads.UploadSession(c.org_id)
        sink = uploads.MultipartSink(session, request.headers.get("content-type", ""))
        async for chunk in request.stream():
            await run_in_threadpool(sink.write, chunk)
        sink.finish()
        made = await run_in_threadpool(session.commit)
        session = None
        return made
    except uploads.UploadRefused as e:
        if spent_at is not None:  # the door spends before the bytes arrive, and nothing was stored
            await run_in_threadpool(quotas.refund_upload, c.org_id, spent_at)
        raise HTTPException(e.status, e.detail) from e
    finally:
        if session is not None:
            session.abort()


def _read(org_id: str, upload_id: str, name: str) -> Any:
    try:
        return json.loads((uploads.upload_dir(org_id, upload_id) / name).read_text())
    except (UnsafePath, FileNotFoundError) as e:
        raise HTTPException(404, NOT_FOUND) from e


@router.get("/uploads")
def list_uploads(c: CtxDep) -> list[dict[str, Any]]:
    """This org's uploads, newest first, each with how far it has got. The wizard resumes
    from this, so the status lives here and the job's label stays on the server."""
    store, out = JobStore(), []
    for made in uploads.list_uploads(c.org_id):
        folder = uploads.upload_dir(c.org_id, made["id"])
        jobs = store.list(c.org_id, uploads.profile_job_label(made["id"]), limit=1)
        out.append({**made, "profile_job": {"id": jobs[0]["id"], "state": jobs[0]["state"]} if jobs else None,
                    "profiled": (folder / "profile.json").is_file(), "proposed": (folder / "proposal.json").is_file()})
    return out


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str, c: CtxDep) -> dict[str, Any]:
    return _read(c.org_id, upload_id, uploads.MANIFEST)


@router.get("/uploads/{upload_id}/profile")
def get_profile(upload_id: str, c: CtxDep) -> dict[str, Any]:
    return _read(c.org_id, upload_id, "profile.json")


@router.get("/uploads/{upload_id}/proposal")
def get_proposal(upload_id: str, c: CtxDep) -> dict[str, Any]:
    return _read(c.org_id, upload_id, "proposal.json")


# ---------------------------------------------------------------- source versions
# The app-wide gate scopes every /workspaces/{name} route to the caller's org. Past it an
# org may learn its own state, so these answers read differently from the gate's NOT_FOUND
# (the isolation sweep holds them apart). The upload ref in the body resolves under
# ctx.org_id, so another org's id reads as one nobody has.
NOT_TENANT = "this is not one of your workspaces, so it has no source versions"


class VersionBody(BaseModel):
    upload: str


class SpecBody(BaseModel):
    columns: list[dict[str, Any]] | None = None
    layout: dict[str, Any] | None = None


class AddSourcesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_upload: str
    sources: list[dict[str, Any]]
    models: dict[str, dict[str, str]] | None = None


@contextlib.contextmanager
def _answers() -> Iterator[None]:
    try:
        yield
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except wt.NoSuchSource as e:
        raise HTTPException(404, str(e)) from e
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/workspaces/{name}/sources/{source}/versions")
def list_versions(name: str, source: str, c: CtxDep) -> dict[str, Any]:
    with _answers():
        return {"source": source, "versions": wt.source_versions(c.org_id, name, source)}


@router.post("/workspaces/{name}/sources/{source}/versions", status_code=201)
def add_version(name: str, source: str, body: VersionBody, c: CtxDep) -> dict[str, Any]:
    with _answers():
        return wt.add_source_version(c.org_id, name, source, body.upload, c.principal)


@router.post("/workspaces/{name}/sources/{source}/spec")
def update_spec(name: str, source: str, body: SpecBody, c: CtxDep) -> dict[str, Any]:
    with _answers():
        return wt.update_source_spec(c.org_id, name, source, columns=body.columns, layout=body.layout)


@router.post("/workspaces/{name}/sources", status_code=201)
def add_sources(name: str, body: AddSourcesBody, c: CtxDep) -> dict[str, Any]:
    journey_routes._require_upload(c.org_id, body.from_upload)
    if not journey_routes._inside_upload({"sources": body.sources}, body.from_upload):
        raise HTTPException(422, "every source has to come from the upload named in from_upload")
    with _answers():
        return {"sources": wt.add_sources(c.org_id, name, body.sources, models=body.models, created_by=c.principal)}


@router.get("/workspaces/{name}/drift")
def workspace_drift(name: str, c: CtxDep) -> dict[str, Any]:
    with _answers():
        return wt.drift_overview(c.org_id, name)
