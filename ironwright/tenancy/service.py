"""Accounts, orgs, sessions and invites. Every rule about them lives here.

Time always comes in as an argument, so auth.py owns the one clock. Tokens are
stored as sha256 of 256 random bits: nothing to stretch, and a stolen table
holds no usable credential.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..config import (
    ORG_ID,
    UnsafePath,
    data_root,
    lake_uri,
    private_dir,
    safe_child,
    tenant_dir,
    workspaces_root,
)
from ..packs import list_packs
from ..security import cookies, mfa, outbox, passwords, ratelimit
from ..security import store as sec
from . import db, usage
from .models import DEV_ORG_ID, DEV_ORG_SLUG, ROLES, Ctx, at_least, principal_of

IDLE_SECONDS = 8 * 3600
# However busy a session is, it ends this long after sign in (enterprise-security.md item 2).
ABSOLUTE_SECONDS = 12 * 3600
# Invite, verify and reset links: single use, 30 minutes, only the hash kept (item 6).
LINK_SECONDS = 30 * 60
INVITE_SECONDS = LINK_SECONDS
RESET_MAILS = 3  # per email per 15 minutes
# A session only rewrites last_seen this often, so reads stay reads.
TOUCH_SECONDS = 60
# Who owns a scenario folder under the data root. Written once, at creation.
OWNER_FILE = ".ironwright-owner"

_ITERATIONS = 200_000
_SLUG = re.compile(r"[a-z][a-z0-9-]{2,39}")
_RESERVED_SLUGS = frozenset({DEV_ORG_SLUG})
# jobs.workspace cannot be null, and an org job has no workspace. Not a safe name, so
# it cannot be a real workspace or come in through POST /jobs.
ORG_JOBS = "(org)"
_RETRY_WAITS = (1, 4)  # the object store client tries once, with a short timeout

# Contract 1's orgs table stays as written, so "being deleted" lives here. Nothing
# but the owner route writes a row, and the delete job does nothing without one.
org_lifecycle = sa.Table(
    "org_lifecycle", usage.metadata,
    sa.Column("org_id", sa.String(32), primary_key=True),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("requested_by", sa.String(256), nullable=False),
    sa.Column("requested_at", sa.Float, nullable=False),
    sa.Column("member_ids", sa.Text, nullable=False),  # memberships go first, and the job still needs them
)


class Invalid(ValueError):
    """Input a caller can fix."""


class EmailTaken(Exception):
    pass


class BadCredentials(Exception):
    pass


class NoSuchInvite(Exception):
    pass


class NoSuchLink(Exception):
    pass


class NoSuchMember(Exception):
    pass


class LastOwner(Exception):
    pass


class Forbidden(Exception):
    """The caller's role does not allow this."""


class NotDeleting(Exception):
    """Nobody asked for this org to be deleted."""


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
    """Where a password is set. Never at sign in, so nobody is locked out by a new rule."""
    try:
        passwords.check(password)
    except passwords.Weak as e:
        raise Invalid(str(e)) from None


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


def _sweep_sessions(con: Connection, now: float) -> None:
    """Expired sessions go, and so does any info row whose session is gone."""
    con.execute(db.sessions.delete().where(sa.or_(
        db.sessions.c.expires_at <= now,
        db.sessions.c.token_hash.in_(sa.select(sec.session_info.c.token_hash)
                                     .where(sec.session_info.c.created_at <= now - ABSOLUTE_SECONDS)))))
    con.execute(sec.session_info.delete().where(
        sec.session_info.c.token_hash.not_in(sa.select(db.sessions.c.token_hash))))


def _age_of(con: Connection, token: str) -> float | None:
    """When the session behind this token signed in, if it is one."""
    return con.execute(sa.select(sec.session_info.c.created_at)
                       .where(sec.session_info.c.token_hash == _token_hash(token or ""))).scalar()


def _open_session(con: Connection, user_id: str, org_id: str, now: float,
                  seen: dict[str, str] | None = None, since: float | None = None) -> str:
    """A new id every time. The session the request came in on, if any, goes: that is
    the rotation on sign in and org switch. Other devices stay signed in. A rotation
    that is not a sign in passes since, so the 12 hour cap still counts from the
    password and switching orgs cannot stretch it."""
    seen = seen or {}
    if seen.get("replacing"):
        _end(con, db.sessions.c.token_hash == _token_hash(seen["replacing"]))
    _sweep_sessions(con, now)
    token = secrets.token_urlsafe(32)
    hashed = _token_hash(token)
    con.execute(db.sessions.insert().values(token_hash=hashed, user_id=user_id,
                                            org_id=org_id, expires_at=now + IDLE_SECONDS,
                                            last_seen=now))
    con.execute(sec.session_info.insert().values(
        token_hash=hashed, sid=secrets.token_hex(16), user_id=user_id, org_id=org_id, created_at=since or now,
        ip=(seen.get("ip") or "")[:64], user_agent=(seen.get("user_agent") or "")[:256]))
    return token


def _end(con: Connection, where: Any) -> None:
    """End sessions and their info rows together."""
    hashes = sa.select(db.sessions.c.token_hash).where(where)
    con.execute(sec.session_info.delete().where(sec.session_info.c.token_hash.in_(hashes)))
    con.execute(db.sessions.delete().where(where))


def _session_answer(con: Connection, token: str, user_id: str, org_id: str) -> dict[str, Any]:
    orgs = _orgs_of(con, user_id)
    org = next(o for o in orgs if o["id"] == org_id)
    email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == user_id)).scalar_one()
    required = con.execute(sa.select(sec.org_security.c.require_mfa).where(sec.org_security.c.org_id == org_id)).scalar()
    return {"token": token, "csrf": cookies.csrf_of(token), "email": email, "org_id": org_id,
            "mfa_setup_required": bool(required) and not mfa_enabled(con, user_id),
            "org_slug": org["slug"], "user_id": user_id, "role": org["role"], "principal": principal_of(user_id),
            "orgs": [{"slug": o["slug"], "name": o["name"], "role": o["role"]} for o in orgs]}


def _decoy_answer(con: Connection, email: str, org_name: str, base: str) -> dict[str, Any]:
    """What a signup on a taken email answers: a new account's shape over ids and a token
    nobody stored. No password is checked and no row is written, so it opens nothing.
    The account's owner keeps their sessions and gets a mail saying someone tried."""
    user_id, slug, token = _id(), _free_slug(con, base), secrets.token_urlsafe(32)
    return {"token": token, "csrf": cookies.csrf_of(token), "email": email, "org_id": _id(),
            "mfa_setup_required": False, "org_slug": slug,
            "user_id": user_id, "role": "owner", "principal": principal_of(user_id),
            "orgs": [{"slug": slug, "name": org_name, "role": "owner"}]}


def signup(email: str, password: str, org_name: str, now: float,
           seen: dict[str, str] | None = None) -> dict[str, Any]:
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
            answer, mail = _signup_once(email, pw_hash, org_name, base, now, seen)
        except IntegrityError:
            continue
        outbox.deliver([mail])
        return answer
    raise EmailTaken


def _signup_once(email: str, pw_hash: str, org_name: str, base: str, now: float,
                 seen: dict[str, str] | None) -> tuple[dict[str, Any], tuple[str, str, str]]:
    """The mail goes to the address either way. Only its owner can tell the two apart."""
    with sec.engine().begin() as con:
        if con.execute(sa.select(db.users.c.id).where(db.users.c.email == email)).first():
            return _decoy_answer(con, email, org_name, base), (email, *_taken_mail())
        user_id, org_id = _id(), _id()
        con.execute(db.users.insert().values(id=user_id, email=email, pw_hash=pw_hash,
                                             created_at=now))
        con.execute(db.orgs.insert().values(id=org_id, slug=_free_slug(con, base),
                                            name=org_name, created_at=now))
        con.execute(db.memberships.insert().values(org_id=org_id, user_id=user_id,
                                                   role="owner"))
        usage.audit(con, org_id, principal_of(user_id), "auth.signup", _where(seen), now)
        verify = _new_link(con, "verify", user_id, now)
        return (_session_answer(con, _open_session(con, user_id, org_id, now, seen), user_id, org_id),
                (email, *_verify_mail(verify)))


def _taken_mail() -> tuple[str, str]:
    return ("Someone tried to sign up with your email",
            ("Someone tried to make a new Ironwright account with this email. You already have one, so "
            f"nothing changed. If it was you, sign in at {outbox.link('')}\n"
            "If you forgot your password, use the reset link on the sign in page."))


def _verify_mail(token: str) -> tuple[str, str]:
    return ("Confirm your email",
            (f"Open this link to confirm your email for Ironwright:\n{outbox.link('verify=' + token)}\n"
            "It works once and expires in 30 minutes."))


# ---------------------------------------------------------------- links

def _new_link(con: Connection, kind: str, user_id: str, now: float) -> str:
    """One live link of a kind per user. A new one replaces the old."""
    t = sec.auth_links
    con.execute(t.delete().where(t.c.user_id == user_id, t.c.kind == kind))
    token = secrets.token_urlsafe(32)
    con.execute(t.insert().values(token_hash=_token_hash(token), kind=kind, user_id=user_id,
                                  expires_at=now + LINK_SECONDS))
    return token


def _take_link(con: Connection, kind: str, token: str, now: float) -> str | None:
    """Single use: whoever deletes the row has it. An expired one goes too."""
    t = sec.auth_links
    hashed = _token_hash(token or "")
    row = con.execute(sa.select(t).where(t.c.token_hash == hashed, t.c.kind == kind)).mappings().first()
    if row is None:
        return None
    gone = con.execute(t.delete().where(t.c.token_hash == hashed)).rowcount == 1
    return row["user_id"] if gone and row["expires_at"] > now else None


def _mark_verified(con: Connection, user_id: str, now: float) -> None:
    f = sec.user_flags
    con.execute(f.delete().where(f.c.user_id == user_id))
    con.execute(f.insert().values(user_id=user_id, email_verified_at=now))


def email_verified(con: Connection, user_id: str) -> bool:
    f = sec.user_flags
    return con.execute(sa.select(f.c.email_verified_at).where(f.c.user_id == user_id)).scalar() is not None


def verify_email(token: str, now: float) -> None:
    with sec.engine().begin() as con:
        user_id = _take_link(con, "verify", token, now)
        if user_id is None:
            raise NoSuchLink
        _mark_verified(con, user_id, now)
        audit_user(con, user_id, principal_of(user_id), "auth.email_verified", {}, now)


def resend_verify(user_id: str, now: float) -> None:
    with sec.engine().begin() as con:
        email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == user_id)).scalar_one()
        if email_verified(con, user_id):
            return
        token = _new_link(con, "verify", user_id, now)
    outbox.deliver([(email, *_verify_mail(token))])


def request_reset(email: str, now: float) -> None:
    """Says nothing about whether the email has an account, and mails nobody if not."""
    try:
        email = clean_email(email)
    except Invalid:
        return
    if not ratelimit.allow("reset", email, RESET_MAILS, now):
        return
    with sec.engine().begin() as con:
        user_id = con.execute(sa.select(db.users.c.id).where(db.users.c.email == email)).scalar()
        if user_id is None:
            return
        token = _new_link(con, "reset", user_id, now)
    outbox.deliver([(email, "Reset your password",
                     (f"Someone asked to reset the password for this Ironwright account. If it was you, "
                     f"open this link:\n{outbox.link('reset=' + token)}\n"
                     "It works once and expires in 30 minutes. If it was not you, you can ignore this mail."))])


def reset_password(token: str, password: str, now: float) -> None:
    """The policy first, so a weak password does not spend the link. Every session ends,
    the lock lifts, and the email counts as verified: the link proved the inbox."""
    _check_password(password)
    pw_hash = hash_password(password)
    with sec.engine().begin() as con:
        user_id = _take_link(con, "reset", token, now)
        if user_id is None:
            raise NoSuchLink
        email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == user_id)).scalar_one()
        con.execute(db.users.update().where(db.users.c.id == user_id).values(pw_hash=pw_hash))
        _end(con, db.sessions.c.user_id == user_id)
        con.execute(sec.mfa_tickets.delete().where(sec.mfa_tickets.c.user_id == user_id))
        _mark_verified(con, user_id, now)
        audit_user(con, user_id, principal_of(user_id), "auth.password_reset", {}, now)
    ratelimit.clear(email)


def audit_user(con: Connection, user_id: str, actor: str, action: str, detail: dict[str, Any],
               now: float) -> None:
    """An auth event goes into the chain of every org the user is in: each org's admins
    see what happens to their people."""
    for org in _orgs_of(con, user_id):
        usage.audit(con, org["id"], actor, action, detail, now)


def _where(seen: dict[str, str] | None) -> dict[str, str]:
    return {"ip": (seen or {}).get("ip", "")}


def failed_sign_in(email: str, user_id: str | None, now: float, seen: dict[str, str] | None) -> None:
    """Count it toward the lock and write it down, in a transaction of its own."""
    locked = ratelimit.fail(email, now)
    if user_id is None:
        return
    with sec.engine().begin() as con:
        audit_user(con, user_id, "anonymous", "auth.fail", _where(seen), now)
        if locked:
            audit_user(con, user_id, "anonymous", "auth.lock", {**_where(seen), "minutes": 15}, now)


def login(email: str, password: str, now: float, seen: dict[str, str] | None = None) -> dict[str, Any]:
    """Locked emails are refused before any password is checked, known or not."""
    try:
        email = clean_email(email)
    except Invalid:
        email = ""
    ratelimit.check(email, now)
    with sec.engine().begin() as con:
        user = con.execute(sa.select(db.users).where(db.users.c.email == email)).mappings().first()
        good = verify_password(password, user["pw_hash"] if user else _NOBODY) and user is not None
        orgs = _orgs_of(con, user["id"]) if good else []
        if orgs and mfa_enabled(con, user["id"]):
            # The count clears only once the code is in, or wrong codes could never lock.
            return _mfa_ticket(con, user["id"], orgs[0]["id"], now)
        if orgs:
            answer = _session_answer(con, _open_session(con, user["id"], orgs[0]["id"], now, seen),
                                     user["id"], orgs[0]["id"])
            audit_user(con, user["id"], principal_of(user["id"]), "auth.login", _where(seen), now)
    if not orgs:
        failed_sign_in(email, user["id"] if user else None, now, seen)
        raise BadCredentials
    ratelimit.clear(email)
    return answer


def resolve(token: str, now: float) -> Ctx | None:
    """The session behind a token, read fresh: a role change or removal applies at once."""
    if not token:
        return None
    hashed = _token_hash(token)
    with sec.engine().begin() as con:
        row = con.execute(
            sa.select(db.sessions, db.orgs.c.slug, db.memberships.c.role, sec.session_info.c.created_at)
            .join(db.orgs, db.orgs.c.id == db.sessions.c.org_id)
            .join(sec.session_info, sec.session_info.c.token_hash == db.sessions.c.token_hash, isouter=True)
            .join(db.memberships, sa.and_(db.memberships.c.org_id == db.sessions.c.org_id,
                                          db.memberships.c.user_id == db.sessions.c.user_id),
                  isouter=True)
            .where(db.sessions.c.token_hash == hashed)
        ).mappings().first()
        if row is None:
            return None
        if (row["expires_at"] <= now or row["created_at"] is None
                or now - row["created_at"] >= ABSOLUTE_SECONDS):
            _end(con, db.sessions.c.token_hash == hashed)
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
    with sec.engine().connect() as con:
        email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == ctx.user_id)).scalar()
        orgs = _orgs_of(con, ctx.user_id)
        verified = email_verified(con, ctx.user_id)
    return {**ctx.__dict__, "email": email, "email_verified": verified,
            "orgs": [{"slug": o["slug"], "name": o["name"], "role": o["role"]} for o in orgs]}


def revoke(token: str) -> None:
    with sec.engine().begin() as con:
        _end(con, db.sessions.c.token_hash == _token_hash(token))


def list_sessions(user_id: str, current: str, now: float) -> list[dict[str, Any]]:
    """Every live session of this user, in every org. The id is not the token."""
    info = sec.session_info
    with sec.engine().connect() as con:
        rows = con.execute(
            sa.select(info.c.sid, info.c.token_hash, info.c.created_at, info.c.ip, info.c.user_agent,
                      db.sessions.c.last_seen, db.orgs.c.slug)
            .join(db.sessions, db.sessions.c.token_hash == info.c.token_hash)
            .join(db.orgs, db.orgs.c.id == db.sessions.c.org_id)
            .where(info.c.user_id == user_id, db.sessions.c.expires_at > now,
                   info.c.created_at > now - ABSOLUTE_SECONDS)
            .order_by(info.c.created_at.desc())).mappings().all()
    mine = _token_hash(current) if current else ""
    return [{"id": r["sid"], "org_slug": r["slug"], "created_at": r["created_at"], "last_seen": r["last_seen"],
             "ip": r["ip"], "user_agent": r["user_agent"], "current": r["token_hash"] == mine} for r in rows]


def revoke_session(user_id: str, sid: str) -> bool:
    """False when no session of this user has that id, someone else's included."""
    info = sec.session_info
    with sec.engine().begin() as con:
        hashed = con.execute(sa.select(info.c.token_hash).where(info.c.sid == sid, info.c.user_id == user_id)).scalar()
        if hashed is None:
            return False
        _end(con, db.sessions.c.token_hash == hashed)
        return True


def revoke_all(user_id: str, actor: str, now: float) -> None:
    """Sign out everywhere."""
    with sec.engine().begin() as con:
        _end(con, db.sessions.c.user_id == user_id)
        audit_user(con, user_id, actor, "auth.signout_all", {}, now)


def role_in(user_id: str, slug: str) -> str | None:
    with sec.engine().connect() as con:
        return con.execute(
            sa.select(db.memberships.c.role)
            .join(db.orgs, db.orgs.c.id == db.memberships.c.org_id)
            .where(db.memberships.c.user_id == user_id, db.orgs.c.slug == slug)
        ).scalar()


def switch(ctx: Ctx, slug: str, now: float, seen: dict[str, str] | None = None) -> dict[str, Any] | None:
    with sec.engine().begin() as con:
        org_id = next((o["id"] for o in _orgs_of(con, ctx.user_id) if o["slug"] == slug), None)
        if org_id is None:
            return None
        since = _age_of(con, (seen or {}).get("replacing", ""))
        return _session_answer(con, _open_session(con, ctx.user_id, org_id, now, seen, since),
                               ctx.user_id, org_id)


def invite(slug: str, email: str, role: str, now: float, actor: str = "system") -> str:
    """Returns the one copy of the token. A new invite replaces an older one."""
    email = clean_email(email)
    if role not in ROLES:
        raise Invalid(f"role must be one of {', '.join(ROLES)}")
    token = secrets.token_urlsafe(32)
    with sec.engine().begin() as con:
        org_id = con.execute(sa.select(db.orgs.c.id).where(db.orgs.c.slug == slug)).scalar_one()
        con.execute(db.invites.delete().where(db.invites.c.org_id == org_id,
                                              db.invites.c.email == email))
        con.execute(db.invites.insert().values(token_hash=_token_hash(token), org_id=org_id,
                                               email=email, role=role,
                                               expires_at=now + INVITE_SECONDS))
        usage.audit(con, org_id, actor, "member.invite", {"role": role}, now)
        name = con.execute(sa.select(db.orgs.c.name).where(db.orgs.c.id == org_id)).scalar_one()
    outbox.deliver([(email, f"You are invited to {name} on Ironwright",
                     (f"You are invited to join {name} on Ironwright as {role}. Open this link to accept:\n"
                     f"{outbox.link('invite=' + token)}\nIt works once and expires in 30 minutes."))])
    return token


class _WrongPassword(BadCredentials):
    def __init__(self, email: str, user_id: str) -> None:
        super().__init__()
        self.email, self.user_id = email, user_id


def accept_invite(token: str, password: str, now: float, seen: dict[str, str] | None = None) -> dict[str, Any]:
    """A new email sets its password here. A known email must prove the one it has, and
    a wrong one counts toward the lock like any sign in."""
    try:
        return _accept_invite(token, password, now, seen)
    except _WrongPassword as e:
        failed_sign_in(e.email, e.user_id, now, seen)
        raise BadCredentials from None


def _accept_invite(token: str, password: str, now: float, seen: dict[str, str] | None) -> dict[str, Any]:
    hashed = _token_hash(token)
    with sec.engine().begin() as con:
        row = con.execute(sa.select(db.invites).where(db.invites.c.token_hash == hashed)).mappings().first()
        if row is None:
            raise NoSuchInvite
        # Then look again under the org's lock. Marking an org for deletion deletes its
        # invites, and nobody may join once that has committed.
        _lock_org(con, row["org_id"])
        row = con.execute(sa.select(db.invites).where(db.invites.c.token_hash == hashed)).mappings().first()
        if row is None or row["expires_at"] <= now:
            raise NoSuchInvite
        user = con.execute(sa.select(db.users).where(db.users.c.email == row["email"])).mappings().first()
        if user:
            ratelimit.check(row["email"], now)
            if not verify_password(password, user["pw_hash"]):
                raise _WrongPassword(row["email"], user["id"])
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
        usage.audit(con, row["org_id"], principal_of(user_id), "member.join", {"user_id": user_id, "role": row["role"]}, now)
        if mfa_enabled(con, user_id):
            return _mfa_ticket(con, user_id, row["org_id"], now)
        return _session_answer(con, _open_session(con, user_id, row["org_id"], now, seen),
                               user_id, row["org_id"])


def seed_operator(email: str, password: str, now: float) -> None:
    """`ironwright auth-set`: an owner in the dev org. A new password ends old sessions."""
    email = clean_email(email)
    _check_password(password)
    pw_hash = hash_password(password)
    with sec.engine().begin() as con:
        if not con.execute(sa.select(db.orgs.c.id).where(db.orgs.c.id == DEV_ORG_ID)).first():
            con.execute(db.orgs.insert().values(id=DEV_ORG_ID, slug=DEV_ORG_SLUG, name="Dev org",
                                                created_at=now))
        user_id = con.execute(sa.select(db.users.c.id).where(db.users.c.email == email)).scalar()
        if user_id:
            con.execute(db.users.update().where(db.users.c.id == user_id).values(pw_hash=pw_hash))
            _end(con, db.sessions.c.user_id == user_id)
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
    with sec.engine().connect() as con:
        return con.execute(sa.select(db.users.c.id).limit(1)).first() is not None


# ---------------------------------------------------------------- members

def list_members(org_id: str, now: float) -> dict[str, Any]:
    with sec.engine().connect() as con:
        people = con.execute(
            sa.select(db.users.c.id, db.users.c.email, db.memberships.c.role)
            .join(db.memberships, db.memberships.c.user_id == db.users.c.id)
            .where(db.memberships.c.org_id == org_id).order_by(db.users.c.email)).all()
        invites = con.execute(
            sa.select(db.invites.c.email, db.invites.c.role, db.invites.c.expires_at)
            .where(db.invites.c.org_id == org_id, db.invites.c.expires_at > now)
            .order_by(db.invites.c.email)).all()
    return {"members": [{"user_id": i, "email": e, "role": r} for i, e, r in people],
            "invites": [{"email": e, "role": r, "expires_at": x} for e, r, x in invites]}


def _lock_org(con: Connection, org_id: str) -> None:
    """A write that changes nothing, first in the transaction: the org's row on Postgres,
    the one write lock on SQLite. So "how many owners" and the change are a single step,
    and two owners demoting each other cannot both read two."""
    con.execute(db.orgs.update().where(db.orgs.c.id == org_id).values(name=db.orgs.c.name))


def _role_there(con: Connection, org_id: str, user_id: str) -> str | None:
    return con.execute(sa.select(db.memberships.c.role).where(
        db.memberships.c.org_id == org_id, db.memberships.c.user_id == user_id)).scalar()


def _owners(con: Connection, org_id: str) -> int:
    return con.execute(sa.select(sa.func.count()).select_from(db.memberships).where(
        db.memberships.c.org_id == org_id, db.memberships.c.role == "owner")).scalar_one()


def _may_manage(con: Connection, org_id: str, target: str, actor_id: str) -> str:
    """The target's role, once the actor's own role is read under the lock. An admin can
    run the org's people and has no say over an owner."""
    mine = _role_there(con, org_id, actor_id)
    if mine is None or not at_least(mine, "admin"):
        raise Forbidden("only an org admin can do this")
    held = _role_there(con, org_id, target)
    if held is None:
        raise NoSuchMember
    if held == "owner" and mine != "owner":
        raise Forbidden("only an owner can change an owner")
    return held


def set_role(org_id: str, target: str, role: str, actor_id: str, actor: str, now: float) -> None:
    if role not in ROLES:
        raise Invalid(f"role must be one of {', '.join(ROLES)}")
    with sec.engine().begin() as con:
        _lock_org(con, org_id)
        held = _may_manage(con, org_id, target, actor_id)
        if role == "owner" and _role_there(con, org_id, actor_id) != "owner":
            raise Forbidden("only an owner can make an owner")
        if role == held:
            return
        if held == "owner" and _owners(con, org_id) == 1:
            raise LastOwner
        con.execute(db.memberships.update().where(db.memberships.c.org_id == org_id,
                                                  db.memberships.c.user_id == target).values(role=role))
        # Rotation on a role change: the member signs in again and gets a new session id.
        _end(con, sa.and_(db.sessions.c.org_id == org_id, db.sessions.c.user_id == target))
        usage.audit(con, org_id, actor, "member.role", {"user_id": target, "from": held, "to": role}, now)


def _drop_if_no_org_left(con: Connection, user_id: str) -> None:
    """Nobody with no org can log in, and the email would stay taken for good."""
    if con.execute(sa.select(db.memberships.c.org_id).where(db.memberships.c.user_id == user_id).limit(1)).first() is None:
        _end(con, db.sessions.c.user_id == user_id)
        for table in sec.USER_TABLES:
            con.execute(table.delete().where(table.c.user_id == user_id))
        con.execute(db.users.delete().where(db.users.c.id == user_id))


def remove_member(org_id: str, target: str, actor_id: str, actor: str, now: float) -> None:
    with sec.engine().begin() as con:
        _lock_org(con, org_id)
        held = _may_manage(con, org_id, target, actor_id)
        if held == "owner" and _owners(con, org_id) == 1:
            raise LastOwner
        con.execute(db.memberships.delete().where(db.memberships.c.org_id == org_id,
                                                  db.memberships.c.user_id == target))
        _end(con, sa.and_(db.sessions.c.org_id == org_id, db.sessions.c.user_id == target))
        _drop_if_no_org_left(con, target)
        usage.audit(con, org_id, actor, "member.remove", {"user_id": target, "role": held}, now)


# ---------------------------------------------------------------- export

EXPORT_LAYERS = ("silver", "gold")


def _scenario_folders(org_id: str) -> list[Path]:
    """Scenario workspaces live under the data root as o<8 of the org id>-<name>. Eight
    characters can collide, so the owner file decides. Nothing here follows a link."""
    found = []
    for d in sorted(data_root().glob(f"o{org_id[:8]}-*")):
        marker = d / OWNER_FILE
        if (not d.is_symlink() and d.is_dir() and marker.is_file() and not marker.is_symlink()
                and marker.read_text().strip() == org_id):
            found.append(d)
    return found


def _add_file(z: zipfile.ZipFile, path: Path, name: str) -> None:
    """Regular files only. O_NOFOLLOW, so a link planted in a folder cannot pull a file
    from outside the org into the download."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return
    with os.fdopen(fd, "rb") as fh:
        if stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            z.writestr(name, fh.read())


def _add_workspace(z: zipfile.ZipFile, org_id: str, d: Path) -> dict[str, Any]:
    from .. import workspaces_tenant as wt
    from ..lake import Lakehouse

    base = f"workspaces/{d.name}"
    for name in ("workspace.yaml", wt.RECORDS_FILE, "ontology.yaml", "policies.yaml"):
        _add_file(z, d / name, f"{base}/{name}")
    for sub in ("models/silver", "models/gold", "ontology_versions"):
        folder = d / sub
        if folder.is_dir() and not folder.is_symlink():
            for f in sorted(folder.iterdir()):
                _add_file(z, f, f"{base}/{sub}/{f.name}")
    try:
        lake = Lakehouse(wt.load_workspace(org_id, d.name))
    except wt.WorkspaceError as e:
        return {"tables": {}, "skipped": str(e)}
    tables = {}
    for ident in lake.tables():
        layer, table = ident.split(".")
        if layer in EXPORT_LAYERS:
            data = lake.read(ident)
            buf = pa.BufferOutputStream()
            pq.write_table(data, buf)
            z.writestr(f"{base}/{layer}/{table}.parquet", buf.getvalue().to_pybytes())
            tables[ident] = data.num_rows
    return {"tables": tables, "skipped": None}


def export_org(org_id: str, principal: str, job_id: str, check_cancel: Callable[[], None],
               log: Callable[[str], None], now: float) -> dict[str, Any]:
    """Specs, accepted ontologies, and silver and gold tables, as one zip inside the org's own
    folder. A wizard workspace has silver and no gold, so silver is what makes this hold the
    data. Raw uploads and bronze stay out: the owner has those already. The role is read here
    because /jobs is member level and takes any registered kind."""
    from .. import workspaces_tenant as wt

    user_id = principal.removeprefix("user:")
    with sec.engine().connect() as con:
        if org_id == DEV_ORG_ID or _role_there(con, org_id, user_id) != "owner":
            raise Forbidden("only an org owner can export")
    folder = private_dir(tenant_dir(org_id) / "exports")
    target = safe_child(folder, f"{job_id}.zip")
    partial = safe_child(folder, f"{job_id}.part")
    root = workspaces_root(org_id)
    manifest: dict[str, Any] = {"exported_at": now, "exported_by": principal, "workspaces": [], "scenarios": [],
                                "gold": {}, "tables": {}, "totals": {"tables": 0, "rows": 0}, "skipped": {}}
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
            for d in sorted(root.iterdir()) if root.is_dir() else []:
                if d.is_symlink() or not wt._created_here(d, org_id, d.name):
                    continue
                check_cancel()
                log(f"[{d.name}] exporting")
                got = _add_workspace(z, org_id, d)
                manifest["workspaces"].append(d.name)
                manifest["gold"][d.name] = [t.split(".")[1] for t in got["tables"] if t.startswith("gold.")]
                manifest["tables"][d.name] = got["tables"]
                manifest["totals"]["tables"] += len(got["tables"])
                manifest["totals"]["rows"] += sum(got["tables"].values())
                if got["skipped"]:
                    manifest["skipped"][d.name] = got["skipped"]
            for d in _scenario_folders(org_id):
                name = d.name.split("-", 1)[1]
                _add_file(z, d / "scenario.yaml", f"scenarios/{name}/scenario.yaml")
                manifest["scenarios"].append(name)
            z.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    for old in folder.glob("*.zip"):  # one copy of everything is enough to look after
        if old != target:
            old.unlink(missing_ok=True)
    return {**manifest, "bytes": target.stat().st_size}


def export_file(org_id: str, job_id: str) -> Path | None:
    try:
        path = safe_child(tenant_dir(org_id) / "exports", f"{job_id}.zip")
    except UnsafePath:
        return None
    return path if path.is_file() and not path.is_symlink() else None


# ---------------------------------------------------------------- delete

def mark_deleting(org_id: str, principal: str, now: float) -> bool:
    """The point of no return, in one transaction. Nobody can sign in to this org from
    the moment it commits, and no new work can start. False means someone got here first."""
    from ..jobs.store import JobStore, _now, job_runtime, jobs

    if org_id == DEV_ORG_ID:
        raise Forbidden("the dev org cannot be deleted")
    JobStore()  # its tables exist before this transaction needs them
    with sec.engine().begin() as con:
        _lock_org(con, org_id)
        if con.execute(sa.select(org_lifecycle.c.org_id).where(org_lifecycle.c.org_id == org_id)).first():
            return False
        ids = con.execute(sa.select(db.memberships.c.user_id).where(db.memberships.c.org_id == org_id)).scalars().all()
        con.execute(org_lifecycle.insert().values(org_id=org_id, state="deleting", requested_by=principal,
                                                  requested_at=now, member_ids=json.dumps(sorted(ids))))
        for table in (db.sessions, db.invites, db.memberships):
            con.execute(table.delete().where(table.c.org_id == org_id))
        con.execute(jobs.update().where(jobs.c.org_id == org_id, jobs.c.state == "queued")
                    .values(state="cancelled", finished_at=_now()))
        running = sa.select(jobs.c.id).where(jobs.c.org_id == org_id, jobs.c.state == "running")
        con.execute(job_runtime.update().where(job_runtime.c.job_id.in_(running)).values(cancel_requested=True))
    return True


def enqueue_deletion(org_id: str, principal: str, store: Any) -> str:
    return store.enqueue(org_id, ORG_JOBS, "org_delete", principal, {})


def pending_deletions() -> list[str]:
    """For an operator: orgs still marked, because the job is waiting or it failed."""
    with sec.engine().connect() as con:
        return list(con.execute(sa.select(org_lifecycle.c.org_id).order_by(org_lifecycle.c.requested_at)).scalars())


def resume_deletion(org_id: str, store: Any) -> str:
    """Nobody is left in the org to retry, so a failed delete comes back through here."""
    with sec.engine().connect() as con:
        by = con.execute(sa.select(org_lifecycle.c.requested_by).where(org_lifecycle.c.org_id == org_id)).scalar()
    if by is None:
        raise NotDeleting(org_id)
    return enqueue_deletion(org_id, by, store)


def _remove_lake_prefix(org_id: str) -> None:
    uri = lake_uri()
    if uri is None:
        return
    from .. import hosted

    try:
        hosted.object_store().delete_dir(f"{uri.removeprefix('s3://')}/tenants/{org_id}")
    except FileNotFoundError:
        pass


def _remove_files(org_id: str) -> None:
    """tenant_dir refuses anything but a full org id, so this cannot name a parent."""
    org_root = tenant_dir(org_id)
    if org_root.exists():
        shutil.rmtree(org_root)
    for d in _scenario_folders(org_id):
        shutil.rmtree(d)


def _forget_mail(org_id: str, request: Any) -> None:
    """The local mailbox holds invites that name the org and mail to its people. Before the
    rows go, while the names are still known, take out every line about either. Someone
    who stays in another org keeps their mail. A hosted provider's sent mail is not ours."""
    with sec.engine().connect() as con:
        org = con.execute(sa.select(db.orgs.c.name, db.orgs.c.slug).where(db.orgs.c.id == org_id)).first()
        gone = [e for uid in json.loads(request["member_ids"])
                if not con.execute(sa.select(db.memberships.c.org_id).where(db.memberships.c.user_id == uid)).first()
                for e in con.execute(sa.select(db.users.c.email).where(db.users.c.id == uid)).scalars()]
    outbox.forget([*(org or ()), *gone])


def _forget_org(org_id: str, request: Any, now: float) -> None:
    """Every row, in one transaction, then the tombstone. The tables come from the live
    database and not from a list, so one added later is cleared too. A hosted lake's
    catalog is in here as well, in pyiceberg's tables, keyed by a name that holds the org."""
    from ..jobs.store import JobStore, job_runtime, jobs

    JobStore()
    with sec.engine().begin() as con:
        _lock_org(con, org_id)
        people = set(json.loads(request["member_ids"]))
        people |= set(con.execute(sa.select(db.memberships.c.user_id)
                                  .where(db.memberships.c.org_id == org_id)).scalars())
        con.execute(job_runtime.delete().where(
            job_runtime.c.job_id.in_(sa.select(jobs.c.id).where(jobs.c.org_id == org_id))))
        inspector = sa.inspect(con)
        for name in inspector.get_table_names():
            if name == "orgs":
                continue
            columns = {c["name"] for c in inspector.get_columns(name)}
            if "org_id" in columns:
                t = sa.table(name, sa.column("org_id"))
                con.execute(t.delete().where(t.c.org_id == org_id))
            if "catalog_name" in columns:
                t = sa.table(name, sa.column("catalog_name"))
                con.execute(t.delete().where(t.c.catalog_name.startswith(f"tenant/{org_id}/", autoescape=True)))
        for user_id in sorted(people):
            _drop_if_no_org_left(con, user_id)
        con.execute(db.orgs.delete().where(db.orgs.c.id == org_id))
        usage.audit(con, org_id, request["requested_by"], "org.deleted",
                    {"requested_at": request["requested_at"], "members": len(people)}, now)


def delete_org_data(org_id: str, now: float) -> dict[str, str]:
    """Safe to run twice: a worker can die anywhere in here, and the job comes back.
    The rows go last, because the mark is what lets a second run happen. The caller
    must not log after this returns: the job's log folder and its row are gone."""
    if not ORG_ID.fullmatch(org_id) or org_id == DEV_ORG_ID:
        raise NotDeleting(org_id)
    with sec.engine().connect() as con:
        request = con.execute(sa.select(org_lifecycle).where(org_lifecycle.c.org_id == org_id)).mappings().first()
    if request is None:
        raise NotDeleting(org_id)
    for wait in (*_RETRY_WAITS, None):
        try:
            # The lake first. The job's log is in the org's folder, and a failure here must still reach it.
            _remove_lake_prefix(org_id)
            _remove_files(org_id)
            _forget_mail(org_id, request)
            break
        except OSError:
            if wait is None:
                raise
            time.sleep(wait)
    _forget_org(org_id, request, now)
    return {"state": "deleted"}


# ---------------------------------------------------------------- mfa

MFA_TICKET_SECONDS = 5 * 60
MFA_TICKET_ATTEMPTS = 5


class Conflict(Exception):
    """The request clashes with the account's state. The words say how."""


def _mfa_row(con: Connection, user_id: str) -> Any:
    return con.execute(sa.select(sec.user_mfa).where(sec.user_mfa.c.user_id == user_id)).mappings().first()


def mfa_enabled(con: Connection, user_id: str) -> bool:
    row = _mfa_row(con, user_id)
    return bool(row and row["enabled_at"] is not None)


def _requiring_orgs(con: Connection, user_id: str) -> list[str]:
    o = sec.org_security
    return [org["slug"] for org in _orgs_of(con, user_id)
            if con.execute(sa.select(o.c.require_mfa).where(o.c.org_id == org["id"])).scalar()]


def mfa_setup_required(ctx: Ctx) -> bool:
    """The org asks for two-step sign in and this user has not set it up."""
    with sec.engine().connect() as con:
        required = con.execute(sa.select(sec.org_security.c.require_mfa)
                               .where(sec.org_security.c.org_id == ctx.org_id)).scalar()
        return bool(required) and not mfa_enabled(con, ctx.user_id)


def _mfa_ticket(con: Connection, user_id: str, org_id: str, now: float) -> dict[str, Any]:
    """What a right password buys an MFA account: a ticket for the code, never a session."""
    t = sec.mfa_tickets
    con.execute(t.delete().where(sa.or_(t.c.user_id == user_id, t.c.expires_at <= now)))
    ticket = secrets.token_urlsafe(32)
    con.execute(t.insert().values(token_hash=_token_hash(ticket), user_id=user_id, org_id=org_id,
                                  expires_at=now + MFA_TICKET_SECONDS, attempts=0))
    email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == user_id)).scalar_one()
    return {"mfa_required": True, "ticket": ticket, "email": email}


def complete_mfa(ticket: str, code: str | None, recovery: str | None, now: float,
                 seen: dict[str, str] | None = None) -> dict[str, Any]:
    t = sec.mfa_tickets
    hashed = _token_hash(ticket or "")
    with sec.engine().begin() as con:
        row = con.execute(sa.select(t).where(t.c.token_hash == hashed)).mappings().first()
        if row is None or row["expires_at"] <= now or row["attempts"] >= MFA_TICKET_ATTEMPTS:
            raise BadCredentials
        user_id = row["user_id"]
        email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == user_id)).scalar_one()
        ratelimit.check(email, now)  # a ticket from before the lock does not get round it
        held = _mfa_row(con, user_id)
        step = used = None
        if held and held["enabled_at"] is not None:
            if code:
                secret = mfa.unseal(user_id, held["sealed"])
                step = mfa.match(secret, code, now, held["last_step"]) if secret else None
            elif recovery:
                left = json.loads(held["recovery"])
                used = mfa.code_hash(recovery) if mfa.code_hash(recovery) in left else None
        if step is not None or used is not None:
            if con.execute(t.delete().where(t.c.token_hash == hashed)).rowcount != 1:
                raise BadCredentials  # a racing request spent it first
            values = ({"last_step": step} if step is not None else
                      {"recovery": json.dumps([h for h in json.loads(held["recovery"]) if h != used])})
            con.execute(sec.user_mfa.update().where(sec.user_mfa.c.user_id == user_id).values(**values))
            if used is not None:
                audit_user(con, user_id, principal_of(user_id), "mfa.recovery_used", _where(seen), now)
            audit_user(con, user_id, principal_of(user_id), "auth.login", {**_where(seen), "mfa": True}, now)
            answer = _session_answer(con, _open_session(con, user_id, row["org_id"], now, seen),
                                     user_id, row["org_id"])
    if step is None and used is None:
        with sec.engine().begin() as con:
            con.execute(t.update().where(t.c.token_hash == hashed).values(attempts=t.c.attempts + 1))
        failed_sign_in(email, user_id, now, seen)
        raise BadCredentials
    ratelimit.clear(email)
    return answer


def mfa_status(user_id: str) -> dict[str, Any]:
    with sec.engine().connect() as con:
        row = _mfa_row(con, user_id)
        return {"enabled": bool(row and row["enabled_at"] is not None),
                "recovery_left": len(json.loads(row["recovery"])) if row and row["enabled_at"] else 0,
                "required_by": _requiring_orgs(con, user_id)}


def mfa_enrol(user_id: str, now: float, code: str | None = None) -> dict[str, str]:
    """A secret waiting for its first code. It counts for nothing until confirmed. With
    two-step already on, this is a new phone: it takes a current code or a recovery
    code, and the old secret keeps working until the new one is confirmed."""
    secret = mfa.new_secret()
    sealed = mfa.seal(user_id, secret)
    with sec.engine().begin() as con:
        email = con.execute(sa.select(db.users.c.email).where(db.users.c.id == user_id)).scalar_one()
        if mfa_enabled(con, user_id):
            _prove(con, user_id, code, now)
            con.execute(sec.user_mfa.update().where(sec.user_mfa.c.user_id == user_id).values(pending=sealed))
        else:
            con.execute(sec.user_mfa.delete().where(sec.user_mfa.c.user_id == user_id))
            con.execute(sec.user_mfa.insert().values(user_id=user_id, sealed="", pending=sealed, enabled_at=None,
                                                     last_step=0, recovery="[]"))
    return {"secret": mfa.b32(secret), "otpauth_uri": mfa.uri(secret, email)}


def mfa_confirm(ctx: Ctx, code: str, now: float, seen: dict[str, str] | None = None) -> dict[str, Any]:
    """The first code turns it on. Every session ends, since none of them passed a code,
    and this device gets a new one: the rotation on MFA enrolment."""
    codes = mfa.recovery_codes()
    with sec.engine().begin() as con:
        row = _mfa_row(con, ctx.user_id)
        if row is None or not row["pending"]:
            raise Conflict("start setting up two-step sign in first")
        secret = mfa.unseal(ctx.user_id, row["pending"])
        step = mfa.match(secret, code, now, 0) if secret else None
        if step is None:
            raise Invalid("that code does not match; check the time on your phone and try the next one")
        con.execute(sec.user_mfa.update().where(sec.user_mfa.c.user_id == ctx.user_id).values(
            sealed=row["pending"], pending=None, enabled_at=now, last_step=step,
            recovery=json.dumps([mfa.code_hash(c) for c in codes])))
        since = _age_of(con, (seen or {}).get("replacing", ""))
        _end(con, db.sessions.c.user_id == ctx.user_id)
        audit_user(con, ctx.user_id, ctx.principal, "mfa.enable", {}, now)
        session = _session_answer(con, _open_session(con, ctx.user_id, ctx.org_id, now, seen, since),
                                  ctx.user_id, ctx.org_id)
    return {"session": session, "recovery_codes": codes}


def _prove(con: Connection, user_id: str, code: str | None, now: float) -> dict[str, Any]:
    """A current code, or a recovery code, which is spent. Someone who lost their phone
    has only the second, and needs it to turn two-step off or move it to a new phone."""
    row = _mfa_row(con, user_id)
    if row is None or row["enabled_at"] is None:
        raise Conflict("two-step sign in is not on")
    secret = mfa.unseal(user_id, row["sealed"])
    step = mfa.match(secret, code or "", now, row["last_step"]) if secret else None
    if step is not None:
        con.execute(sec.user_mfa.update().where(sec.user_mfa.c.user_id == user_id).values(last_step=step))
        return row
    left = json.loads(row["recovery"])
    used = mfa.code_hash(code or "")
    if used not in left:
        raise Invalid("that code does not match")
    con.execute(sec.user_mfa.update().where(sec.user_mfa.c.user_id == user_id)
                .values(recovery=json.dumps([h for h in left if h != used])))
    audit_user(con, user_id, principal_of(user_id), "mfa.recovery_used", {}, now)
    return row


def mfa_disable(ctx: Ctx, code: str | None, now: float) -> None:
    with sec.engine().begin() as con:
        required = _requiring_orgs(con, ctx.user_id)
        if required:
            raise Conflict(f"{', '.join(required)} requires two-step sign in, so it has to stay on")
        _prove(con, ctx.user_id, code, now)
        con.execute(sec.user_mfa.delete().where(sec.user_mfa.c.user_id == ctx.user_id))
        audit_user(con, ctx.user_id, ctx.principal, "mfa.disable", {}, now)


def mfa_new_recovery(ctx: Ctx, code: str | None, now: float) -> list[str]:
    codes = mfa.recovery_codes()
    with sec.engine().begin() as con:
        _prove(con, ctx.user_id, code, now)
        con.execute(sec.user_mfa.update().where(sec.user_mfa.c.user_id == ctx.user_id)
                    .values(recovery=json.dumps([mfa.code_hash(c) for c in codes])))
        audit_user(con, ctx.user_id, ctx.principal, "mfa.recovery_new", {}, now)
    return codes


def org_security(org_id: str) -> dict[str, Any]:
    with sec.engine().connect() as con:
        required = con.execute(sa.select(sec.org_security.c.require_mfa)
                               .where(sec.org_security.c.org_id == org_id)).scalar()
        people = con.execute(sa.select(db.memberships.c.user_id).where(db.memberships.c.org_id == org_id)).scalars().all()
        enrolled = sum(1 for p in people if mfa_enabled(con, p))
    return {"require_mfa": bool(required), "members": len(people), "members_with_mfa": enrolled}


def set_require_mfa(org_id: str, on: bool, actor_id: str, actor: str, now: float) -> None:
    """Owner only, which the gate checks. An owner cannot lock themselves out: they must
    have it on first."""
    o = sec.org_security
    with sec.engine().begin() as con:
        _lock_org(con, org_id)
        if _role_there(con, org_id, actor_id) != "owner":
            raise Forbidden("only an org owner can change this")
        if on and not mfa_enabled(con, actor_id):
            raise Conflict("turn on two-step sign in for yourself before you require it for the org")
        con.execute(o.delete().where(o.c.org_id == org_id))
        con.execute(o.insert().values(org_id=org_id, require_mfa=on))
        usage.audit(con, org_id, actor, "org.require_mfa", {"on": on}, now)
