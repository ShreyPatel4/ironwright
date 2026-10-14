"""The platform spine: one app over every workspace.

Where api.create_app binds one pack for the pack-scoped REST surface, this
app is what the console talks to: list workspaces with their derived
verification level, run pipeline stages, read and decide pending approvals,
and verify the audit chain. It opens each workspace's store per request and
closes it after, so workspaces swap freely; a local single-user console
needs no pool.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import time
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import verification
from .cli import STAGES
from .ontology.runtime import Ontology
from .packs import list_packs, load_pack
from .tracing import Tracer

STAGE_ORDER = ["generate", "migrate", "reconcile", "ontology", "agents", "evals", "export"]
DEFAULT_SEEDS = {"retail": 7, "supply_chain": 11, "banking_aml": 23, "insurance_claims": 31}


class RunBody(BaseModel):
    seed: int | None = None
    stages: list[str] | None = None


class ActionBody(BaseModel):
    params: dict[str, Any] = {}
    actor: str = "user:console"
    on_behalf_of: str | None = None
    idempotency_key: str | None = None


class DecideBody(BaseModel):
    approved_by: str
    decision: str = "approve"  # approve | deny


def _pack(name: str):
    if name not in list_packs():
        raise HTTPException(404, f"no workspace named {name}")
    return load_pack(name)


@contextlib.contextmanager
def _ontology(pack) -> Iterator[Ontology]:
    ont = Ontology(pack, Tracer(pack.paths.ensure().traces))
    try:
        yield ont
    finally:
        with contextlib.suppress(Exception):
            ont.store.con.close()


def _pending_approvals(ont: Ontology) -> list[dict[str, Any]]:
    """needs_approval rows with no later applied or denied row for the same
    action and params. The audit log is the only source of truth here."""
    rows = ont.store.con.execute(
        "select seq, actor, action, status, params from audit_log order by seq"
    ).fetchall()
    settled: set[tuple[str, str]] = set()
    pending: dict[tuple[str, str], dict[str, Any]] = {}
    for seq, actor, action, status, params in rows:
        key = (action, params or "null")
        if status == "needs_approval":
            pending[key] = {"seq": seq, "actor": actor, "action": action,
                            "params": json.loads(params) if params else {}}
        elif status in ("applied", "applied_pending_sync", "denied"):
            settled.add(key)
    return [p for k, p in pending.items() if k not in settled]


def create_platform_app() -> FastAPI:
    app = FastAPI(title="ironwright platform",
                  description="One spine over every workspace: runs, approvals, audit, verification.")

    @app.get("/workspaces")
    def workspaces() -> list[dict[str, Any]]:
        out = []
        for name in list_packs():
            pack = load_pack(name)
            out.append({"name": name, "title": pack.title,
                        "decision": pack.spec.get("decision"),
                        "verification": verification.derive(pack)})
        return out

    @app.post("/workspaces/{name}/runs")
    def run(name: str, body: RunBody) -> dict[str, Any]:
        pack = _pack(name)
        stages = body.stages or STAGE_ORDER
        bad = [s for s in stages if s not in STAGES]
        if bad:
            raise HTTPException(422, f"unknown stages {bad}")
        tracer = Tracer(pack.paths.ensure().traces)
        args = argparse.Namespace(seed=body.seed or DEFAULT_SEEDS.get(name, 7), limit=None)
        report = []
        for stage in stages:
            t0 = time.perf_counter()
            STAGES[stage](pack, tracer, args)
            report.append({"stage": stage, "status": "ok",
                           "seconds": round(time.perf_counter() - t0, 2)})
        evals_path = pack.paths.reports / "evals.json"
        evals = json.loads(evals_path.read_text()) if evals_path.exists() else None
        return {"workspace": name, "stages": report,
                "evals": {"passed": evals.get("passed")} if evals else None,
                "verification": verification.derive(pack)}

    @app.get("/workspaces/{name}/approvals")
    def approvals(name: str) -> list[dict[str, Any]]:
        with _ontology(_pack(name)) as ont:
            return _pending_approvals(ont)

    @app.post("/workspaces/{name}/approvals/{seq}/decide")
    def decide(name: str, seq: int, body: DecideBody) -> dict[str, Any]:
        with _ontology(_pack(name)) as ont:
            row = next((p for p in _pending_approvals(ont) if p["seq"] == seq), None)
            if row is None:
                raise HTTPException(404, f"no pending approval with seq {seq}")
            if body.decision == "deny":
                ont.store.audit(actor=body.approved_by, on_behalf_of=row["actor"],
                                action=row["action"], status="denied",
                                params=row["params"], objects=[], before=None,
                                after=None, evidence=[], policy=None,
                                approved_by=body.approved_by, idempotency_key=None,
                                action_run=None)
                return {"status": "denied", "seq": seq}
            result = ont.act(row["action"], row["params"], actor=row["actor"],
                             approved_by=body.approved_by)
            return {"status": result.get("status"), "seq": seq, "result": result}

    @app.post("/workspaces/{name}/actions/{action}")
    def act(name: str, action: str, body: ActionBody) -> dict[str, Any]:
        with _ontology(_pack(name)) as ont:
            return ont.act(action, body.params, actor=body.actor,
                           on_behalf_of=body.on_behalf_of,
                           idempotency_key=body.idempotency_key)

    @app.get("/workspaces/{name}/audit/verify")
    def verify(name: str) -> dict[str, Any]:
        with _ontology(_pack(name)) as ont:
            rows = ont.store.con.execute("select count(*) from audit_log").fetchone()[0]
            return {"intact": ont.store.verify_audit_chain(), "rows": rows}

    return app
