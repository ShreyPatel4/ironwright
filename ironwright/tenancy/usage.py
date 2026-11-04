"""The tables behind quotas: daily counters, each org's plan, and the org audit log.

These sit on T1's metadata store with their own MetaData, created on first use, the
way jobs/store.py does it. Contract 1's tables stay as written.

A charge is atomic. It makes sure the row exists, then adds only if the sum still
fits, in one UPDATE. So two requests racing for the last unit cannot both win, and a
refusal changes nothing. Days are UTC. Time always comes in as an argument.
"""
from __future__ import annotations

import json
import time
import weakref
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from ..llm import LLM, LLMResult
from . import db

metadata = sa.MetaData()

usage = sa.Table(
    "usage", metadata,
    sa.Column("org_id", sa.String(32), primary_key=True),
    sa.Column("day", sa.String(10), primary_key=True),
    sa.Column("metric", sa.String(32), primary_key=True),
    sa.Column("amount", sa.BigInteger, nullable=False),
)
org_plans = sa.Table(
    "org_plans", metadata,
    sa.Column("org_id", sa.String(32), primary_key=True),
    sa.Column("plan", sa.String(16), nullable=False),
)
org_audit = sa.Table(
    "org_audit", metadata,
    sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("org_id", sa.String(32), nullable=False, index=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("actor", sa.String(256), nullable=False),
    sa.Column("action", sa.String(32), nullable=False),
    sa.Column("detail", sa.Text, nullable=False),
)
org_llm_keys = sa.Table(
    "org_llm_keys", metadata,
    sa.Column("org_id", sa.String(32), primary_key=True),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("sealed", sa.Text, nullable=False),
    sa.Column("last4", sa.String(4), nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Column("updated_by", sa.String(256), nullable=False),
)

_ready: weakref.WeakSet[sa.Engine] = weakref.WeakSet()


def engine() -> sa.Engine:
    eng = db.engine()
    if eng not in _ready:
        # Several processes can boot on an empty database, and create_all's
        # check-then-create can lose that race. Losing is fine: look again.
        for attempt in range(10):
            try:
                metadata.create_all(eng)
                break
            except sa.exc.DBAPIError:
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
        _ready.add(eng)
    return eng


def day_of(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def _ensure_row(con: Connection, org_id: str, day: str, metric: str) -> None:
    insert = pg_insert if con.dialect.name == "postgresql" else sqlite_insert
    con.execute(insert(usage).values(org_id=org_id, day=day, metric=metric, amount=0)
                .on_conflict_do_nothing())


def _this(org_id: str, day: str, metric: str) -> sa.ColumnElement[bool]:
    return sa.and_(usage.c.org_id == org_id, usage.c.day == day, usage.c.metric == metric)


def charge(org_id: str, metric: str, limit: int, now: float, n: int = 1) -> bool:
    """Add n if the day's total stays within limit. False means nothing was added."""
    day = day_of(now)
    with engine().begin() as con:
        _ensure_row(con, org_id, day, metric)
        done = con.execute(usage.update()
                           .where(_this(org_id, day, metric), usage.c.amount + n <= limit)
                           .values(amount=usage.c.amount + n))
        return done.rowcount == 1


def add(org_id: str, metric: str, now: float, n: int) -> None:
    """Add what was already spent. A model reports its tokens after the call, so this
    can pass the limit. The next request is the one refused."""
    day = day_of(now)
    with engine().begin() as con:
        _ensure_row(con, org_id, day, metric)
        con.execute(usage.update().where(_this(org_id, day, metric)).values(amount=usage.c.amount + n))


def used(org_id: str, metric: str, now: float) -> int:
    with engine().connect() as con:
        return con.execute(sa.select(usage.c.amount).where(_this(org_id, day_of(now), metric))).scalar() or 0


def audit(con: Connection, org_id: str, actor: str, action: str, detail: dict[str, Any], now: float) -> None:
    """Written in the caller's transaction, so a change and its record land together."""
    con.execute(org_audit.insert().values(org_id=org_id, ts=now, actor=actor,
                                          action=action, detail=json.dumps(detail, sort_keys=True)))


def audit_log(org_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Oldest first, of the newest `limit` rows."""
    with engine().connect() as con:
        rows = con.execute(sa.select(org_audit).where(org_audit.c.org_id == org_id)
                           .order_by(org_audit.c.seq.desc()).limit(limit)).mappings().all()
    return [{"ts": r["ts"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail"])}
            for r in reversed(rows)]


class MeteredLLM:
    """An LLM that records what each call really spent, under one metric."""

    def __init__(self, inner: LLM, org_id: str, metric: str, now: float) -> None:
        self._inner, self._org_id, self._metric, self._now = inner, org_id, metric, now

    @property
    def offline(self) -> bool:
        return self._inner.offline

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(self, system: str, prompt: str, max_tokens: int = 600) -> LLMResult:
        res = self._inner.complete(system, prompt, max_tokens)
        spent = res.tokens_in + res.tokens_out
        if spent:
            add(self._org_id, self._metric, self._now, spent)
        return res
