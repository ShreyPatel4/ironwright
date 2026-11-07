"""Side tables for auth. Contract 1's tables stay as written, and there are no migrations,
so anything new about a session, a user or an org lives here, the way usage.py does it.

Every table that holds an org's rows has an org_id column, so the org delete sweep
clears it. Rows keyed by a user go in service._drop_if_no_org_left.
"""
from __future__ import annotations

import time
import weakref

import sqlalchemy as sa

from ..tenancy import usage

metadata = sa.MetaData()

# One row per session. A session with no row here predates this table and is refused.
session_info = sa.Table(
    "session_info", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("sid", sa.String(32), nullable=False, unique=True),  # what the sessions list shows
    sa.Column("user_id", sa.String(32), nullable=False, index=True),
    sa.Column("org_id", sa.String(32), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("ip", sa.String(64), nullable=False),
    sa.Column("user_agent", sa.String(256), nullable=False),
)
# Failed sign-ins per email, known or not, so a lock reads the same either way.
auth_failures = sa.Table(
    "auth_failures", metadata,
    sa.Column("key", sa.String(64), primary_key=True),  # sha256 of the normalized email
    sa.Column("count", sa.Integer, nullable=False),
    sa.Column("window_start", sa.Float, nullable=False),
    sa.Column("locked_until", sa.Float, nullable=False),
)
# Verify and reset links. Single use, only the hash is kept.
auth_links = sa.Table(
    "auth_links", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("user_id", sa.String(32), nullable=False, index=True),
    sa.Column("expires_at", sa.Float, nullable=False),
)
user_flags = sa.Table(
    "user_flags", metadata,
    sa.Column("user_id", sa.String(32), primary_key=True),
    sa.Column("email_verified_at", sa.Float),
)
user_mfa = sa.Table(
    "user_mfa", metadata,
    sa.Column("user_id", sa.String(32), primary_key=True),
    sa.Column("sealed", sa.Text, nullable=False),     # the secret sign in checks; "" before the first
    sa.Column("pending", sa.Text),                    # a new secret waiting for its first code
    sa.Column("enabled_at", sa.Float),
    sa.Column("last_step", sa.BigInteger, nullable=False),
    sa.Column("recovery", sa.Text, nullable=False),  # json list of sha256 hashes
)
# A correct password on an MFA account buys one of these, never a session.
mfa_tickets = sa.Table(
    "mfa_tickets", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(32), nullable=False, index=True),
    sa.Column("org_id", sa.String(32), nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
)
org_security = sa.Table(
    "org_security", metadata,
    sa.Column("org_id", sa.String(32), primary_key=True),
    sa.Column("require_mfa", sa.Boolean, nullable=False),
)
api_tokens = sa.Table(
    "api_tokens", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("org_id", sa.String(32), nullable=False, index=True),
    sa.Column("name", sa.String(80), nullable=False),
    sa.Column("scopes", sa.String(128), nullable=False),  # space separated
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("created_by", sa.String(256), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("last_used", sa.Float),
    sa.Column("revoked_at", sa.Float),
)

USER_TABLES = (session_info, auth_links, user_flags, user_mfa, mfa_tickets)

_ready: weakref.WeakSet[sa.Engine] = weakref.WeakSet()


def engine() -> sa.Engine:
    """The metadata store, with these tables created on first use."""
    eng = usage.engine()  # the quota and audit tables first
    if eng not in _ready:
        for attempt in range(10):
            try:
                metadata.create_all(eng)
                break
            except sa.exc.DBAPIError:  # another process lost the same create_all race
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
        _ready.add(eng)
    return eng
