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

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from .. import auth, verification
from .. import workspaces_tenant as wt
from ..config import UnsafePath, data_root, safe_child
from ..jobs.store import JobStore
from ..packs import list_packs
from ..tenancy import Ctx
from . import silver_draft, uploads

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


class SilverChoice(BaseModel):
    """What a human did to the silver draft. The SQL itself never comes from a client."""
    model_config = ConfigDict(extra="forbid")

    declined: list[str] = Field(default=[], max_length=silver_draft.MAX_DECLINED)
    renames: dict[str, dict[str, str]] = {}


class CreateFromUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_upload: str
    spec: dict[str, Any]
    slug: str
    models: dict[str, dict[str, str]] | None = None
    silver: SilverChoice | None = None
    records: dict[str, Any] | None = None


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


def _drafted(body: CreateFromUpload, c: Ctx) -> tuple[dict[str, Any] | None, dict[str, dict[str, str]] | None]:
    """(the draft to keep, the models to write). The draft was made in the profile job, so
    this only reads what that job wrote and puts the human's choices on it."""
    if body.silver is None:
        return None, body.models
    if (body.models or {}).get("silver"):
        raise HTTPException(422, "send a silver choice or silver SQL, not both")
    try:
        proposal = json.loads((uploads.upload_dir(c.org_id, body.from_upload) / "proposal.json").read_text())
    except FileNotFoundError as e:
        raise HTTPException(422, "profile this upload first, so there is a draft to choose from") from e
    made = proposal.get("silver")
    if made is None:
        raise HTTPException(422, "this upload was profiled before silver drafts existed, so profile it again")
    spec = wt._normalize(c.org_id, body.spec, body.models)
    files = silver_draft.models(made, spec, body.silver.declined, body.silver.renames)
    return made, {**(body.models or {}), "silver": files}


def create_from_upload(body: CreateFromUpload, c: Ctx) -> dict[str, Any]:
    _require_upload(c.org_id, body.from_upload)
    if _reserved(body.slug):
        raise HTTPException(422, RESERVED)
    if not _inside_upload(body.spec, body.from_upload):
        raise HTTPException(422, "every source has to come from the upload named in from_upload")
    try:
        made, models = _drafted(body, c)
        wt.create_workspace(c.org_id, body.slug, body.spec, models=models, records=body.records)
        if made is not None:
            silver_draft.save(c.org_id, body.slug, made, body.silver.declined, body.silver.renames)
        return _entry(wt.load_workspace(c.org_id, body.slug))
    except (wt.WorkspaceError, silver_draft.DraftError) as e:
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


# ---------------------------------------------------------------- regenerate
# The app-wide gate scopes these. Past it an org may learn its own state, so these answers
# read differently from the gate's NOT_FOUND, which the isolation sweep holds apart.
NOT_TENANT = "this is not one of your workspaces, so it has no silver draft"
NO_DRAFT = "no silver draft yet: queue a silver_draft job"


@router.get("/workspaces/{name}/silver/draft")
def get_silver_draft(name: str, c: CtxDep) -> dict[str, Any]:
    try:
        saved = silver_draft.read_saved(c.org_id, name)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e
    if saved is None:
        raise HTTPException(404, NO_DRAFT)
    return saved


@router.post("/workspaces/{name}/silver/draft")
def regenerate_silver(name: str, body: SilverChoice, c: CtxDep) -> dict[str, Any]:
    """The kept draft with these choices becomes models/silver. The next run builds it."""
    try:
        return silver_draft.apply_choices(c.org_id, name, body.declined, body.renames)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except (wt.WorkspaceError, silver_draft.DraftError) as e:
        raise HTTPException(422, str(e)) from e


@router.post("/uploads/{upload_id}/profile", status_code=status.HTTP_202_ACCEPTED)
def post_profile(upload_id: str, c: CtxDep) -> dict[str, str]:
    _require_upload(c.org_id, upload_id)
    return {"id": JobStore().enqueue(c.org_id, uploads.profile_job_label(upload_id), "profile", c.principal,
                                     {"upload_id": upload_id})}
