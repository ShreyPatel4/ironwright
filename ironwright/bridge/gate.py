"""The promotion gate. The model writes code. It never decides that its code ships.

```mermaid
stateDiagram-v2
  [*] --> Drafted: agent writes spec, code, tests
  Drafted --> Staged: runs in a sandbox on a sample
  Staged --> Evaluated: evaluator and compliance score it
  Evaluated --> Drafted: below threshold, agent retries (max 3)
  Evaluated --> Escalated: supervisor packages evidence
  Escalated --> Approved: human approves in the inbox
  Escalated --> Drafted: human rejects with a reason
  Approved --> Promoted: supervisor watches the deploy
  Promoted --> Observed: monitors on
  Observed --> Drafted: drift or a broken contract reopens it
```

Every transition is checked against the arrows above, written as one span and one row
in a hash-chained audit log, and persisted in the run record. The gate is generic over a
staged pipeline change: a layer, its model files, their contract, tests and DQ rules.

Staging runs in an in-memory DuckDB with its own catalog, `sandbox_<run>`, on a sample of
bronze. It never opens a live store or a models folder. promote() is the only road to a
live models folder, and it walks the fixed act() order: idempotency, policy, params,
preconditions, approval, edits, outbox, audit. Policy runs before validation and denies
by default. The approval is checked again here, so a decision written around decide()
does not count.
"""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from .. import auth
from ..config import private_dir
from ..policy import PolicyEngine
from ..sqlguard import SqlRejected, apply_limits, guard_select
from ..tenancy.models import ROLES, at_least
from ..tracing import Tracer

MAX_ATTEMPTS = 3
APPROVER_ROLE = "admin"
LAYERS = ("silver", "gold")
SAMPLE_ROWS = 200
PROMOTE_STEPS = ("idempotency", "policy", "params", "preconditions", "approval", "edits", "outbox", "audit")

_MODEL_FILE = re.compile(r"[a-z0-9][a-z0-9_]{0,62}\.sql")
_IDENT = re.compile(r"[a-z][a-z0-9_]{0,62}")
_RUN_ID = re.compile(r"run-[0-9a-f]{10}")
_CARD_ID = re.compile(r"card-[0-9a-f]{12}")


class GateError(Exception):
    """A refusal. The message is ours and names no cell value, so it is safe to show."""


class State(str, Enum):
    DRAFTED = "drafted"
    STAGED = "staged"
    EVALUATED = "evaluated"
    ESCALATED = "escalated"
    APPROVED = "approved"
    PROMOTED = "promoted"
    OBSERVED = "observed"


TRANSITIONS: dict[State | None, frozenset[State]] = {
    None: frozenset({State.DRAFTED}),
    State.DRAFTED: frozenset({State.STAGED}),
    State.STAGED: frozenset({State.EVALUATED}),
    State.EVALUATED: frozenset({State.DRAFTED, State.ESCALATED}),
    State.ESCALATED: frozenset({State.APPROVED, State.DRAFTED}),
    State.APPROVED: frozenset({State.PROMOTED}),
    State.PROMOTED: frozenset({State.OBSERVED}),
    State.OBSERVED: frozenset({State.DRAFTED}),
}


def allowed(frm: State | str | None, to: State | str) -> bool:
    frm = State(frm) if frm is not None else None
    return State(to) in TRANSITIONS[frm]


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def model_table(file_name: str) -> str:
    """01_claims.sql -> claims, the same rule the workspace models use."""
    stem = file_name[: -len(".sql")]
    return stem.split("_", 1)[1] if stem[:2].isdigit() and "_" in stem else stem


# ---------------------------------------------------------------- the change

@dataclass
class Change:
    """One staged pipeline change. Any drafter can produce it: the silver builder from the
    silver draft today, the bridge planner later."""
    layer: str
    models: dict[str, str]
    contract: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tests: list[dict[str, str]] = field(default_factory=list)
    dq: list[dict[str, str]] = field(default_factory=list)
    masking: dict[str, dict[str, str]] = field(default_factory=dict)
    purpose: str = ""
    agent: str = ""
    requested_by: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Change:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def content_hash(self) -> str:
        """What an approval signs. The note is narration, so it is left out."""
        body = {k: v for k, v in self.to_dict().items() if k != "note"}
        return hashlib.sha256(_j(body).encode()).hexdigest()

    def tables(self) -> list[str]:
        return [model_table(f) for f in sorted(self.models)]

    def validate(self) -> None:
        if self.layer not in LAYERS:
            raise GateError(f"a change lands in {' or '.join(LAYERS)}, not {self.layer!r}")
        if not self.models:
            raise GateError("a change carries at least one model")
        for name, sql in self.models.items():
            if not isinstance(name, str) or not _MODEL_FILE.fullmatch(name):
                raise GateError(f"bad model file name {name!r}")
            if not _IDENT.fullmatch(model_table(name)):
                raise GateError(f"bad model table name in {name!r}")
            try:
                guard_select(sql)
            except SqlRejected as e:
                raise GateError(f"model {name}: {e}") from e
        for t in self.tests:
            try:
                guard_select(t["sql"])
            except (SqlRejected, KeyError, TypeError) as e:
                raise GateError(f"test {t.get('name')!r}: must be one select") from e
        if not auth.is_agent(self.agent):
            raise GateError("a change is drafted by an agent principal")
        if not self.requested_by or auth.is_agent(self.requested_by):
            raise GateError("a change is requested by a human principal")


# ---------------------------------------------------------------- the store

# One writer at a time per bridge folder. A thread lock per folder inside this process, and
# an flock on a file in the folder across processes. Reentrant per thread, because decide()
# holds it across the transition and the audit row it writes.
_FOLDER_LOCKS: dict[str, threading.RLock] = {}
_FOLDER_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


@contextmanager
def _folder_lock(root: Path) -> Iterator[None]:
    key = str(root)
    with _FOLDER_LOCKS_GUARD:
        rlock = _FOLDER_LOCKS.setdefault(key, threading.RLock())
    with rlock:
        held: dict[str, tuple[int, int]] = getattr(_HELD, "held", None) or {}
        _HELD.held = held
        depth, fd = held.get(key, (0, -1))
        if depth == 0:
            fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        held[key] = (depth + 1, fd)
        try:
            yield
        finally:
            depth, fd = held[key]
            if depth == 1:
                del held[key]
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            else:
                held[key] = (depth - 1, fd)


class BridgeStore:
    """One folder per workspace. Runs are JSON, cards and the outbox are append-only JSONL,
    and the audit log is hash-chained JSONL, so a tenant workspace with no ontology store
    still has a tamper-evident record."""

    def __init__(self, root: Path):
        self.root = private_dir(Path(root))
        private_dir(self.root / "runs")
        self.tracer = Tracer(self.root / "traces")

    def locked(self):
        return _folder_lock(self.root)

    # audit
    def audit(self, **rec: Any) -> dict[str, Any]:
        # seq is read then written: two writers taking the same seq would break the chain
        # for an honest reason the verifier cannot tell from tampering.
        with self.locked():
            path = self.root / "audit.jsonl"
            rows = self.audit_rows()
            prev = rows[-1]["hash"] if rows else "genesis"
            row = {**rec, "seq": len(rows) + 1, "ts": round(time.time(), 6), "prev_hash": prev}
            row["hash"] = hashlib.sha256((prev + _j(row)).encode()).hexdigest()
            with open(path, "a") as f:
                f.write(_j(row) + "\n")
            return row

    def audit_rows(self) -> list[dict[str, Any]]:
        return self._lines("audit.jsonl")

    def verify_audit_chain(self) -> bool:
        prev = "genesis"
        for row in self.audit_rows():
            body = {k: v for k, v in row.items() if k != "hash"}
            if row.get("prev_hash") != prev or hashlib.sha256((prev + _j(body)).encode()).hexdigest() != row.get("hash"):
                return False
            prev = row["hash"]
        return True

    # runs
    def _run_path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise GateError("no such run")
        return self.root / "runs" / f"{run_id}.json"

    def run(self, run_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._run_path(run_id).read_text())
        except FileNotFoundError as e:
            raise GateError("no such run") from e

    def put_run(self, run: dict[str, Any]) -> None:
        path = self._run_path(run["run_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_j(run))
        tmp.replace(path)

    def runs(self) -> list[dict[str, Any]]:
        out = [json.loads(p.read_text()) for p in (self.root / "runs").glob("run-*.json")]
        return sorted(out, key=lambda r: r.get("created", 0))

    # cards
    def append_card(self, rec: dict[str, Any]) -> None:
        with open(self.root / "cards.jsonl", "a") as f:
            f.write(_j(rec) + "\n")

    def card(self, card_id: str) -> dict[str, Any]:
        if not isinstance(card_id, str) or not _CARD_ID.fullmatch(card_id):
            raise GateError("no such card")
        found = [c for c in self._lines("cards.jsonl") if c.get("card_id") == card_id]
        if not found:
            raise GateError("no such card")
        return found[-1]

    def cards(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for c in self._lines("cards.jsonl"):
            latest[c["card_id"]] = c
        return sorted(latest.values(), key=lambda c: c.get("created", 0))

    # outbox and idempotency
    def append_outbox(self, rec: dict[str, Any]) -> None:
        with open(self.root / "outbox.jsonl", "a") as f:
            f.write(_j(rec) + "\n")

    def outbox(self) -> list[dict[str, Any]]:
        return self._lines("outbox.jsonl")

    def idempotent(self, key: str) -> dict[str, Any] | None:
        return next((r["result"] for r in self._lines("idempotency.jsonl") if r["key"] == key), None)

    def remember(self, key: str, result: dict[str, Any]) -> None:
        with open(self.root / "idempotency.jsonl", "a") as f:
            f.write(_j({"key": key, "result": result}) + "\n")

    def _lines(self, name: str) -> list[dict[str, Any]]:
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- transitions

def transition(store: BridgeStore, run_id: str, to: State, actor: str, **detail: Any) -> dict[str, Any]:
    with store.locked():
        return _transition(store, run_id=run_id, to=to, actor=actor, **detail)


def _transition(store: BridgeStore, run_id: str, to: State, actor: str, **detail: Any) -> dict[str, Any]:
    run = store.run(run_id)
    frm = run.get("state")
    if not allowed(frm, to):
        raise GateError(f"a run cannot go from {frm or 'nothing'} to {State(to).value}")
    with store.tracer.span("bridge.transition", run_id=run_id, frm=frm, to=State(to).value, actor=actor) as sp:
        run["state"] = State(to).value
        run["history"].append({"from": frm, "to": State(to).value, "actor": actor, "ts": time.time(),
                               **{k: v for k, v in detail.items() if k in ("reason", "card_id", "attempt")}})
        store.put_run(run)
        row = store.audit(kind="transition", run_id=run_id, actor=actor, status=State(to).value,
                          to=State(to).value, **{"from": frm}, **detail)
        sp.set(audit_seq=row["seq"])
    return run


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:10]}"


def open_run(store: BridgeStore, change: Change, sample_rows: int = SAMPLE_ROWS,
             source: dict[str, Any] | None = None, run_id: str | None = None) -> dict[str, Any]:
    change.validate()
    run_id = run_id or new_run_id()
    store.put_run({"run_id": run_id, "layer": change.layer, "agent": change.agent,
                   "requested_by": change.requested_by, "tables": change.tables(), "state": None,
                   "attempt": 1, "cycle": 1, "change": change.to_dict(), "content_hash": change.content_hash(),
                   "sample_rows": int(sample_rows), "source": source or {}, "history": [],
                   "evaluation": None, "evaluations": [], "card_id": None, "cards": [],
                   "created": time.time()})
    return transition(store, run_id, State.DRAFTED, actor=change.agent, attempt=1)


def set_change(store: BridgeStore, run_id: str, change: Change) -> dict[str, Any]:
    """A redraft. Only a Drafted run takes a new change."""
    change.validate()
    run = store.run(run_id)
    if run["state"] != State.DRAFTED:
        raise GateError("only a drafted run takes a new change")
    if change.requested_by != run["requested_by"] or change.layer != run["layer"]:
        raise GateError("a redraft keeps the requester and the layer")
    run.update(change=change.to_dict(), content_hash=change.content_hash(), tables=change.tables())
    store.put_run(run)
    store.audit(kind="draft", run_id=run_id, actor=change.agent, status="drafted", attempt=run["attempt"],
                content_hash=run["content_hash"])
    return run


def record_evaluation(store: BridgeStore, run_id: str, scores: dict[str, Any], actor: str) -> dict[str, Any]:
    run = store.run(run_id)
    if scores.get("content_hash") != run["content_hash"]:
        raise GateError("the scores are for another version of this change")
    run["evaluation"] = scores
    run["evaluations"].append({"attempt": run["attempt"], "cycle": run["cycle"], "passed": scores["passed"],
                               "failed": [g for g, s in scores["gates"].items() if not s["passed"]],
                               "evidence": scores["evidence"], "content_hash": scores["content_hash"],
                               "note": run["change"].get("note", "")})
    store.put_run(run)
    return transition(store, run_id, State.EVALUATED, actor=actor, passed=scores["passed"],
                      evidence=scores["evidence"])


def retry(store: BridgeStore, run_id: str, actor: str) -> dict[str, Any]:
    run = store.run(run_id)
    if run["state"] != State.EVALUATED or (run["evaluation"] or {}).get("passed"):
        raise GateError("only a failed evaluation loops back")
    if run["attempt"] >= MAX_ATTEMPTS:
        raise GateError(f"{MAX_ATTEMPTS} attempts are spent: escalate it as stuck")
    out = transition(store, run_id, State.DRAFTED, actor=actor, reason="below threshold, retry",
                     attempt=run["attempt"] + 1)
    out["attempt"] = run["attempt"] + 1
    store.put_run(out)
    return out


# ---------------------------------------------------------------- the sandbox

class Staged:
    """A change run in its own in-memory catalog. Close it when the evaluator is done."""

    def __init__(self, con: duckdb.DuckDBPyConnection, schema: str, bronze: dict[str, pa.Table],
                 tables: dict[str, dict[str, Any]], errors: dict[str, str]):
        self.con, self.schema, self.bronze, self.tables, self.errors = con, schema, bronze, tables, errors

    @property
    def inputs(self) -> dict[str, int]:
        return {n: t.num_rows for n, t in self.bronze.items()}

    def rows(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        if table not in self.tables:
            return []
        sql = f'select * from "{table}"' + (f" limit {int(limit)}" if limit else "")
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def raw_pii(self, pii: dict[str, str]) -> dict[str, str]:
        """{value: class} for every value of a PII-tagged column in the bronze sample."""
        out: dict[str, str] = {}
        for t in self.bronze.values():
            for name in t.column_names:
                if name in pii:
                    out.update({str(v): pii[name] for v in t.column(name).to_pylist() if v is not None})
        return out

    def pii_columns(self, table: str, pii: dict[str, str]) -> dict[str, str]:
        """{column: class} for an output table. A column is PII by its name, or because its
        values are raw PII values from bronze, so a rename carries nothing past the gate."""
        raw = self.raw_pii(pii)
        rows = self.rows(table)
        out: dict[str, str] = {}
        for col in self.tables.get(table, {}).get("columns", []):
            name = col["name"]
            if name in pii:
                out[name] = pii[name]
                continue
            hit = next((raw[str(r[name])] for r in rows if r.get(name) is not None and str(r[name]) in raw), None)
            if hit:
                out[name] = hit
        return out

    def samples(self, pii: dict[str, str], n: int = 5) -> dict[str, Any]:
        """Before and after rows for the card, with every PII column masked. The card goes
        to a human inbox, so it must not leak what the change was meant to protect."""
        def mask(row: dict[str, Any], cols: dict[str, str] | set[str]) -> dict[str, Any]:
            return {k: ("[masked]" if k in cols and v is not None else _plain(v)) for k, v in row.items()}
        before = {name: [mask(r, pii) for r in t.slice(0, n).to_pylist()] for name, t in sorted(self.bronze.items())}
        after = {t: [mask(r, self.pii_columns(t, pii)) for r in self.rows(t, n)] for t in sorted(self.tables)}
        return {"before": before, "after": after}

    def close(self) -> None:
        self.con.close()


def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def sample(bronze: dict[str, pa.Table], rows: int) -> dict[str, pa.Table]:
    return {name: t.slice(0, max(0, int(rows))) for name, t in bronze.items()}


def stage(change: Change | dict[str, Any], bronze: dict[str, pa.Table], run_id: str,
          sample_rows: int = SAMPLE_ROWS, upstream: dict[str, dict[str, str]] | None = None) -> Staged:
    """Run the models on a sample in `sandbox_<run>`, a catalog that lives in memory and
    dies with the connection. External access is off before any model runs.

    `upstream` holds the live models of the layers below, by layer. They run first on the
    same sample, under the names a run gives them (`silver_<table>`), so a gold mart stages
    over the silver it will read. Only the change's own models are scored."""
    change = Change.from_dict(change) if isinstance(change, dict) else change
    change.validate()
    schema = f"sandbox_{re.sub(r'[^a-z0-9_]', '_', run_id.lower())}"
    taken = sample(bronze, sample_rows)
    con = duckdb.connect(":memory:")
    try:
        apply_limits(con)
        con.execute(f'attach \':memory:\' as "{schema}"')
        con.execute(f'use "{schema}"')
        for name, t in sorted(taken.items()):
            if not _IDENT.fullmatch(name):
                raise GateError(f"bad source name {name!r}")
            con.register(f"_in_{name}", t)
            con.execute(f'create view "bronze_{name}" as select * from "_in_{name}"')
        con.execute(f'create schema if not exists "{change.layer}"')
        con.execute("set enable_external_access = false")
        con.execute("set lock_configuration = true")
        tables: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        below = [layer for layer in LAYERS if LAYERS.index(layer) < LAYERS.index(change.layer)]
        for layer in below:
            for file_name, sql in sorted((upstream or {}).get(layer, {}).items()):
                table = model_table(file_name)
                if not _MODEL_FILE.fullmatch(file_name) or not _IDENT.fullmatch(table):
                    raise GateError(f"bad upstream model file name {file_name!r}")
                try:
                    con.execute(f'create table "{layer}_{table}" as {guard_select(sql)}')
                except (duckdb.Error, SqlRejected) as e:
                    errors[f"{layer}/{file_name}"] = f"the live {layer} model did not run ({type(e).__name__})"
        for file_name in sorted(change.models):
            table = model_table(file_name)
            try:
                con.execute(f'create table "{table}" as {change.models[file_name]}')
                con.execute(f'create view "{change.layer}"."{table}" as select * from "{table}"')
                con.execute(f'create view "{change.layer}_{table}" as select * from "{table}"')
            except duckdb.Error as e:  # DuckDB's text can quote a cell, so only its kind leaves
                errors[file_name] = f"the model did not run ({type(e).__name__})"
                continue
            cols = con.execute(f"describe \"{table}\"").fetchall()
            n = con.execute(f'select count(*) from "{table}"').fetchone()[0]
            tables[table] = {"rows": int(n), "columns": [{"name": c[0], "type": str(c[1]).lower()} for c in cols]}
    except Exception:
        con.close()
        raise
    return Staged(con, schema, taken, tables, errors)


# ---------------------------------------------------------------- the card

def _diff(live: dict[str, str], models: dict[str, str]) -> dict[str, str]:
    out = {}
    for name in sorted(models):
        old = live.get(name, "")
        if old == models[name]:
            continue
        out[name] = "".join(difflib.unified_diff(old.splitlines(True), models[name].splitlines(True),
                                                 fromfile=f"live/{name}", tofile=f"staged/{name}"))
    return out


def package_card(store: BridgeStore, run_id: str, staged: Staged, scores: dict[str, Any],
                 live_models: dict[str, str], pii: dict[str, str], actor: str = "agent:supervisor") -> dict[str, Any]:
    with store.locked():
        return _package_card(store, run_id=run_id, staged=staged, scores=scores, live_models=live_models, pii=pii, actor=actor)


def _package_card(store: BridgeStore, run_id: str, staged: Staged, scores: dict[str, Any],
                 live_models: dict[str, str], pii: dict[str, str], actor: str = "agent:supervisor") -> dict[str, Any]:
    """ONE approval card per escalation: diff, before and after samples, scores, lineage,
    compliance and evidence ids. A run whose evaluation failed escalates as stuck, with
    every attempt it made."""
    run = store.run(run_id)
    passed = bool(scores.get("passed"))
    if not passed and run["attempt"] < MAX_ATTEMPTS:
        # A failed evaluation with attempts left loops back to the agent. Only the third
        # failure goes to a human, as stuck.
        raise GateError(f"a failed evaluation retries first ({run['attempt']} of {MAX_ATTEMPTS})")
    card_id = "card-" + hashlib.sha1(f"{run_id}:{run['cycle']}:{run['content_hash']}".encode()).hexdigest()[:12]
    existing = next((c for c in store.cards() if c["card_id"] == card_id), None)
    if existing:
        return existing
    with store.tracer.span("supervisor.package_card", run_id=run_id, card_id=card_id) as sp:
        change = run["change"]
        card = {
            "card_id": card_id, "run_id": run_id, "kind": "promotion" if passed else "stuck",
            "layer": run["layer"], "agent": run["agent"], "requested_by": run["requested_by"],
            "content_hash": run["content_hash"], "tables": run["tables"],
            "diff": _diff(live_models, change["models"]),
            "samples": staged.samples(pii),
            "staged": {"schema": staged.schema, "inputs": staged.inputs, "tables": staged.tables,
                       "errors": staged.errors},
            "scores": {g: {k: v for k, v in s.items()} for g, s in scores["gates"].items()},
            "passed": passed,
            "lineage": {**scores["gates"]["lineage"], "contract": change.get("contract", {})},
            "compliance": scores["gates"]["compliance"],
            "evidence": scores["evidence"],
            "attempts": run["evaluations"],
            "purpose": change.get("purpose", ""),
            "note": change.get("note", ""),
            "decision": None, "created": time.time(),
        }
        store.append_card(card)
        run["card_id"] = card_id
        run["cards"].append(card_id)
        store.put_run(run)
        sp.set(kind=card["kind"])
    transition(store, run_id, State.ESCALATED, actor=actor, card_id=card_id, card_kind=card["kind"],
               evidence=scores["evidence"])
    return card


# ---------------------------------------------------------------- the human decision

def _check_decider(by: str, role: str, card: dict[str, Any], approving: bool) -> None:
    if not isinstance(by, str) or not by.strip():
        raise GateError("a decision names the human who made it")
    if auth.is_agent(by) or by == card.get("agent"):
        raise GateError("an agent cannot sign a decision")
    if approving and by == card.get("requested_by"):
        raise GateError("the requester cannot approve their own change")
    if role not in ROLES or not at_least(role, APPROVER_ROLE):
        raise GateError(f"approving a promotion needs the {APPROVER_ROLE} role")


def decide(store: BridgeStore, card_id: str, by: str, role: str, decision: str, content_hash: str,
           reason: str = "") -> dict[str, Any]:
    with store.locked():
        return _decide(store, card_id=card_id, by=by, role=role, decision=decision, content_hash=content_hash, reason=reason)


def _decide(store: BridgeStore, card_id: str, by: str, role: str, decision: str, content_hash: str,
           reason: str = "") -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise GateError("decision must be approve or reject")
    card = store.card(card_id)
    if card.get("decision") is not None:
        raise GateError("this card is already decided")
    run = store.run(card["run_id"])
    _check_decider(by, role, card, approving=decision == "approve")
    if not (content_hash == card["content_hash"] == run["content_hash"]):
        raise GateError("the hash you signed is not the change on this card")
    if run["state"] != State.ESCALATED or run.get("card_id") != card_id:
        raise GateError("this card is not the one waiting on this run")
    ev = run.get("evaluation") or {}
    if decision == "approve" and not (ev.get("passed") and ev.get("content_hash") == content_hash):
        raise GateError("a change whose evaluation failed cannot be approved")
    if decision == "reject" and not str(reason).strip():
        raise GateError("a rejection carries a reason for the agent")
    rec = {"decision": decision, "by": by, "role": role, "content_hash": content_hash,
           "reason": str(reason)[:500], "ts": _now()}
    with store.tracer.span("bridge.decide", run_id=run["run_id"], card_id=card_id, decision=decision, by=by):
        store.append_card({**card, "decision": rec})
        if decision == "approve":
            transition(store, run["run_id"], State.APPROVED, actor=by, card_id=card_id, approved_by=by)
        else:
            transition(store, run["run_id"], State.DRAFTED, actor=by, card_id=card_id, reason=rec["reason"])
            run = store.run(run["run_id"])
            run.update(cycle=run["cycle"] + 1, attempt=1, rejected=rec)
            store.put_run(run)
    return rec


# ---------------------------------------------------------------- promote, in the fixed act() order

def _file_writer(live_dir: Path) -> Callable[[str, dict[str, str]], None]:
    def write(layer: str, files: dict[str, str]) -> None:
        folder = private_dir(Path(live_dir) / layer)
        for name, sql in files.items():
            tmp = folder / f".{name}.tmp"
            tmp.write_text(sql)
            tmp.replace(folder / name)
    return write


def promote(store: BridgeStore, run_id: str, policy: dict[str, Any], live_dir: Path | None = None,
            writer: Callable[[str, dict[str, str]], None] | None = None,
            actor: str | None = None) -> dict[str, Any]:
    with store.locked():
        return _promote(store, run_id=run_id, policy=policy, live_dir=live_dir, writer=writer, actor=actor)


def _promote(store: BridgeStore, run_id: str, policy: dict[str, Any], live_dir: Path | None = None,
            writer: Callable[[str, dict[str, str]], None] | None = None,
            actor: str | None = None) -> dict[str, Any]:
    """The sub-agent applies the approved change. Nothing here trusts the run record alone:
    the approval and the hash are checked again from the card store."""
    if writer is None:
        if live_dir is None:
            raise GateError("promote needs a live folder or a writer")
        writer = _file_writer(live_dir)
    run = store.run(run_id)
    actor = actor or run["agent"]
    steps: list[str] = []

    def refuse(step: str, why: str) -> None:
        store.audit(kind="promote", run_id=run_id, actor=actor, status="refused", step=step, reason=why)
        raise GateError(why)

    with store.tracer.span("bridge.promote", run_id=run_id, actor=actor, layer=run["layer"]) as sp:
        # 1. idempotency
        key = f"promote:{run_id}:{run['content_hash']}"
        steps.append("idempotency")
        prior = store.idempotent(key)
        if prior:
            sp.event("idempotent_replay", key=key)
            return {**prior, "replayed": True}
        # 2. policy, before anything about the change is read
        steps.append("policy")
        decision = PolicyEngine(policy or {}).authorize(
            actor, f"action:bridge.promote.{run['layer']}",
            {"principal": actor, "layer": run["layer"], "tables": run["tables"], "now": _now()})
        if not decision.allowed:
            sp.event("policy.denied", rule=decision.reason)
            store.audit(kind="promote", run_id=run_id, actor=actor, status="denied", step="policy",
                        reason=decision.reason)
            return {"status": "denied", "reason": decision.reason, "steps": steps, "run_id": run_id}
        sp.event("policy.allowed", rule=decision.reason)
        # 3. params
        steps.append("params")
        try:
            change = Change.from_dict(run["change"])
            change.validate()
        except (GateError, TypeError) as e:
            refuse("params", f"invalid change: {e}")
        # 4. preconditions
        steps.append("preconditions")
        if run["state"] != State.APPROVED:
            refuse("preconditions", f"promote needs the run Approved, and it is {run['state']}")
        ev = run.get("evaluation") or {}
        if not (change.content_hash() == run["content_hash"] == ev.get("content_hash")):
            refuse("preconditions", "the change no longer matches the hash that was evaluated")
        if not ev.get("passed"):
            refuse("preconditions", "the evaluation did not pass")
        # 5. approval, from the card store, checked again
        steps.append("approval")
        try:
            card = store.card(run.get("card_id") or "")
        except GateError:
            refuse("approval", "there is no card for this run")
        d = card.get("decision") or {}
        if d.get("decision") != "approve":
            refuse("approval", "there is no human approval on this card")
        try:
            _check_decider(d.get("by", ""), d.get("role", ""), card, approving=True)
        except GateError as e:
            refuse("approval", str(e))
        if not (d.get("content_hash") == card["content_hash"] == change.content_hash()):
            refuse("approval", "the approval signed another hash")
        # 6. edits
        steps.append("edits")
        writer(change.layer, dict(change.models))
        # 7. outbox
        steps.append("outbox")
        ob = {"id": f"ob-{uuid.uuid4().hex[:10]}", "run_id": run_id, "layer": change.layer,
              "files": sorted(change.models), "content_hash": change.content_hash(), "status": "pending",
              "ts": time.time()}
        store.append_outbox(ob)
        # 8. audit
        steps.append("audit")
        row = store.audit(kind="promote", run_id=run_id, actor=actor, status="promoted", approved_by=d["by"],
                          content_hash=change.content_hash(), files=sorted(change.models), outbox=ob["id"],
                          evidence=ev.get("evidence", []))
        result = {"status": "promoted", "steps": steps, "run_id": run_id, "approved_by": d["by"],
                  "files": sorted(change.models), "audit_seq": row["seq"], "outbox": ob["id"]}
        store.remember(key, result)
        transition(store, run_id, State.PROMOTED, actor=actor, approved_by=d["by"])
        sp.set(status="promoted")
        return result


def observe(store: BridgeStore, run_id: str, monitors: list[str], actor: str = "agent:supervisor") -> dict[str, Any]:
    run = transition(store, run_id, State.OBSERVED, actor=actor, monitors=monitors)
    run["monitors"] = list(monitors)
    store.put_run(run)
    return run


def reopen(store: BridgeStore, run_id: str, reason: str, actor: str = "agent:supervisor") -> dict[str, Any]:
    """A breach reopens the item. It never quietly patches production."""
    run = transition(store, run_id, State.DRAFTED, actor=actor, reason=reason)
    run.update(cycle=run["cycle"] + 1, attempt=1, reopened=reason)
    store.put_run(run)
    return run
