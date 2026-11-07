"""Request identity: from a bearer token to a Ctx, through the tenancy store.

ctx() is the one FastAPI dependency that says who is calling and for which org.
The principal is user:<user_id> and comes from the session and nowhere else. An
agent principal can never come from a request, because no user id starts that way.

IRONWRIGHT_DEV_OPEN=1 keeps the old open behavior for tests and demo-check: no
token needed, a fixed dev org, and the X-Principal header names the caller.
Seeding a local operator happens through `ironwright auth-set <email>`, which
prompts for the password so no secret ever lands in code, logs or history.
"""
from __future__ import annotations

import os
import time
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from . import durable
from .tenancy import service
from .tenancy.models import DEV_ORG_ID, DEV_ORG_SLUG, DEV_USER_ID, Ctx

CONSOLE_PRINCIPAL = "user:console"
IDLE_SECONDS = service.IDLE_SECONDS
# "/" only redirects to the console. The console itself is a mount, which
# route dependencies never reach.
PUBLIC_ROUTES = frozenset({"/auth/status", "/auth/login", "/auth/signup",
                           "/auth/accept-invite", "/"})

_now = time.time


def dev_open() -> bool:
    return os.environ.get("IRONWRIGHT_DEV_OPEN") == "1"


def is_agent(principal: str) -> bool:
    return principal.strip().lower().startswith("agent:")


def set_account(email: str, password: str) -> None:
    try:
        service.seed_operator(email, password, _now())
    except service.Invalid as e:
        raise ValueError(str(e)) from e


def seeded() -> bool:
    return service.any_users()


def bearer_token(headers: Headers) -> str:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def session_ctx(token: str) -> Ctx | None:
    return service.resolve(token, _now())


def _dev_ctx(request: Request) -> Ctx:
    return Ctx(org_id=DEV_ORG_ID, org_slug=DEV_ORG_SLUG, user_id=DEV_USER_ID, role="owner",
               principal=request.headers.get("x-principal", CONSOLE_PRINCIPAL))


def ctx(request: Request) -> Ctx:
    """Who is calling, and for which org. The one dependency; see contract 1."""
    if dev_open():
        found = _dev_ctx(request)
    else:
        found = getattr(request.state, "ctx", None) or session_ctx(bearer_token(request.headers))
        if found is None:
            raise HTTPException(401, "no session", headers={"WWW-Authenticate": "Bearer"})
        request.state.ctx = found
    durable.hydrate_for(request, found.org_id)  # a fresh disk gets this org back first
    return found


def describe(found: Ctx) -> dict[str, Any]:
    if found.org_id == DEV_ORG_ID and found.user_id == DEV_USER_ID:
        return {**found.__dict__, "email": None, "orgs": []}
    return service.describe(found)


class BindPrincipalHeader:
    """Makes X-Principal the session's principal, whatever the client sent.

    Only asof.py still reads the header. This stops that route trusting the
    client until it moves to ctx(), then this class can go.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not dev_open():
            headers = [h for h in scope["headers"] if h[0] != b"x-principal"]
            found = session_ctx(bearer_token(Headers(scope=scope)))
            state = dict(scope.get("state") or {})
            if found:
                headers.append((b"x-principal", found.principal.encode()))
                state["ctx"] = found
            scope = {**scope, "headers": headers, "state": state}
        await self.app(scope, receive, send)
