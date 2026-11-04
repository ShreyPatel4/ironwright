"""An org's own model key: stored encrypted, used by llm.py, never shown again.

Fernet under IRONWRIGHT_SECRET_KEY. There is no built-in fallback secret. A hosted
server without one will not start, and a local one refuses to store a key. Only
provider and last 4 are readable without the secret, so reading a key's status
never decrypts anything.

The sealed text holds the org id too. A row moved to another org does not open.
Nothing here puts a key in an exception, a log line or the environment. An org's
key in the environment would be every org's key.
"""
from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

from ..config import setting
from ..llm import BYO_PROVIDERS
from . import service, usage

SERVER_ENVS = ("staging", "prod")
# Long enough that the last 4 give nothing away, and no character a header would refuse.
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{20,512}")
_keys = usage.org_llm_keys


class NoSecret(Exception):
    """This server has no usable IRONWRIGHT_SECRET_KEY."""


class Unreadable(Exception):
    """A key is stored and cannot be opened. It was sealed under another secret, or moved."""


def _fernet() -> Fernet:
    try:
        return Fernet(setting("SECRET_KEY") or "")
    except ValueError:
        raise NoSecret("this server has no usable IRONWRIGHT_SECRET_KEY, so it cannot store a key") from None


def check_startup() -> None:
    """Fail loud. A hosted server needs the secret, and any server that names one needs it to work."""
    raw = setting("SECRET_KEY")
    if not raw and setting("ENV", "local") not in SERVER_ENVS:
        return
    if not raw:
        raise SystemExit(f"refusing to start: IRONWRIGHT_ENV={setting('ENV')} needs IRONWRIGHT_SECRET_KEY. "
                         "Make one with Fernet.generate_key().")
    try:
        Fernet(raw)
    except ValueError:
        raise SystemExit("refusing to start: IRONWRIGHT_SECRET_KEY is not a Fernet key. "
                         "Make one with Fernet.generate_key().") from None


def store(org_id: str, provider: Any, key: Any, actor: str, now: float) -> dict[str, Any]:
    """Refusals name the rule and never echo the value: it may be a real key."""
    if provider not in BYO_PROVIDERS:
        raise service.Invalid(f"provider must be one of {', '.join(BYO_PROVIDERS)}")
    if not isinstance(key, str) or not _KEY_SHAPE.fullmatch(key):
        raise service.Invalid("that does not look like an API key: 20 to 512 characters, no spaces")
    sealed = _fernet().encrypt(f"{org_id}:{key}".encode()).decode()
    with usage.engine().begin() as con:
        con.execute(_keys.delete().where(_keys.c.org_id == org_id))
        con.execute(_keys.insert().values(org_id=org_id, provider=provider, sealed=sealed, last4=key[-4:],
                                          updated_at=now, updated_by=actor))
        usage.audit(con, org_id, actor, "llm_key.set", {"provider": provider, "last4": key[-4:]}, now)
    return status(org_id)


def status(org_id: str) -> dict[str, Any] | None:
    with usage.engine().connect() as con:
        row = con.execute(sa.select(_keys.c.provider, _keys.c.last4, _keys.c.updated_at, _keys.c.updated_by)
                          .where(_keys.c.org_id == org_id)).mappings().first()
    return dict(row) if row else None


def load(org_id: str) -> tuple[str, str] | None:
    """(provider, key), or None when the org has none. Raises Unreadable, never a guess."""
    with usage.engine().connect() as con:
        row = con.execute(sa.select(_keys.c.provider, _keys.c.sealed).where(_keys.c.org_id == org_id)).first()
    if row is None:
        return None
    try:
        owner, _, key = _fernet().decrypt(row.sealed.encode()).decode().partition(":")
    except (InvalidToken, NoSecret):
        raise Unreadable("the stored key cannot be opened with this server's secret") from None
    if owner != org_id:
        raise Unreadable("the stored key was sealed for another org")
    return row.provider, key


def remove(org_id: str, actor: str, now: float) -> bool:
    held = status(org_id)
    if held is None:
        return False
    with usage.engine().begin() as con:
        con.execute(_keys.delete().where(_keys.c.org_id == org_id))
        usage.audit(con, org_id, actor, "llm_key.remove", {"provider": held["provider"], "last4": held["last4"]}, now)
    return True
