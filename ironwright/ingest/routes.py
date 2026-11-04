"""The upload routes. They inherit the app-wide session gate from platform_api."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..config import UnsafePath
from . import uploads

router = APIRouter()

# Multipart framing around each file. Only feeds the early Content-Length check.
_PART_OVERHEAD = 32 * 1024


def current_org() -> str:
    """The one place a route learns its org. T1 replaces the body with ctx().org_id."""
    return uploads.DEV_ORG


@router.post("/uploads", status_code=201)
async def post_upload(request: Request, org_id: str = Depends(current_org)) -> dict[str, Any]:
    session: uploads.UploadSession | None = None
    try:
        limit = uploads.max_files() * (uploads.max_file_bytes() + _PART_OVERHEAD)
        if int(request.headers.get("content-length") or 0) > limit:
            raise uploads.UploadRefused(413, "the request is over the upload caps")
        session = uploads.UploadSession(org_id)
        sink = uploads.MultipartSink(session, request.headers.get("content-type", ""))
        async for chunk in request.stream():
            await run_in_threadpool(sink.write, chunk)
        sink.finish()
        made = await run_in_threadpool(session.commit)
        session = None
        return made
    except uploads.UploadRefused as e:
        raise HTTPException(e.status, e.detail) from e
    finally:
        if session is not None:
            session.abort()


def _read(org_id: str, upload_id: str, name: str) -> Any:
    try:
        return json.loads((uploads.upload_dir(org_id, upload_id) / name).read_text())
    except (UnsafePath, FileNotFoundError) as e:
        raise HTTPException(404, "no such upload") from e


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str, org_id: str = Depends(current_org)) -> dict[str, Any]:
    return _read(org_id, upload_id, uploads.MANIFEST)


@router.get("/uploads/{upload_id}/profile")
def get_profile(upload_id: str, org_id: str = Depends(current_org)) -> dict[str, Any]:
    return _read(org_id, upload_id, "profile.json")


@router.get("/uploads/{upload_id}/proposal")
def get_proposal(upload_id: str, org_id: str = Depends(current_org)) -> dict[str, Any]:
    return _read(org_id, upload_id, "proposal.json")
