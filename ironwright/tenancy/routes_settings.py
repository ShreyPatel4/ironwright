"""An org's settings, all under /orgs/{slug}.

The app gate has already answered 404 to anyone outside the org in the path, and
it has checked the role for a write. These handlers take the org from that slug,
not from the session: someone in two orgs can read either one.

PUT /llm-key reads its own body. A pydantic refusal echoes its input in the 422,
and here that input can be a real key.
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import auth
from . import byokey, quotas, service
from .models import Ctx, at_least

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(auth.ctx)]


def _admin_org(slug: str, c: Ctx) -> str:
    """For reads, which the gate lets any member make. Writes it has already checked."""
    if not at_least(service.role_in(c.user_id, slug), "admin"):
        raise HTTPException(403, "only an org admin can see this")
    return quotas.org_id_of(slug)


async def _raw_body(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError:
        return None


@router.get("/orgs/{slug}/usage")
def org_usage(slug: str, c: CtxDep) -> dict[str, Any]:
    return quotas.report(quotas.org_id_of(slug), auth._now())


@router.get("/orgs/{slug}/llm-key")
def get_llm_key(slug: str, c: CtxDep) -> dict[str, Any]:
    return byokey.status(_admin_org(slug, c)) or {"provider": None}


@router.put("/orgs/{slug}/llm-key")
def put_llm_key(slug: str, body: Annotated[Any, Depends(_raw_body)], c: CtxDep) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(422, "send a JSON object with provider and key")
    try:
        return byokey.store(quotas.org_id_of(slug), body.get("provider"), body.get("key"), c.principal, auth._now())
    except service.Invalid as e:
        raise HTTPException(422, str(e)) from None
    except byokey.NoSecret as e:
        raise HTTPException(503, str(e)) from None


@router.delete("/orgs/{slug}/llm-key")
def delete_llm_key(slug: str, c: CtxDep) -> dict[str, bool]:
    return {"removed": byokey.remove(quotas.org_id_of(slug), c.principal, auth._now())}


@router.get("/orgs/{slug}/audit")
def org_audit(slug: str, c: CtxDep) -> list[dict[str, Any]]:
    return quotas.usage.audit_log(_admin_org(slug, c))
