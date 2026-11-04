"""The jobs table and every state change it allows.

`jobs` is contract 4 to the column. What the worker needs on top of it (params,
the cancel flag, the heartbeat, the claim token) sits in `job_runtime`, so the
contract table stays as written.

All claims take one short lock, so "how many run now" and "take this one" are a
single step. On Postgres that is an advisory lock plus FOR UPDATE SKIP LOCKED.
On SQLite it is a lock row: the first statement of the transaction writes it,
which takes SQLite's write lock before anything is read. A crash releases
either one, because each lives and dies with its transaction.

The engine helper here is for T1 to absorb when tenancy/ merges.
"""
from __future__ import annotations

import codecs
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ..config import UnsafePath, data_root, private_dir, safe_child

DEV_ORG = "dev"  # until T1's ctx() supplies the real org
MAX_ATTEMPTS = 3  # a job that keeps killing workers stops being retried
LOG_CHUNK = 64 * 1024
TERMINAL = {"succeeded", "failed", "cancelled"}
_PG_CLAIM_LOCK = 0x1a0b2c3d  # any fixed key; the same one for every claim

metadata = sa.MetaData()

jobs = sa.Table(
    "jobs", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("org_id", sa.String(64), nullable=False),
    sa.Column("workspace", sa.String(128), nullable=False),
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("created_by", sa.String(256), nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("started_at", sa.String(40)),
    sa.Column("finished_at", sa.String(40)),
    sa.Column("error", sa.Text),
    sa.Column("log_path", sa.Text),
)
# The database itself holds the fairness rule, whatever a claim gets wrong.
sa.Index("one_running_job_per_org", jobs.c.org_id, unique=True,
         sqlite_where=jobs.c.state == "running", postgresql_where=jobs.c.state == "running")
sa.Index("jobs_by_state", jobs.c.state, jobs.c.created_at)
sa.Index("jobs_by_org", jobs.c.org_id, jobs.c.created_at)

job_runtime = sa.Table(
    "job_runtime", metadata,
    sa.Column("job_id", sa.String(32), primary_key=True),
    sa.Column("params", sa.Text, nullable=False),
    sa.Column("result", sa.Text),
    sa.Column("cancel_requested", sa.Boolean, nullable=False, default=False),
    sa.Column("token", sa.String(32)),
    sa.Column("worker", sa.String(128)),
    sa.Column("heartbeat_at", sa.Float),
    sa.Column("attempts", sa.Integer, nullable=False, default=0),
)

job_claim_lock = sa.Table(
    "job_claim_lock", metadata,
    sa.Column("name", sa.String(16), primary_key=True),
    sa.Column("holder", sa.String(128)),
    sa.Column("acquired_at", sa.Float),
)

_engines: dict[str, sa.Engine] = {}
_ready: set[str] = set()
_engines_lock = threading.Lock()


def db_url() -> str:
    return os.environ.get("IRONWRIGHT_DB_URL") or f"sqlite:///{data_root()}/meta.sqlite"


def driver_url(url: str) -> str:
    """psycopg 3 is the Postgres driver this project ships."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _make_engine(url: str) -> sa.Engine:
    url = driver_url(url)
    if not url.startswith("sqlite"):
        return sa.create_engine(url)
    database = sa.engine.make_url(url).database
    if database and database != ":memory:":
        path = Path(database)
        private_dir(path.parent)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        path.chmod(0o600)
    made = sa.create_engine(url, connect_args={"timeout": 30})

    @sa.event.listens_for(made, "connect")
    def _wal(con, _record) -> None:
        con.execute("pragma journal_mode=wal")

    return made


def engine() -> sa.Engine:
    url = db_url()
    with _engines_lock:
        if url not in _engines:
            _engines[url] = _make_engine(url)
        return _engines[url]


def dispose_engines() -> None:
    with _engines_lock:
        for e in _engines.values():
            e.dispose()
        _engines.clear()
        _ready.clear()


def worker_cap() -> int:
    return max(1, int(os.environ.get("IRONWRIGHT_WORKERS", "2")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_schema(eng: sa.Engine) -> None:
    """Several processes boot at once on an empty database, and create_all's
    check-then-create can lose that race. Losing is fine: look again."""
    for attempt in range(10):
        try:
            metadata.create_all(eng)
            with eng.begin() as c:
                if not c.execute(sa.select(job_claim_lock.c.name)).first():
                    c.execute(job_claim_lock.insert().values(name="claim"))
            return
        except sa.exc.DBAPIError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def _job_row(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["params"] = json.loads(out["params"]) if out.get("params") else {}
    out["result"] = json.loads(out["result"]) if out.get("result") else None
    return out


_VIEW = sa.select(jobs, job_runtime.c.params, job_runtime.c.result, job_runtime.c.cancel_requested,
                  job_runtime.c.attempts).join(job_runtime, job_runtime.c.job_id == jobs.c.id)


class JobStore:
    def __init__(self, fresh_engine: bool = False) -> None:
        if fresh_engine:  # its own connections, as a separate process has
            self.engine = _make_engine(db_url())
            _create_schema(self.engine)
            return
        self.engine = engine()
        key = str(self.engine.url)
        if key not in _ready:
            _create_schema(self.engine)
            _ready.add(key)

    def _serialize(self, c: sa.Connection, holder: str) -> None:
        """Must be the first statement of its transaction. See the module note."""
        if self.engine.dialect.name == "postgresql":
            c.execute(sa.select(sa.func.pg_advisory_xact_lock(_PG_CLAIM_LOCK)))
        else:
            c.execute(job_claim_lock.update().where(job_claim_lock.c.name == "claim")
                      .values(holder=holder, acquired_at=time.time()))

    def enqueue(self, org_id: str, workspace: str, kind: str, created_by: str,
                params: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        with self.engine.begin() as c:
            c.execute(jobs.insert().values(
                id=job_id, org_id=org_id, workspace=workspace, kind=kind, state="queued",
                created_by=created_by, created_at=_now(),
                log_path=f"tenants/{org_id}/jobs/{job_id}.log"))
            c.execute(job_runtime.insert().values(job_id=job_id, params=json.dumps(params)))
        return job_id

    def get(self, org_id: str, job_id: str) -> dict[str, Any] | None:
        """Org scoped by construction: another org's id reads as no such job."""
        with self.engine.connect() as c:
            row = c.execute(_VIEW.where(jobs.c.id == job_id, jobs.c.org_id == org_id)).mappings().first()
        return _job_row(row) if row else None

    def list(self, org_id: str, workspace: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = _VIEW.where(jobs.c.org_id == org_id)
        if workspace is not None:
            q = q.where(jobs.c.workspace == workspace)
        q = q.order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(limit)
        with self.engine.connect() as c:
            return [_job_row(r) for r in c.execute(q).mappings()]

    def claim(self, worker: str, cap: int | None = None, job_id: str | None = None) -> dict[str, Any] | None:
        """Take the oldest job allowed to run, or None. With job_id, only that one."""
        cap = worker_cap() if cap is None else cap
        token = uuid.uuid4().hex
        with self.engine.begin() as c:
            self._serialize(c, worker)
            running = sa.select(jobs.c.org_id).where(jobs.c.state == "running")
            if c.execute(sa.select(sa.func.count()).select_from(running.subquery())).scalar_one() >= cap:
                return None
            q = sa.select(jobs).where(jobs.c.state == "queued", jobs.c.org_id.not_in(running))
            if job_id is not None:
                q = q.where(jobs.c.id == job_id)
            q = q.order_by(jobs.c.created_at, jobs.c.id).limit(1).with_for_update(skip_locked=True, of=jobs)
            row = c.execute(q).mappings().first()
            if row is None:
                return None
            started = _now()
            c.execute(jobs.update().where(jobs.c.id == row["id"]).values(state="running", started_at=started))
            c.execute(job_runtime.update().where(job_runtime.c.job_id == row["id"]).values(
                token=token, worker=worker, heartbeat_at=time.time(),
                attempts=job_runtime.c.attempts + 1))
            params = c.execute(sa.select(job_runtime.c.params)
                               .where(job_runtime.c.job_id == row["id"])).scalar_one()
        return {**row, "state": "running", "started_at": started, "token": token,
                "params": json.loads(params)}

    def heartbeat(self, job_id: str, token: str, now: float | None = None) -> bool:
        """False means this worker no longer owns the job and must stop."""
        with self.engine.begin() as c:
            hit = c.execute(job_runtime.update()
                            .where(job_runtime.c.job_id == job_id, job_runtime.c.token == token)
                            .values(heartbeat_at=time.time() if now is None else now))
        return hit.rowcount == 1

    def last_heartbeat(self, job_id: str) -> float | None:
        with self.engine.connect() as c:
            return c.execute(sa.select(job_runtime.c.heartbeat_at)
                             .where(job_runtime.c.job_id == job_id)).scalar_one_or_none()

    def finish(self, job_id: str, token: str, state: str, error: str | None = None,
               result: Any = None) -> bool:
        """Only the current owner can finish. A zombie's late result goes nowhere."""
        with self.engine.begin() as c:
            hit = c.execute(job_runtime.update()
                            .where(job_runtime.c.job_id == job_id, job_runtime.c.token == token)
                            .values(token=None, heartbeat_at=None,
                                    result=None if result is None else json.dumps(result, default=str)))
            if hit.rowcount != 1:
                return False
            c.execute(jobs.update().where(jobs.c.id == job_id)
                      .values(state=state, finished_at=_now(), error=error))
        return True

    def cancel(self, org_id: str, job_id: str) -> str | None:
        """Queued jobs cancel now. Running ones get the flag their handler polls."""
        scoped = (jobs.c.id == job_id, jobs.c.org_id == org_id)
        with self.engine.begin() as c:
            hit = c.execute(jobs.update().where(*scoped, jobs.c.state == "queued")
                            .values(state="cancelled", finished_at=_now()))
            if hit.rowcount == 1:
                return "cancelled"
            state = c.execute(sa.select(jobs.c.state).where(*scoped)).scalar_one_or_none()
            if state == "running":
                c.execute(job_runtime.update().where(job_runtime.c.job_id == job_id)
                          .values(cancel_requested=True))
            return state

    def cancel_requested(self, job_id: str) -> bool:
        with self.engine.connect() as c:
            return bool(c.execute(sa.select(job_runtime.c.cancel_requested)
                                  .where(job_runtime.c.job_id == job_id)).scalar_one_or_none())

    def requeue_stale(self, timeout: float, now: float | None = None) -> list[str]:
        """Recover jobs whose worker stopped beating. Returns the ids requeued."""
        cutoff = (time.time() if now is None else now) - timeout
        requeued: list[str] = []
        with self.engine.begin() as c:
            self._serialize(c, "reaper")
            stale = c.execute(
                sa.select(jobs.c.id, job_runtime.c.attempts, job_runtime.c.cancel_requested)
                .join(job_runtime, job_runtime.c.job_id == jobs.c.id)
                .where(jobs.c.state == "running", job_runtime.c.heartbeat_at < cutoff)).all()
            for job_id, attempts, cancelled in stale:
                c.execute(job_runtime.update().where(job_runtime.c.job_id == job_id)
                          .values(token=None, worker=None, heartbeat_at=None))
                row = jobs.update().where(jobs.c.id == job_id)
                if cancelled:
                    c.execute(row.values(state="cancelled", finished_at=_now()))
                elif attempts >= MAX_ATTEMPTS:
                    c.execute(row.values(state="failed", finished_at=_now(),
                                         error=f"the worker died {attempts} times running this job"))
                else:
                    c.execute(row.values(state="queued", started_at=None))
                    requeued.append(job_id)
        return requeued

    def read_log(self, org_id: str, job_id: str, offset: int = 0,
                 limit: int = LOG_CHUNK) -> dict[str, Any] | None:
        """Tail from a byte offset. next_offset never lands inside a character,
        so a client that keeps passing it back reads clean text."""
        job = self.get(org_id, job_id)
        if job is None:
            return None
        try:
            fd = os.open(log_file(org_id, job_id), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"data": "", "next_offset": offset}
        except (OSError, UnsafePath):
            return None
        with os.fdopen(fd, "rb") as fh:
            fh.seek(offset)
            raw = fh.read(min(limit, LOG_CHUNK))
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        data = decoder.decode(raw)
        pending = decoder.getstate()[0]
        return {"data": data, "next_offset": offset + len(raw) - len(pending)}


def log_file(org_id: str, job_id: str) -> Path:
    """tenants/<org_id>/jobs/<job_id>.log, built so it cannot leave the org."""
    tenants = safe_child(data_root(), "tenants")
    return safe_child(safe_child(safe_child(tenants, org_id), "jobs"), f"{job_id}.log")
