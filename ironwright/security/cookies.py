"""The session cookie and its CSRF token.

The cookie is httpOnly, Secure and SameSite=Lax, scoped to the base path. A page
script never sees it. The CSRF token is derived from the session token, so it needs
no storage, and anyone who can read it already holds the session. The console gets
it in the login answer or from /auth/me and sends it back as X-CSRF-Token on every
unsafe request. A bearer client has no ambient credential, so it needs none.

IRONWRIGHT_COOKIE_SECURE=0 drops Secure for a plain http server on a LAN name.
Browsers keep Secure cookies on localhost and 127.0.0.1 without it.
"""
from __future__ import annotations

import hashlib
import secrets

from starlette.datastructures import Headers
from starlette.responses import Response

from ..config import setting

SESSION_COOKIE = "iw_session"
CSRF_HEADER = "x-csrf-token"


def base_path() -> str:
    """IRONWRIGHT_BASE_PATH, owned by C2's deploy work, as a cookie Path."""
    raw = (setting("BASE_PATH") or "").strip().strip("/")
    return f"/{raw}" if raw else "/"


def _secure() -> bool:
    return (setting("COOKIE_SECURE") or "1").strip().lower() not in ("0", "false", "off")


def csrf_of(token: str) -> str:
    return hashlib.sha256(b"iw-csrf\0" + token.encode()).hexdigest()


def csrf_ok(token: str, sent: str | None) -> bool:
    return bool(token and sent) and secrets.compare_digest(csrf_of(token), sent or "")


def issue(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, path=base_path(),
                        secure=_secure(), httponly=True, samesite="lax")


def clear(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path=base_path(), secure=_secure(), httponly=True, samesite="lax")


def credential(headers: Headers) -> tuple[str, str | None]:
    """(token, how it came). An Authorization header always wins and never falls back,
    so a client that sends one gets exactly what it sent."""
    raw = headers.get("authorization")
    if raw is not None:
        scheme, _, token = raw.partition(" ")
        return (token.strip(), "bearer") if scheme.lower() == "bearer" and token.strip() else ("", None)
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE and value:
            return value, "cookie"
    return "", None
