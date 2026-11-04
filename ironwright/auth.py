"""Real identity for the platform API, and the local accounts behind it.

Accounts live as salted PBKDF2 hashes in a file under the data root (user
data, never in the repo). Sessions live server side for the life of the
process, expire after 8 hours idle and rotate on login. The principal on a
request comes from its bearer token and nowhere else. Seeding happens through
`ironwright auth-set <email>`, which prompts for the password so no secret
ever lands in code, logs or history.

IRONWRIGHT_DEV_OPEN=1 keeps the old open behavior for tests and demo-check:
no token needed, and the X-Principal header names the caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import data_root

CONSOLE_PRINCIPAL = "user:console"
IDLE_SECONDS = 8 * 3600
# "/" only redirects to the console. The console itself is a mount, which
# route dependencies never reach.
PUBLIC_ROUTES = frozenset({"/auth/status", "/auth/login", "/"})

_ITERATIONS = 200_000
_SESSIONS: dict[str, dict[str, Any]] = {}  # token -> email, last_seen; process-lifetime only
_LOCK = threading.Lock()
_now = time.time


def dev_open() -> bool:
    return os.environ.get("IRONWRIGHT_DEV_OPEN") == "1"


def _is_agent(principal: str) -> bool:
    return principal.strip().lower().startswith("agent:")


def _path() -> Path:
    return data_root() / "auth" / "credentials.json"


def _load() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS).hex()


def principal_for(email: str) -> str:
    local = email.split("@", 1)[0].lower().replace(".", "")
    return f"user:{local}"


def set_account(email: str, password: str, principal: str | None = None) -> None:
    email = email.strip().lower()
    if "@" not in email or len(password) < 8:
        raise ValueError("an email and a password of at least 8 characters")
    if principal and _is_agent(principal):
        raise ValueError("agent principals cannot have accounts")
    accounts = _load()
    salt = secrets.token_bytes(16)
    accounts[email] = {"salt": salt.hex(), "hash": _hash(password, salt),
                       "principal": principal or principal_for(email)}
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(accounts, indent=1, sort_keys=True))
    p.chmod(0o600)


def seeded() -> bool:
    return bool(_load())


def _expired(session: dict[str, Any]) -> bool:
    return _now() - session["last_seen"] >= IDLE_SECONDS


def login(email: str, password: str) -> str | None:
    email = email.strip().lower()
    acct = _load().get(email)
    if not acct or _is_agent(acct["principal"]):
        return None
    if not secrets.compare_digest(_hash(password, bytes.fromhex(acct["salt"])), acct["hash"]):
        return None
    token = secrets.token_urlsafe(32)
    with _LOCK:
        for old, session in list(_SESSIONS.items()):
            if session["email"] == email or _expired(session):
                del _SESSIONS[old]
        _SESSIONS[token] = {"email": email, "last_seen": _now()}
    return token


def me(token: str) -> dict[str, str] | None:
    with _LOCK:
        session = _SESSIONS.get(token)
        if not session:
            return None
        if _expired(session):
            del _SESSIONS[token]
            return None
        session["last_seen"] = _now()
    email = session["email"]
    acct = _load().get(email)
    if not acct or _is_agent(acct["principal"]):
        return None
    return {"email": email, "principal": acct["principal"]}


def logout(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(token, None)


def bearer_token(headers: Headers) -> str:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def current_principal(request: Request) -> str:
    """Who is calling. The FastAPI dependency every principal-aware route uses."""
    if dev_open():
        return request.headers.get("x-principal", CONSOLE_PRINCIPAL)
    info = me(bearer_token(request.headers))
    if not info:
        raise HTTPException(401, "no session", headers={"WWW-Authenticate": "Bearer"})
    return info["principal"]


def require_session(request: Request) -> None:
    """The app-wide gate. Default deny: a new route is closed unless listed above."""
    if getattr(request.scope.get("route"), "path", None) not in PUBLIC_ROUTES:
        current_principal(request)


class BindPrincipalHeader:
    """Makes X-Principal the session's principal, whatever the client sent.

    Only asof.py still reads the header. This stops that route trusting the
    client until it moves to current_principal, then this class can go.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not dev_open():
            headers = [h for h in scope["headers"] if h[0] != b"x-principal"]
            info = me(bearer_token(Headers(scope=scope)))
            if info:
                headers.append((b"x-principal", info["principal"].encode()))
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)
