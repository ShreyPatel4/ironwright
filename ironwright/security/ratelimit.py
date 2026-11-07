"""The per email soft lock (enterprise-security.md item 3).

Ten failed sign-ins in 15 minutes lock an email for 15 minutes. Failures count by the
normalized email whether or not it has an account, so the lock reads the same either
way. While locked even the right password is refused. A good sign in clears the count,
and so does a password reset.

The per IP limit on every password door is api.LoginLimit, in memory, in front of this.
Each call here runs in its own short transaction, so a failure is still counted when
the sign in that caused it raises.
"""
from __future__ import annotations

import hashlib

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import store

FAILURES = 10
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60
LOCKED_DETAIL = "too many failed sign-ins for this email. Wait 15 minutes, or reset your password"

_t = store.auth_failures


class Locked(Exception):
    pass


def _key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def check(email: str, now: float) -> None:
    with store.engine().connect() as con:
        until = con.execute(sa.select(_t.c.locked_until).where(_t.c.key == _key(email))).scalar()
    if until is not None and until > now:
        raise Locked


def fail(email: str, now: float) -> bool:
    """Count one failure. True when this one locked the email."""
    key = _key(email)
    with store.engine().begin() as con:
        insert = pg_insert if con.dialect.name == "postgresql" else sqlite_insert
        con.execute(insert(_t).values(key=key, count=0, window_start=now, locked_until=0).on_conflict_do_nothing())
        stale = _t.c.window_start <= now - WINDOW_SECONDS
        # One UPDATE, so two racing failures cannot both read the same count.
        con.execute(_t.update().where(_t.c.key == key).values(
            count=sa.case((stale, 1), else_=_t.c.count + 1),
            window_start=sa.case((stale, now), else_=_t.c.window_start)))
        done = con.execute(_t.update().where(_t.c.key == key, _t.c.count >= FAILURES)
                           .values(count=0, window_start=now, locked_until=now + LOCK_SECONDS))
        return done.rowcount == 1


def clear(email: str) -> None:
    with store.engine().begin() as con:
        con.execute(_t.delete().where(_t.c.key == _key(email)))
