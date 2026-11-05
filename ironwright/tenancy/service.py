"""Accounts, orgs, sessions and invites. Every rule about them lives here.

Time always comes in as an argument, so auth.py owns the one clock. Tokens are
stored as sha256 of 256 random bits: nothing to stretch, and a stolen table
holds no usable credential.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..config import UnsafePath, data_root, safe_child
from ..packs import list_packs
from . import db
from .models import DEV_ORG_ID, DEV_ORG_SLUG, ROLES, Ctx, principal_of

IDLE_SECONDS = 8 * 3600
INVITE_SECONDS = 7 * 24 * 3600
# A session only rewrites last_seen this often, so reads stay reads.
TOUCH_SECONDS = 60
# Who owns a scenario folder under the data root. Written once, at creation.
OWNER_FILE = ".ironwright-owner"

_ITERATIONS = 200_000
_SLUG = re.compile(r"[a-z][a-z0-9-]{2,39}")
_RESERVED_SLUGS = frozenset({DEV_ORG_SLUG})


class Invalid(ValueError):
    """Input a caller can fix."""


class EmailTaken(Exception):
    pass


class BadCredentials(Exception):
    pass


class NoSuchInvite(Exception):
    pass


def _id() -> str:
    return uuid.uuid4().hex


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_email(email: str) -> str:
    email = email.strip().lower()
    local, at, domain = email.partition("@")
    if not (at and local and domain) or len(email) > 320:
        raise Invalid("that does not look like an email address")
    return email


def _check_password(password: str) -> None:
    if len(password) < 8:
        raise Invalid("a password needs at least 8 characters")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest}"


def verify_password(password: str, stored: str) -> bool:
    _, iterations, salt, digest = stored.split("$")
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return secrets.compare_digest(got, digest)


# Checked when an email is unknown, so a miss costs the same as a wrong password.
_NOBODY = hash_password(secrets.token_urlsafe(16))


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    if not _SLUG.fullmatch(slug):
        slug = f"org-{slug}"[:40].strip("-")
    if not _SLUG.fullmatch(slug):
        slug = "org"
    return slug


def _free_slug(con: Connection, base: str) -> str:
    taken = con.execute(sa.select(db.orgs.c.id).where(db.orgs.c.slug == base)).first()
    if not taken and base not in _RESERVED_SLUGS:
        return base
    return f"{base[:35].rstrip('-')}-{secrets.token_hex(2)}"


def _orgs_of(con: Connection, user_id: str) -> list[dict[str, Any]]:
    """Oldest first. The first one is the user's default org."""
    rows = con.execute(
        sa.select(db.orgs.c.id, db.orgs.c.slug, db.orgs.c.name, db.memberships.c.role)
        .join(db.memberships, db.memberships.c.org_id == db.orgs.c.id)
        .where(db.memberships.c.user_id == user_id)
        .order_by(db.orgs.c.created_at, db.orgs.c.id)
    ).mappings().all()
    return [dict(r) for r in rows]


def _open_session(con: Connection, user_id: str, org_id: str, now: float) -> str:
    """Rotate: this user's older sessions go, and so does anything expired."""
    con.execute(db.sessions.delete().where(
        sa.or_(db.sessions.c.user_id == user_id, db.sessions.c.expires_at <= now)))
    token = secrets.token_urlsafe(32)
    con.execute(db.sessions.insert().values(token_hash=_token_hash(token), user_id=user_id,
                                            org_id=org_id, expires_at=now + IDLE_SECONDS,
                                            last_seen=now))
    return token


def _session_answer(con: Connection, token: str, user_id: str, org_id: str) -> dict[str, Any]:
    orgs = _orgs_of(con, user_id)
    org = next(o for o in orgs if o["id"] == org_id)
    email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == user_id)).scalar_one()
    return {"token": token, "email": email, "org_id": org_id, "org_slug": org["slug"],
            "user_id": user_id, "role": org["role"], "principal": principal_of(user_id),
            "orgs": [{"slug": o["slug"], "name": o["name"], "role": o["role"]} for o in orgs]}


def _decoy_answer(con: Connection, email: str, org_name: str, base: str) -> dict[str, Any]:
    """What a signup on a taken email answers: a new account's shape over ids and a token
    nobody stored. No password is checked and no row is written, so it opens nothing.
    The account's owner keeps their session and never knows."""
    user_id, slug = _id(), _free_slug(con, base)
    return {"token": secrets.token_urlsafe(32), "email": email, "org_id": _id(), "org_slug": slug,
            "user_id": user_id, "role": "owner", "principal": principal_of(user_id),
            "orgs": [{"slug": slug, "name": org_name, "role": "owner"}]}


def signup(email: str, password: str, org_name: str, now: float) -> dict[str, Any]:
    """The answer cannot say whether an email has an account, and neither can the slow
    part of the work: the password is hashed before anyone looks the email up."""
    email = clean_email(email)
    _check_password(password)
    org_name = org_name.strip()
    if not org_name or len(org_name) > 200:
        raise Invalid("an org name needs 1 to 200 characters")
    pw_hash = hash_password(password)
    base = slugify(org_name)
    # A lost race on the email or the slug lands here again and resolves itself.
    for _ in range(5):
        try:
            with db.engine().begin() as con:
                if con.execute(sa.select(db.users.c.id).where(db.users.c.email == email)).first():
                    return _decoy_answer(con, email, org_name, base)
                user_id, org_id = _id(), _id()
                con.execute(db.users.insert().values(id=user_id, email=email, pw_hash=pw_hash,
                                                     created_at=now))
                con.execute(db.orgs.insert().values(id=org_id, slug=_free_slug(con, base),
                                                    name=org_name, created_at=now))
                con.execute(db.memberships.insert().values(org_id=org_id, user_id=user_id,
                                                           role="owner"))
                return _session_answer(con, _open_session(con, user_id, org_id, now),
                                       user_id, org_id)
        except IntegrityError:
            continue
    raise EmailTaken


def login(email: str, password: str, now: float) -> dict[str, Any]:
    try:
        email = clean_email(email)
    except Invalid:
        email = ""
    with db.engine().begin() as con:
        user = con.execute(sa.select(db.users).where(db.users.c.email == email)).mappings().first()
        if not verify_password(password, user["pw_hash"] if user else _NOBODY) or not user:
            raise BadCredentials
        orgs = _orgs_of(con, user["id"])
        if not orgs:
            raise BadCredentials
        return _session_answer(con, _open_session(con, user["id"], orgs[0]["id"], now),
                               user["id"], orgs[0]["id"])


def resolve(token: str, now: float) -> Ctx | None:
    """The session behind a token, read fresh: a role change or removal applies at once."""
    if not token:
        return None
    hashed = _token_hash(token)
    with db.engine().begin() as con:
        row = con.execute(
            sa.select(db.sessions, db.orgs.c.slug, db.memberships.c.role)
            .join(db.orgs, db.orgs.c.id == db.sessions.c.org_id)
            .join(db.memberships, sa.and_(db.memberships.c.org_id == db.sessions.c.org_id,
                                          db.memberships.c.user_id == db.sessions.c.user_id),
                  isouter=True)
            .where(db.sessions.c.token_hash == hashed)
        ).mappings().first()
        if row is None:
            return None
        if row["expires_at"] <= now:
            con.execute(db.sessions.delete().where(db.sessions.c.token_hash == hashed))
            return None
        if row["role"] is None:
            return None
        if now - row["last_seen"] >= TOUCH_SECONDS:
            con.execute(db.sessions.update().where(db.sessions.c.token_hash == hashed)
                        .values(last_seen=now, expires_at=now + IDLE_SECONDS))
    return Ctx(org_id=row["org_id"], org_slug=row["slug"], user_id=row["user_id"],
               role=row["role"], principal=principal_of(row["user_id"]))


def describe(ctx: Ctx) -> dict[str, Any]:
    """The Ctx plus what the console shows about it."""
    with db.engine().connect() as con:
        email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == ctx.user_id)).scalar()
        orgs = _orgs_of(con, ctx.user_id)
    return {**ctx.__dict__, "email": email,
            "orgs": [{"slug": o["slug"], "name": o["name"], "role": o["role"]} for o in orgs]}


def revoke(token: str) -> None:
    with db.engine().begin() as con:
        con.execute(db.sessions.delete().where(db.sessions.c.token_hash == _token_hash(token)))


def role_in(user_id: str, slug: str) -> str | None:
    with db.engine().connect() as con:
        return con.execute(
            sa.select(db.memberships.c.role)
            .join(db.orgs, db.orgs.c.id == db.memberships.c.org_id)
            .where(db.memberships.c.user_id == user_id, db.orgs.c.slug == slug)
        ).scalar()


def switch(ctx: Ctx, slug: str, now: float) -> dict[str, Any] | None:
    with db.engine().begin() as con:
        org_id = next((o["id"] for o in _orgs_of(con, ctx.user_id) if o["slug"] == slug), None)
        if org_id is None:
            return None
        return _session_answer(con, _open_session(con, ctx.user_id, org_id, now),
                               ctx.user_id, org_id)


def invite(slug: str, email: str, role: str, now: float) -> str:
    """Returns the one copy of the token. A new invite replaces an older one."""
    email = clean_email(email)
    if role not in ROLES:
        raise Invalid(f"role must be one of {', '.join(ROLES)}")
    token = secrets.token_urlsafe(32)
    with db.engine().begin() as con:
        org_id = con.execute(sa.select(db.orgs.c.id).where(db.orgs.c.slug == slug)).scalar_one()
        con.execute(db.invites.delete().where(db.invites.c.org_id == org_id,
                                              db.invites.c.email == email))
        con.execute(db.invites.insert().values(token_hash=_token_hash(token), org_id=org_id,
                                               email=email, role=role,
                                               expires_at=now + INVITE_SECONDS))
    return token


def accept_invite(token: str, password: str, now: float) -> dict[str, Any]:
    """A new email sets its password here. A known email must prove the one it has."""
    hashed = _token_hash(token)
    with db.engine().begin() as con:
        row = con.execute(sa.select(db.invites).where(db.invites.c.token_hash == hashed)).mappings().first()
        if row is None or row["expires_at"] <= now:
            raise NoSuchInvite
        user = con.execute(sa.select(db.users).where(db.users.c.email == row["email"])).mappings().first()
        if user:
            if not verify_password(password, user["pw_hash"]):
                raise BadCredentials
            user_id = user["id"]
        else:
            _check_password(password)
            user_id = _id()
            con.execute(db.users.insert().values(id=user_id, email=row["email"],
                                                 pw_hash=hash_password(password), created_at=now))
        held = con.execute(sa.select(db.memberships.c.role).where(
            db.memberships.c.org_id == row["org_id"], db.memberships.c.user_id == user_id)).scalar()
        if held is None:
            con.execute(db.memberships.insert().values(org_id=row["org_id"], user_id=user_id,
                                                       role=row["role"]))
        elif ROLES.index(row["role"]) > ROLES.index(held):
            con.execute(db.memberships.update().where(
                db.memberships.c.org_id == row["org_id"], db.memberships.c.user_id == user_id
            ).values(role=row["role"]))
        con.execute(db.invites.delete().where(db.invites.c.token_hash == hashed))
        return _session_answer(con, _open_session(con, user_id, row["org_id"], now),
                               user_id, row["org_id"])


def seed_operator(email: str, password: str, now: float) -> None:
    """`ironwright auth-set`: an owner in the dev org. A new password ends old sessions."""
    email = clean_email(email)
    _check_password(password)
    pw_hash = hash_password(password)
    with db.engine().begin() as con:
        if not con.execute(sa.select(db.orgs.c.id).where(db.orgs.c.id == DEV_ORG_ID)).first():
            con.execute(db.orgs.insert().values(id=DEV_ORG_ID, slug=DEV_ORG_SLUG, name="Dev org",
                                                created_at=now))
        user_id = con.execute(sa.select(db.users.c.id).where(db.users.c.email == email)).scalar()
        if user_id:
            con.execute(db.users.update().where(db.users.c.id == user_id).values(pw_hash=pw_hash))
            con.execute(db.sessions.delete().where(db.sessions.c.user_id == user_id))
        else:
            user_id = _id()
            con.execute(db.users.insert().values(id=user_id, email=email, pw_hash=pw_hash,
                                                 created_at=now))
        held = con.execute(sa.select(db.memberships.c.role).where(
            db.memberships.c.org_id == DEV_ORG_ID, db.memberships.c.user_id == user_id)).scalar()
        if held is None:
            con.execute(db.memberships.insert().values(org_id=DEV_ORG_ID, user_id=user_id,
                                                       role="owner"))


def can_see_workspace(ctx: Ctx, name: str) -> bool:
    """The one rule for whether an org can see a workspace, by name.

    The app gate applies it to every {name} in a path. A handler that takes a
    workspace from a request body must call it too, because the gate cannot see
    a body. Demo packs yes, the org's own yes, everything else no.
    """
    if name in list_packs():
        return True
    try:
        if safe_child(data_root() / "tenants" / ctx.org_id / "workspaces", name).is_dir():
            return True
        marker = safe_child(data_root(), name) / OWNER_FILE
    except UnsafePath:
        return False
    owner = marker.read_text().strip() if marker.is_file() and not marker.is_symlink() else None
    # A folder with no owner predates tenancy, or never existed. Both are the
    # dev org's to answer for, and nobody else's.
    return owner == ctx.org_id if owner else ctx.org_id == DEV_ORG_ID


def any_users() -> bool:
    with db.engine().connect() as con:
        return con.execute(sa.select(db.users.c.id).limit(1)).first() is not None
