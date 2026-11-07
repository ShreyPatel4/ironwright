"""Org scoped API tokens, for service accounts (enterprise-security.md item 7).

A token is iwt_ plus 256 random bits. It is shown once and stored as sha256. It speaks
as service:<id>, never as the admin who made it, and holds the member role in its org
and nothing else: so the gate keeps it off every admin write, and every /orgs/{slug}
route answers it 404, because no membership has its id. That is how it can never
approve anything, whatever its scopes say.

Scopes map to routes and deny by default:
  read          every GET outside /auth and /orgs, and the two POSTs that only read
  ask           Ask, and replaying an Ask
  run           a pipeline run, a job, and cancelling a job
  act:request   asking for a governed action; the policy engine and a human decide
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Any

import sqlalchemy as sa

from ..tenancy import db, usage
from ..tenancy.models import Ctx
from . import store

PREFIX = "iwt_"
TOUCH_SECONDS = 60
SCOPES: dict[str, frozenset[tuple[str, str]]] = {
    "read": frozenset({("POST", "/workspaces/{name}/lake/query"),
                       ("POST", "/workspaces/{name}/governance/check")}),
    "ask": frozenset({("POST", "/workspaces/{name}/ask"), ("POST", "/workspaces/{name}/ask/replay")}),
    "run": frozenset({("POST", "/workspaces/{name}/runs"), ("POST", "/jobs"), ("POST", "/jobs/{job_id}/cancel")}),
    "act:request": frozenset({("POST", "/workspaces/{name}/actions/{action}")}),
}
REFUSAL = "this API token's scopes do not allow this"

_t = store.api_tokens


class Invalid(ValueError):
    pass


def is_token(raw: str) -> bool:
    return raw.startswith(PREFIX)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def principal_of(token_id: str) -> str:
    return f"service:{token_id}"


def allows(scopes: frozenset[str], method: str, template: str | None) -> bool:
    if template is None:
        return False
    if method in ("GET", "HEAD") and "read" in scopes:
        return not template.startswith(("/auth/", "/orgs/"))
    return any((method, template) in SCOPES[s] for s in scopes if s in SCOPES)


def create(org_id: str, name: str, scopes: list[str], actor: str, now: float) -> dict[str, Any]:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise Invalid("a token needs a name of 1 to 80 characters")
    wanted = sorted(set(scopes or []))
    if not wanted or any(s not in SCOPES for s in wanted):
        raise Invalid(f"scopes must be some of {', '.join(SCOPES)}")
    raw = PREFIX + secrets.token_urlsafe(32)
    token_id = uuid.uuid4().hex
    with store.engine().begin() as con:
        con.execute(_t.insert().values(id=token_id, org_id=org_id, name=name, scopes=" ".join(wanted),
                                       token_hash=_hash(raw), created_by=actor, created_at=now))
        usage.audit(con, org_id, actor, "token.create", {"id": token_id, "name": name, "scopes": wanted}, now)
    return {"id": token_id, "name": name, "scopes": wanted, "token": raw, "created_at": now}


def listed(org_id: str) -> list[dict[str, Any]]:
    with store.engine().connect() as con:
        rows = con.execute(sa.select(_t).where(_t.c.org_id == org_id, _t.c.revoked_at.is_(None))
                           .order_by(_t.c.created_at)).mappings().all()
    return [{"id": r["id"], "name": r["name"], "scopes": r["scopes"].split(), "created_by": r["created_by"],
             "created_at": r["created_at"], "last_used": r["last_used"]} for r in rows]


def revoke(org_id: str, token_id: str, actor: str, now: float) -> bool:
    """False for an id this org does not have, another org's included."""
    with store.engine().begin() as con:
        done = con.execute(_t.update().where(_t.c.id == token_id, _t.c.org_id == org_id, _t.c.revoked_at.is_(None))
                           .values(revoked_at=now))
        if done.rowcount != 1:
            return False
        usage.audit(con, org_id, actor, "token.revoke", {"id": token_id}, now)
        return True


def resolve(raw: str, now: float) -> tuple[Ctx, frozenset[str]] | None:
    with store.engine().begin() as con:
        row = con.execute(sa.select(_t, db.orgs.c.slug).join(db.orgs, db.orgs.c.id == _t.c.org_id)
                          .where(_t.c.token_hash == _hash(raw), _t.c.revoked_at.is_(None))).mappings().first()
        if row is None:
            return None
        if row["last_used"] is None or now - row["last_used"] >= TOUCH_SECONDS:
            con.execute(_t.update().where(_t.c.id == row["id"]).values(last_used=now))
    ctx = Ctx(org_id=row["org_id"], org_slug=row["slug"], user_id=f"tok_{row['id']}", role="member",
              principal=principal_of(row["id"]))
    return ctx, frozenset(row["scopes"].split())
