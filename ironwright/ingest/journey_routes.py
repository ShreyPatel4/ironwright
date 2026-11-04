"""The two steps between an upload and a workspace (contract 5).

POST /uploads/{id}/profile queues T3's profile job, which needs no workspace yet.
GET /uploads/{id}/proposal already lives in routes.py and reads what that job wrote.
POST /workspaces with {from_upload, spec, slug} turns an approved proposal into a
contract 3 workspace. platform_api owns that path because scenario orgs share it, so
it hands this module the body.

The org comes from ctx() and nothing else. Another org's upload id answers exactly
what an id nobody has answers, and nothing gets queued or written.
"""
from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from .. import auth, verification
from .. import workspaces_tenant as wt
from ..config import UnsafePath, data_root, safe_child
from ..jobs.store import JobStore
from ..packs import list_packs
from ..tenancy import Ctx
from . import uploads

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(auth.ctx)]
# platform_api.NOT_FOUND, which imports this module. The isolation sweep holds them equal.
NOT_FOUND = "no such resource"
RESERVED = "that name is reserved by the platform; pick another"
# How platform_api spells a tenant's scenario folder. Those share one namespace under the
# data root, and the gate opens any name an org holds a folder for, so a workspace spelled
# like one would reach another org's scenario. It is a rule about the spelling, so the
# answer says nothing about whether that folder exists.
_SCENARIO_FOLDER = re.compile(r"o[0-9a-f]{8}-.*")


class CreateFromUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_upload: str
    spec: dict[str, Any]
    slug: str
    models: dict[str, dict[str, str]] | None = None


def _require_upload(org_id: str, upload_id: str) -> None:
    try:
        uploads.manifest(org_id, upload_id)
    except (UnsafePath, FileNotFoundError) as e:
        raise HTTPException(404, NOT_FOUND) from e


def _reserved(slug: str) -> bool:
    """A demo pack answers first at the gate, so a workspace with its name is unreachable.
    A scenario org with no prefix is the dev org's, and it stays the dev org's."""
    if slug in list_packs() or _SCENARIO_FOLDER.fullmatch(slug):
        return True
    try:
        return (safe_child(data_root(), slug) / "org.json").exists()
    except UnsafePath:
        return False  # create_workspace refuses the bad name in its own words


def _inside_upload(spec: dict[str, Any], upload_id: str) -> bool:
    """Only the refs that are spelled like refs. The schema answers for the rest."""
    sources = spec.get("sources")
    refs = [s["upload"] for s in sources if isinstance(s, dict) and isinstance(s.get("upload"), str)] \
        if isinstance(sources, list) else []
    return all(r.startswith(f"{upload_id}/") for r in refs)


def _entry(pack: Any) -> dict[str, Any]:
    return {"name": pack.name, "kind": "workspace", "title": pack.spec["title"], "decision": None,
            "verification": verification.derive(pack)}


def create_from_upload(body: CreateFromUpload, c: Ctx) -> dict[str, Any]:
    _require_upload(c.org_id, body.from_upload)
    if _reserved(body.slug):
        raise HTTPException(422, RESERVED)
    if not _inside_upload(body.spec, body.from_upload):
        raise HTTPException(422, "every source has to come from the upload named in from_upload")
    try:
        wt.create_workspace(c.org_id, body.slug, body.spec, models=body.models)
        return _entry(wt.load_workspace(c.org_id, body.slug))
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e


def listed(org_id: str) -> list[dict[str, Any]]:
    """What GET /workspaces adds for an org. One that no longer loads stays out, the way
    list_workspaces leaves out one it cannot read."""
    out = []
    for w in wt.list_workspaces(org_id):
        try:
            out.append(_entry(wt.load_workspace(org_id, w["slug"])))
        except (wt.WorkspaceError, FileNotFoundError):
            continue
    return out


@router.post("/uploads/{upload_id}/profile", status_code=status.HTTP_202_ACCEPTED)
def post_profile(upload_id: str, c: CtxDep) -> dict[str, str]:
    """A job row needs a workspace name and the handler never reads this one. The label
    keeps it apart from real workspaces in GET /jobs?workspace=."""
    _require_upload(c.org_id, upload_id)
    return {"id": JobStore().enqueue(c.org_id, f"upload-{upload_id}", "profile", c.principal,
                                     {"upload_id": upload_id})}
