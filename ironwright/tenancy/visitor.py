"""The demo door: a visitor tries the product without an account.

IRONWRIGHT_DEMO_ORG names one org. Each visit gets its own viewer in it, with a session and
no usable password, so two visitors never sign each other out and nobody can sign in as one.
A viewer reads the demo packs and that org's workspaces and writes nothing; the app gate
already holds a viewer to that. The org is made on the first visit if nobody seeded it.
Visitors whose sessions have all expired are swept on the next visit.
"""
from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..config import setting
from . import db, service

DOMAIN = "visitors.invalid"  # .invalid never resolves, and clean_email never hands it out
ORG_NAME = "Ironwright demo"
_SLUG = re.compile(r"[a-z][a-z0-9-]{2,39}")


def demo_org() -> str | None:
    slug = (setting("DEMO_ORG") or "").strip()
    if not slug:
        return None
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"IRONWRIGHT_DEMO_ORG must be an org slug like ironwright-demo, not {slug!r}")
    return slug


def _sweep(con: Connection, now: float) -> None:
    live = sa.select(db.sessions.c.user_id).where(db.sessions.c.expires_at > now)
    gone = [r[0] for r in con.execute(sa.select(db.users.c.id).where(
        db.users.c.email.like(f"%@{DOMAIN}"), db.users.c.id.not_in(live)))]
    if gone:
        con.execute(db.sessions.delete().where(db.sessions.c.user_id.in_(gone)))
        con.execute(db.memberships.delete().where(db.memberships.c.user_id.in_(gone)))
        con.execute(db.users.delete().where(db.users.c.id.in_(gone)))


def _org_id(con: Connection, slug: str, now: float) -> str:
    found = con.execute(sa.select(db.orgs.c.id).where(db.orgs.c.slug == slug)).scalar()
    if found:
        return found
    org_id = service._id()
    con.execute(db.orgs.insert().values(id=org_id, slug=slug, name=ORG_NAME, created_at=now))
    return org_id


def visit(slug: str, now: float) -> dict[str, Any]:
    for _ in range(3):  # two first visitors racing to make the org: the loser joins it
        try:
            with db.engine().begin() as con:
                _sweep(con, now)
                org_id = _org_id(con, slug, now)
                user_id = service._id()
                con.execute(db.users.insert().values(id=user_id, email=f"visitor-{user_id[:12]}@{DOMAIN}",
                                                     pw_hash=service._NOBODY, created_at=now))
                con.execute(db.memberships.insert().values(org_id=org_id, user_id=user_id, role="viewer"))
                token = service._open_session(con, user_id, org_id, now)
                return {**service._session_answer(con, token, user_id, org_id), "demo": True}
        except IntegrityError:
            continue
    raise RuntimeError("the demo org could not be joined")
