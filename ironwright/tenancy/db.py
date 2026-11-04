"""The metadata store: SQLAlchemy Core, plain tables, no migrations for the MVP.

The URL comes from IRONWRIGHT_DB_URL, or a private SQLite file under the data
root. Both are read on every call, so engines are cached per URL and a process
that changes its data root gets the right store.
"""
from __future__ import annotations

import os
import threading

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from ..config import secure_data_root

SCHEMA_VERSION = 1
# Any fixed number. It stops two processes booting at once from racing create_all.
_INIT_LOCK_ID = 7231904

metadata = sa.MetaData()

orgs = sa.Table(
    "orgs", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("slug", sa.String(40), nullable=False, unique=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("email", sa.String(320), nullable=False, unique=True),
    sa.Column("pw_hash", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
memberships = sa.Table(
    "memberships", metadata,
    sa.Column("org_id", sa.String(32), sa.ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),
    sa.CheckConstraint("role in ('owner','admin','member','viewer')", name="ck_memberships_role"),
)
sessions = sa.Table(
    "sessions", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("org_id", sa.String(32), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("last_seen", sa.Float, nullable=False),
)
invites = sa.Table(
    "invites", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("org_id", sa.String(32), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
    sa.Column("email", sa.String(320), nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.CheckConstraint("role in ('owner','admin','member','viewer')", name="ck_invites_role"),
)
schema_version = sa.Table(
    "schema_version", metadata,
    sa.Column("version", sa.Integer, primary_key=True),
)

_ENGINES: dict[str, Engine] = {}
_LOCK = threading.Lock()


def normalize_url(url: str) -> str:
    """Bare postgres URLs mean psycopg2 to SQLAlchemy. We ship psycopg 3."""
    for bare in ("postgresql://", "postgres://"):
        if url.startswith(bare):
            return "postgresql+psycopg://" + url[len(bare):]
    return url


def _default_url() -> str:
    path = secure_data_root() / "meta.sqlite"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    return f"sqlite:///{path}"


def init(eng: Engine) -> None:
    """create_all plus the one schema_version row. Safe to run twice, or twice at once."""
    def create() -> None:
        with eng.begin() as con:
            if eng.dialect.name == "postgresql":
                con.execute(sa.select(sa.func.pg_advisory_xact_lock(_INIT_LOCK_ID)))
            metadata.create_all(con)
            if con.execute(sa.select(schema_version)).first() is None:
                con.execute(schema_version.insert().values(version=SCHEMA_VERSION))
    try:
        create()
    except (OperationalError, IntegrityError):  # SQLite has no advisory lock: another process won
        create()


def _current_url() -> str:
    return normalize_url(os.environ.get("IRONWRIGHT_DB_URL") or _default_url())


def dispose_current() -> None:
    """Close the current store's connections and forget it. For test teardown."""
    with _LOCK:
        eng = _ENGINES.pop(_current_url(), None)
    if eng is not None:
        eng.dispose()


def engine() -> Engine:
    url = _current_url()
    with _LOCK:
        eng = _ENGINES.get(url)
        if eng is None:
            eng = sa.create_engine(url, pool_pre_ping=True)
            if eng.dialect.name == "sqlite":
                @sa.event.listens_for(eng, "connect")
                def _foreign_keys(con, _record) -> None:
                    con.execute("pragma foreign_keys=on")
            init(eng)
            _ENGINES[url] = eng
    return eng
