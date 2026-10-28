"""Sketch 07, the honest slice: labels from audit, gated offline evaluation.

Approvals, denials and overrides already live in the hash-chained audit log.
This module replays them into labeled examples and scores a proposed rule
change against those human verdicts, offline. It adopts nothing. Adoption
stays with the reviewed orgpolicy and escalation paths, no model is
fine-tuned here, and nothing retrains silently. The training set is
listable because it IS a filtered audit query: an operator can read every
example the platform would learn from.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .ontology.store import OntologyStore
from .packs import Pack, list_packs, load_pack
from .policy import PolicyEngine
from .workspaces import read_org

LABELS_FILE = "training_labels.jsonl"
SETTLE_STATUSES = ("applied", "applied_pending_sync", "denied")

ADOPTION_NOTE = ("adopted is always false here. This report only scores the proposal "
                 "against prior human verdicts, offline. Adoption happens through the "
                 "reviewed orgpolicy and escalation paths. Nothing retrains silently.")


def _canon(params_json: str | None) -> tuple[str, dict[str, Any]]:
    """One canonical string per params payload, so a request and its
    settlement key the same way regardless of dict order."""
    parsed = json.loads(params_json) if params_json else {}
    if not isinstance(parsed, dict):
        parsed = {"_value": parsed}
    return json.dumps(parsed, sort_keys=True, default=str), parsed


def derive_labels(pack: Pack) -> list[dict[str, Any]]:
    """Labeled examples replayed from the audit log, and nowhere else.

    A needs_approval row paired with its LATER applied or denied settlement
    for the same action and params becomes one label. Seq order decides the
    pairing the same way the approvals inbox does: one open request per
    action+params key, a re-request replaces the earlier open one (which
    then can never settle, so it never becomes a label), and a settlement
    closes the request the inbox was showing. A denied row only settles a
    request when a human signed it (approved_by is set); a denied row with
    no open request behind it is a straight policy denial and becomes a
    denied_by_policy label carrying the deciding rule. Unsettled requests
    are not labels.
    """
    db = pack.paths.ontology_db
    if not db.exists():
        return []
    store = OntologyStore(db)
    try:
        rows = store.con.execute(
            "select seq, actor, action, status, params, approved_by, policy "
            "from audit_log order by seq").fetchall()
    finally:
        store.con.close()

    open_req: dict[tuple[str, str], dict[str, Any]] = {}
    labels: list[dict[str, Any]] = []
    for seq, actor, action, status, params, approved_by, policy in rows:
        canon, parsed = _canon(params)
        key = (action, canon)
        if status == "needs_approval":
            open_req[key] = {"seq": seq, "actor": actor, "params": parsed}
            continue
        if status not in SETTLE_STATUSES:
            continue
        req = open_req.get(key)
        settles = req is not None and (status != "denied" or approved_by is not None)
        if settles:
            del open_req[key]
            labels.append({"action": action, "params": req["params"],
                           "requested_by": req["actor"], "decided_by": approved_by,
                           "verdict": "denied" if status == "denied" else "approved",
                           "rule": None, "seq": req["seq"], "settled_seq": seq})
        elif status == "denied" and approved_by is None:
            pol = json.loads(policy) if policy else {}
            labels.append({"action": action, "params": parsed,
                           "requested_by": actor, "decided_by": None,
                           "verdict": "denied_by_policy",
                           "rule": (pol or {}).get("reason"),
                           "seq": seq, "settled_seq": seq})
    labels.sort(key=lambda lb: lb["settled_seq"])
    return labels


def _check_proposal(proposal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(proposal, list):
        raise ValueError("the proposal must be a list of rules")
    for r in proposal:
        if not isinstance(r, dict) or r.get("effect") not in ("permit", "forbid"):
            raise ValueError("every proposed rule must be an object with effect "
                             "permit or forbid, in the policies.yaml rule schema")
    return proposal


def evaluate(pack: Pack, proposal: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a proposed rule change against prior human verdicts, offline.

    The engine under test is the pack's hand-written rules plus the
    proposal. Every label's (actor, action, params) replays through
    authorize. An approved label agrees when the engine permits; a denied
    or denied_by_policy label agrees when it denies. The replay env carries
    params, principal and a clock; a when clause over object state cannot
    evaluate and skips its rule, the same fail-closed way the live engine
    treats a clause that cannot evaluate.

    adopted is ALWAYS false. Adoption happens only through the reviewed
    orgpolicy and escalation paths. Nothing retrains silently.
    """
    proposal = _check_proposal(proposal or [])
    labels = derive_labels(pack)
    engine = PolicyEngine({"default": "deny",
                           "rules": list(pack.policies.get("rules", [])) + proposal})
    now = datetime.now(timezone.utc).isoformat()
    agreements = 0
    disagreements: list[dict[str, Any]] = []
    for lb in labels:
        env = {"params": lb["params"], "principal": lb["requested_by"], "now": now}
        decision = engine.authorize(lb["requested_by"], f"action:{lb['action']}", env)
        got = "permit" if decision.allowed else "deny"
        expected = "permit" if lb["verdict"] == "approved" else "deny"
        if got == expected:
            agreements += 1
        else:
            disagreements.append({"seq": lb["seq"], "action": lb["action"],
                                  "expected": expected, "got": got})
    n = len(labels)
    return {"labels": n, "agreements": agreements, "disagreements": disagreements,
            "agreement_rate": round(agreements / n, 4) if n else None,
            "adopted": False, "note": ADOPTION_NOTE}


def write_labels(pack: Pack) -> dict[str, Any]:
    """Export the labels to reports/training_labels.jsonl.

    A whole-file atomic rewrite, never an append, derived purely from the
    audit log: a rerun over the same audit log gives byte-identical output.
    """
    labels = derive_labels(pack)
    reports = pack.paths.ensure().reports
    path = reports / LABELS_FILE
    body = "".join(json.dumps(lb, sort_keys=True, default=str) + "\n" for lb in labels)
    fd, tmp = tempfile.mkstemp(dir=reports, prefix=f".{LABELS_FILE}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    return {"path": str(path), "labels": len(labels)}


# ----------------------------------------------------------------- router

class EvaluateBody(BaseModel):
    rules: list[dict[str, Any]] = []


router = APIRouter()


def _pack_or_404(name: str) -> Pack:
    if name in list_packs():
        return load_pack(name)
    try:
        read_org(name)
    except FileNotFoundError:
        raise HTTPException(404, f"no workspace named {name}")
    raise HTTPException(404, f"{name} is a created organization with no ontology "
                             "store, so it has no audit rows to replay into labels")


@router.get("/workspaces/{name}/training/labels")
def training_labels(name: str) -> dict[str, Any]:
    pack = _pack_or_404(name)
    labels = derive_labels(pack)
    return {"workspace": name, "labels": labels, "count": len(labels),
            "note": "Every label is a filtered audit query: a human approval, a "
                    "human denial, or a straight policy denial. Unsettled "
                    "requests are not labels."}


@router.post("/workspaces/{name}/training/evaluate")
def training_evaluate(name: str, body: EvaluateBody) -> dict[str, Any]:
    pack = _pack_or_404(name)
    try:
        return {"workspace": name, **evaluate(pack, body.rules)}
    except ValueError as e:
        raise HTTPException(422, str(e))
