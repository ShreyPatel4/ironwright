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
from . import cookies, mfa, ratelimit, tokens

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
    """An MFA ticket is not a session, so it gets no cookie."""
    response = JSONResponse(answer, status_code=status)
    if "token" in answer:
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


class TokenBody(BaseModel):
    token: str


class EmailBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    password: str


@router.post("/auth/verify")
def auth_verify(body: TokenBody) -> dict[str, bool]:
    try:
        tenancy.verify_email(body.token, auth._now())
    except tenancy.NoSuchLink:
        raise HTTPException(404, "that link is not valid; it may have been used or expired") from None
    return {"ok": True}


@router.post("/auth/verify/resend")
def auth_verify_resend(c: CtxDep) -> dict[str, bool]:
    tenancy.resend_verify(c.user_id, auth._now())
    return {"ok": True}


@router.post("/auth/reset-request", status_code=202)
def auth_reset_request(body: EmailBody) -> dict[str, Any]:
    tenancy.request_reset(body.email, auth._now())
    return {"ok": True, "detail": "if that email has an account, a reset link is on its way"}


@router.post("/auth/reset")
def auth_reset(body: ResetBody) -> dict[str, bool]:
    try:
        tenancy.reset_password(body.token, body.password, auth._now())
    except tenancy.Invalid as e:
        raise HTTPException(422, str(e)) from None
    except tenancy.NoSuchLink:
        raise HTTPException(404, "that link is not valid; it may have been used or expired") from None
    return {"ok": True}


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
    answer["mfa_setup_required"] = not auth.dev_open() and tenancy.mfa_setup_required(c)
    if via == "cookie":
        answer["csrf"] = cookies.csrf_of(token)
    return answer


@router.post("/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    """A door, so a dead session can still clear its cookie. A live cookie session
    still needs its CSRF token, or any page could sign you out."""
    token, via = cookies.credential(request.headers)
    if via == "cookie" and auth.session_ctx(token) is not None \
            and not cookies.csrf_ok(token, request.headers.get(cookies.CSRF_HEADER)):
        raise HTTPException(403, "this request needs the X-CSRF-Token header; reload the page and try again")
    tenancy.revoke(token)
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


# ----- two-step sign in

class MfaBody(BaseModel):
    ticket: str
    code: str | None = None
    recovery_code: str | None = None


class CodeBody(BaseModel):
    code: str | None = None


class OrgSecurityBody(BaseModel):
    require_mfa: bool


def _conflicts(change: Any) -> Any:
    try:
        return change()
    except mfa.NoSecret as e:
        raise HTTPException(503, str(e)) from None
    except tenancy.Conflict as e:
        raise HTTPException(409, str(e)) from None
    except tenancy.Invalid as e:
        raise HTTPException(422, str(e)) from None
    except tenancy.Forbidden as e:
        raise HTTPException(403, str(e)) from None


@router.post("/auth/mfa")
def auth_mfa(body: MfaBody, request: Request) -> JSONResponse:
    """The second step of a sign in. Wrong codes count toward the email's lock."""
    try:
        return opened(tenancy.complete_mfa(body.ticket, body.code, body.recovery_code, auth._now(), seen(request)))
    except ratelimit.Locked as e:
        raise HTTPException(429, ratelimit.LOCKED_DETAIL) from e
    except tenancy.BadCredentials as e:
        raise HTTPException(401, "that code does not match, or the sign in took too long; start again") from e


@router.get("/auth/mfa/status")
def auth_mfa_status(c: CtxDep) -> dict[str, Any]:
    return tenancy.mfa_status(c.user_id)


@router.post("/auth/mfa/enrol")
def auth_mfa_enrol(c: CtxDep) -> dict[str, str]:
    return _conflicts(lambda: tenancy.mfa_enrol(c.user_id, auth._now()))


@router.post("/auth/mfa/confirm")
def auth_mfa_confirm(body: CodeBody, request: Request, c: CtxDep) -> JSONResponse:
    done = _conflicts(lambda: tenancy.mfa_confirm(c, body.code or "", auth._now(), seen(request)))
    response = JSONResponse(done)
    cookies.issue(response, done["session"]["token"], tenancy.ABSOLUTE_SECONDS)
    return response


@router.post("/auth/mfa/disable")
def auth_mfa_disable(body: CodeBody, c: CtxDep) -> dict[str, bool]:
    _conflicts(lambda: tenancy.mfa_disable(c, body.code, auth._now()))
    return {"ok": True}


@router.post("/auth/mfa/recovery-codes")
def auth_mfa_recovery(body: CodeBody, c: CtxDep) -> dict[str, Any]:
    return {"recovery_codes": _conflicts(lambda: tenancy.mfa_new_recovery(c, body.code, auth._now()))}


@router.get("/orgs/{slug}/security")
def org_security(slug: str, c: CtxDep) -> dict[str, Any]:
    if not at_least(tenancy.role_in(c.user_id, slug), "admin"):
        raise HTTPException(403, "only an org admin can see this")
    return tenancy.org_security(quotas.org_id_of(slug))


@router.put("/orgs/{slug}/security")
def put_org_security(slug: str, body: OrgSecurityBody, c: CtxDep) -> dict[str, Any]:
    org_id = quotas.org_id_of(slug)
    _conflicts(lambda: tenancy.set_require_mfa(org_id, body.require_mfa, c.user_id, c.principal, auth._now()))
    return tenancy.org_security(org_id)


# ----- API tokens

class TokenCreateBody(BaseModel):
    name: str
    scopes: list[str]


def _admin_of(slug: str, c: Ctx) -> str:
    if not at_least(tenancy.role_in(c.user_id, slug), "admin"):
        raise HTTPException(403, "only an org admin can do this")
    return quotas.org_id_of(slug)


@router.get("/orgs/{slug}/tokens")
def list_tokens(slug: str, c: CtxDep) -> list[dict[str, Any]]:
    return tokens.listed(_admin_of(slug, c))


@router.post("/orgs/{slug}/tokens", status_code=201)
def create_token(slug: str, body: TokenCreateBody, c: CtxDep) -> dict[str, Any]:
    """The answer holds the only copy of the token."""
    try:
        return tokens.create(_admin_of(slug, c), body.name, body.scopes, c.principal, auth._now())
    except tokens.Invalid as e:
        raise HTTPException(422, str(e)) from None


@router.delete("/orgs/{slug}/tokens/{token_id}")
def revoke_token(slug: str, token_id: str, c: CtxDep) -> dict[str, bool]:
    if not tokens.revoke(_admin_of(slug, c), token_id, c.principal, auth._now()):
        raise HTTPException(404, "no such token")
    return {"ok": True}
