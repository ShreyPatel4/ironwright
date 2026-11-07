"""The /auth routes, org switch and invites. Every answer that opens a session sets the
cookie and still carries the token in the body, for API clients. The console never
stores that token: it keeps only the CSRF token, in memory.

The app gate runs before all of these. PUBLIC_ROUTES in auth.py are the doors that
need no session. Every self-service write here is listed for viewer in WRITE_ROLES.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import auth
from ..tenancy import ROLES, Ctx, quotas
from ..tenancy import service as tenancy
from ..tenancy.models import at_least
from . import cookies, ratelimit

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(auth.ctx)]
NOT_FOUND = "no such resource"


class LoginBody(BaseModel):
    email: str
    password: str


class SignupBody(BaseModel):
    email: str
    password: str
    org_name: str


class InviteBody(BaseModel):
    email: str
    role: str = "member"


class AcceptInviteBody(BaseModel):
    token: str
    password: str


def seen(request: Request) -> dict[str, str]:
    """What the sessions list shows about a sign in, and the session it replaces."""
    return {"ip": request.client.host if request.client else "",
            "user_agent": request.headers.get("user-agent", ""),
            "replacing": cookies.credential(request.headers)[0]}


def opened(answer: dict[str, Any], status: int = 200) -> JSONResponse:
    response = JSONResponse(answer, status_code=status)
    cookies.issue(response, answer["token"], tenancy.ABSOLUTE_SECONDS)
    return response


@router.get("/auth/status")
def auth_status() -> dict[str, Any]:
    return {"seeded": auth.seeded(), "dev_open": auth.dev_open(),
            "note": "Every API route needs a session. Sign up to make an org, "
                    "or accept an invite to join one."}


@router.post("/auth/signup", status_code=201)
def auth_signup(body: SignupBody, request: Request) -> JSONResponse:
    try:
        return opened(tenancy.signup(body.email, body.password, body.org_name, auth._now(), seen(request)), 201)
    except tenancy.Invalid as e:
        raise HTTPException(422, str(e)) from e
    except tenancy.EmailTaken as e:
        raise HTTPException(409, "that email already has an account; sign in instead") from e


@router.post("/auth/login")
def auth_login(body: LoginBody, request: Request) -> JSONResponse:
    try:
        return opened(tenancy.login(body.email, body.password, auth._now(), seen(request)))
    except ratelimit.Locked as e:
        raise HTTPException(429, ratelimit.LOCKED_DETAIL) from e
    except tenancy.BadCredentials as e:
        raise HTTPException(401, "that email and password do not match") from e


@router.post("/auth/accept-invite")
def auth_accept_invite(body: AcceptInviteBody, request: Request) -> JSONResponse:
    try:
        return opened(tenancy.accept_invite(body.token, body.password, auth._now(), seen(request)))
    except tenancy.NoSuchInvite as e:
        raise HTTPException(404, "that invite is not valid") from e
    except ratelimit.Locked as e:
        raise HTTPException(429, ratelimit.LOCKED_DETAIL) from e
    except tenancy.BadCredentials as e:
        raise HTTPException(401, "that email and password do not match") from e
    except tenancy.Invalid as e:
        raise HTTPException(422, str(e)) from e


@router.get("/auth/me")
def auth_me(request: Request, c: CtxDep) -> dict[str, Any]:
    # A dev open server has no need of a token, but one it was handed still
    # has to be alive, or a client would never learn it logged out.
    token, via = cookies.credential(request.headers)
    if token:
        c = auth.session_ctx(token)
        if c is None:
            raise HTTPException(401, "no session", headers={"WWW-Authenticate": "Bearer"})
    answer = auth.describe(c)
    if via == "cookie":
        answer["csrf"] = cookies.csrf_of(token)
    return answer


@router.post("/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    tenancy.revoke(cookies.credential(request.headers)[0])
    response = JSONResponse({"ok": True})
    cookies.clear(response)
    return response


@router.get("/auth/sessions")
def auth_sessions(request: Request, c: CtxDep) -> list[dict[str, Any]]:
    return tenancy.list_sessions(c.user_id, cookies.credential(request.headers)[0], auth._now())


@router.delete("/auth/sessions/{session_id}")
def auth_end_session(session_id: str, c: CtxDep) -> dict[str, bool]:
    if not tenancy.revoke_session(c.user_id, session_id):
        raise HTTPException(404, NOT_FOUND)  # someone else's reads like nothing at all
    return {"ok": True}


@router.post("/auth/sessions/revoke-all")
def auth_sign_out_everywhere(c: CtxDep) -> JSONResponse:
    tenancy.revoke_all(c.user_id, c.principal, auth._now())
    response = JSONResponse({"ok": True})
    cookies.clear(response)
    return response


@router.post("/orgs/{slug}/switch")
def org_switch(slug: str, request: Request, c: CtxDep) -> JSONResponse:
    answer = tenancy.switch(c, slug, auth._now(), seen(request))
    if answer is None:
        raise HTTPException(404, NOT_FOUND)
    return opened(answer)


@router.post("/orgs/{slug}/invites", status_code=201)
def org_invite(slug: str, body: InviteBody, c: CtxDep) -> dict[str, Any]:
    mine = tenancy.role_in(c.user_id, slug)
    if body.role in ROLES and not at_least(mine, body.role):
        raise HTTPException(403, "you cannot invite someone above your own role")
    quotas.check_seat(slug, body.email, auth._now())
    try:
        return {"token": tenancy.invite(slug, body.email, body.role, auth._now(), c.principal),
                "email": body.email.strip().lower(), "role": body.role, "org_slug": slug}
    except tenancy.Invalid as e:
        raise HTTPException(422, str(e)) from e
