"""TOTP (RFC 6238: HMAC-SHA1, 30 second steps, 6 digits) and recovery codes, stdlib only.

A code is good for the step before, the step of, and the step after now, and only for a
step later than the last one used, so a code seen over a shoulder cannot be replayed.
Secrets are sealed with Fernet under IRONWRIGHT_SECRET_KEY, the way byokey seals keys,
with the user id inside, so a row moved to another user does not open. No secret, no
enrolment. Recovery codes are kept as sha256 and work once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from ..config import setting

STEP = 30
DIGITS = 6
RECOVERY_CODES = 10
ISSUER = "Ironwright"


class NoSecret(Exception):
    """This server has no usable IRONWRIGHT_SECRET_KEY."""


def totp(secret: bytes, step: int) -> str:
    mac = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def new_secret() -> bytes:
    return secrets.token_bytes(20)


def b32(secret: bytes) -> str:
    return base64.b32encode(secret).decode()


def uri(secret: bytes, email: str) -> str:
    """What an authenticator app takes, typed in or from a QR code."""
    return (f"otpauth://totp/{ISSUER}:{quote(email)}?secret={b32(secret)}"
            f"&issuer={ISSUER}&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def match(secret: bytes, code: str, now: float, last_step: int) -> int | None:
    """The step this code is for, or None. Never a step at or before last_step."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    here = int(now // STEP)
    for step in (here - 1, here, here + 1):
        if step > last_step and hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def _fernet() -> Fernet:
    try:
        return Fernet(setting("SECRET_KEY") or "")
    except ValueError:
        raise NoSecret("this server has no usable IRONWRIGHT_SECRET_KEY, so two-step sign in "
                       "cannot be set up") from None


def seal(user_id: str, secret: bytes) -> str:
    return _fernet().encrypt(json.dumps({"user_id": user_id, "secret": b32(secret)}).encode()).decode()


def unseal(user_id: str, sealed: str) -> bytes | None:
    try:
        opened = json.loads(_fernet().decrypt(sealed.encode()))
    except (InvalidToken, ValueError, NoSecret):
        return None
    return base64.b32decode(opened["secret"]) if opened.get("user_id") == user_id else None


def recovery_codes() -> list[str]:
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(RECOVERY_CODES)]


def code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()
