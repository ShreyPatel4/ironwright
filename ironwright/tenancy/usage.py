"""The tables behind quotas: daily counters, each org's plan, and the org audit log.

These sit on T1's metadata store with their own MetaData, created on first use, the
way jobs/store.py does it. Contract 1's tables stay as written.

A charge is atomic. It makes sure the row exists, then adds only if the sum still
fits, in one UPDATE. So two requests racing for the last unit cannot both win, and a
refusal changes nothing. Days are UTC. Time always comes in as an argument.
"""
from __future__ import annotations

import hashlib
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
# One hash per org_audit row, written with it. It sits beside org_audit because there are
# no migrations and that table is already live. A row with no link predates the chain.
org_audit_chain = sa.Table(
    "org_audit_chain", metadata,
    sa.Column("seq", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("org_id", sa.String(32), nullable=False, index=True),
    sa.Column("prev_hash", sa.String(64), nullable=False),
    sa.Column("hash", sa.String(64), nullable=False),
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


def refund(org_id: str, metric: str, now: float, n: int = 1) -> None:
    """Give back what a refused request took. Never below zero."""
    with engine().begin() as con:
        con.execute(usage.update().where(_this(org_id, day_of(now), metric), usage.c.amount >= n)
                    .values(amount=usage.c.amount - n))


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


def _link(org_id: str, seq: int, ts: float, actor: str, action: str, detail: str, prev: str) -> str:
    return hashlib.sha256(json.dumps([org_id, seq, ts, actor, action, detail, prev]).encode()).hexdigest()


def audit(con: Connection, org_id: str, actor: str, action: str, detail: dict[str, Any], now: float) -> None:
    """Written in the caller's transaction, so a change and its record land together.

    Two writers must not read the same last hash, or the chain forks. On Postgres
    that takes a lock on the org's chain. On SQLite the insert already holds the
    one write lock.
    """
    if con.dialect.name == "postgresql":
        con.execute(sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext(org_id))))
    text = json.dumps(detail, sort_keys=True)
    seq = con.execute(org_audit.insert().values(org_id=org_id, ts=now, actor=actor, action=action,
                                                detail=text)).inserted_primary_key[0]
    prev = con.execute(sa.select(org_audit_chain.c.hash).where(org_audit_chain.c.org_id == org_id)
                       .order_by(org_audit_chain.c.seq.desc()).limit(1)).scalar() or ""
    con.execute(org_audit_chain.insert().values(seq=seq, org_id=org_id, prev_hash=prev,
                                                hash=_link(org_id, seq, now, actor, action, text, prev)))


def audit_log(org_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Oldest first, of the newest `limit` rows."""
    with engine().connect() as con:
        rows = con.execute(sa.select(org_audit).where(org_audit.c.org_id == org_id)
                           .order_by(org_audit.c.seq.desc()).limit(limit)).mappings().all()
    return [{"ts": r["ts"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail"])}
            for r in reversed(rows)]


def _chain_rows(org_id: str) -> tuple[list[Any], dict[int, Any]]:
    with engine().connect() as con:
        rows = con.execute(sa.select(org_audit).where(org_audit.c.org_id == org_id)
                           .order_by(org_audit.c.seq)).mappings().all()
        links = con.execute(sa.select(org_audit_chain)
                            .where(org_audit_chain.c.org_id == org_id)).mappings().all()
    return rows, {link["seq"]: link for link in links}


def _judge(org_id: str, rows: list[Any], links: dict[int, Any]) -> dict[str, Any]:
    """Recompute every link. A link left with no row means a row was taken away, so that
    holds for the newest row too, unless its link was taken as well. Nothing here can
    see the newest rows and their links removed together: this chain has no outside anchor."""
    prev, unchained, broken_at = "", 0, None
    for r in rows:
        link = links.get(r["seq"])
        if link is None:
            if prev:
                broken_at = r["seq"]
                break
            unchained += 1
            continue
        want = _link(org_id, r["seq"], r["ts"], r["actor"], r["action"], r["detail"], prev)
        if link["prev_hash"] != prev or link["hash"] != want:
            broken_at = r["seq"]
            break
        prev = link["hash"]
    if broken_at is None:
        orphans = sorted(set(links) - {r["seq"] for r in rows})
        broken_at = orphans[0] if orphans else None
    return {"verified": broken_at is None, "broken_at": broken_at, "total": len(rows), "unchained": unchained}


def verify_chain(org_id: str) -> dict[str, Any]:
    return _judge(org_id, *_chain_rows(org_id))


def export_chain(org_id: str) -> list[dict[str, Any]]:
    """Every row with its link, oldest first, then the verdict. Anyone holding the file
    can recompute each hash with _link and see what we see."""
    rows, links = _chain_rows(org_id)
    out = [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "action": r["action"], "detail": _detail(r["detail"]),
            "prev_hash": links[r["seq"]]["prev_hash"] if r["seq"] in links else None,
            "hash": links[r["seq"]]["hash"] if r["seq"] in links else None} for r in rows]
    return [*out, {"org_id": org_id, **_judge(org_id, rows, links)}]


def audit_chain(org_id: str, actor: str | None = None, action: str | None = None, limit: int = 500,
                prefix: str | None = None) -> dict[str, Any]:
    """The verdict always covers the whole chain. Filters narrow what is listed, nothing else.
    prefix narrows by action, comma separated: "auth.,mfa.,token." is the auth audit."""
    rows, links = _chain_rows(org_id)
    listed = [r for r in rows if actor in (None, r["actor"]) and action in (None, r["action"])
              and r["action"].startswith(tuple((prefix or "").split(",")))][-limit:]
    return {**_judge(org_id, rows, links),
            "actors": sorted({r["actor"] for r in rows}), "actions": sorted({r["action"] for r in rows}),
            "rows": [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                      "detail": _detail(r["detail"]), "hash": links[r["seq"]]["hash"] if r["seq"] in links else None}
                     for r in listed]}


def _detail(text: str) -> Any:
    """A row can be tampered into anything. The page that reports that must still load."""
    try:
        return json.loads(text)
    except ValueError:
        return text


class MeteredLLM:
    """An LLM that records what each call really spent, under one metric."""

    def __init__(self, inner: LLM, org_id: str, metric: str, now: float) -> None:
        self._inner, self._org_id, self.metric, self._now = inner, org_id, metric, now

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
            add(self._org_id, self.metric, self._now, spent)
        return res
