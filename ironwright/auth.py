"""The invite gate: local credential accounts for the console door.

Honest scope, stated everywhere it surfaces: accounts live as salted
PBKDF2 hashes in a file under the data root (user data, never in the
repo), the gate guards the CONSOLE door, and the API itself stays locally
open until the hosted build closes it with real authentication. Seeding
happens through `ironwright auth-set <email>`, which prompts for the
password so no secret ever lands in code, logs or history.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from .config import data_root

_ITERATIONS = 200_000
_SESSIONS: dict[str, str] = {}  # token -> email, process-lifetime only


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


def login(email: str, password: str) -> str | None:
    acct = _load().get(email.strip().lower())
    if not acct:
        return None
    if not secrets.compare_digest(_hash(password, bytes.fromhex(acct["salt"])), acct["hash"]):
        return None
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = email.strip().lower()
    return token


def me(token: str) -> dict[str, str] | None:
    email = _SESSIONS.get(token)
    if not email:
        return None
    acct = _load().get(email)
    if not acct:
        return None
    return {"email": email, "principal": acct["principal"]}


def logout(token: str) -> None:
    _SESSIONS.pop(token, None)
