"""Sketch 05: escalation at any grain.

A denial becomes a door instead of a wall. A denied principal raises the
exact decision context to the role that owns the object. The owner's grant
is itself policy: a narrow permit for this principal, this action, this
object id, with an expiry the engine enforces through its when clause over
env.now. The grant lands through the sketch 04 apply path, so there is no
bypass; a hand-written forbid still beats it inside the engine.

Records are append-only JSONL, one line per moment (escalated, granted,
denied). Organizations keep the file in their workspace dir; packs keep it
under their reports dir in the data root, never in ironwright/packs/.
When an ontology store exists for the workspace every moment is also a row
in the hash-chained audit log. Org workspaces have no store, so for them
the JSONL log is the record, and the response says so.

Expiry honesty: the expression engine compares ISO strings, so a rule with
when "now < '2026-11-01'" is enforced whenever the caller passes env.now.
Without a clock in the env the comparison fails and the engine denies, so
the grant fails closed. orgpolicy.active_rules() additionally drops
expired grants at load time.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import orgpolicy
from .config import PackPaths, data_root
from .packs import list_packs, load_pack

RECORD_FILE = "escalations.jsonl"

_PRINCIPAL = re.compile(r"^[a-z]+:[A-Za-z0-9_.\-]{1,64}$")
_ACTION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OBJECT = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_RULE = re.compile(r"^[A-Za-z0-9_.:\-* ]{1,120}$")
# ASCII digits only: \d matches unicode digits, and a non-ASCII "date" would
# compare greater than every ISO timestamp forever, a grant that never expires.
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _valid_date(value: str) -> bool:
    if not _DATE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


class EscalateBody(BaseModel):
    principal: str
    action: str
    object_id: str
    denying_rule: str


class EscalationDecideBody(BaseModel):
    decision: str  # grant | deny
    decided_by: str
    expires: str | None = None  # iso date, required for grants
    reason: str | None = None


def _workspace(name: str) -> tuple[str, Path]:
    """(kind, record path). Packs record under their data-root reports dir,
    organizations in their workspace dir. Unknown names are a 404."""
    if name in list_packs():
        d = PackPaths(name).reports
        d.mkdir(parents=True, exist_ok=True)
        return "pack", d / RECORD_FILE
    d = data_root() / name
    if (d / "org.json").exists():
        return "organization", d / RECORD_FILE
    raise HTTPException(404, f"no workspace named {name}")


def _moments(path: Path) -> list[dict[str, Any]]:
    """Parse the JSONL record, skipping lines that are not valid JSON objects.
    A truncated or hand-damaged line must not brick every escalation endpoint
    for the workspace; the surviving moments still fold."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict) and "moment" in m and "id" in m:
            out.append(m)
    return out


def _records(path: Path) -> dict[str, dict[str, Any]]:
    """Fold the append-only moments into current escalation state. A decision
    moment whose escalated moment is missing is an orphan and is skipped."""
    out: dict[str, dict[str, Any]] = {}
    for m in _moments(path):
        if m["moment"] == "escalated":
            out[m["id"]] = {k: v for k, v in m.items() if k != "moment"}
            out[m["id"]]["status"] = "pending"
        elif m["id"] not in out:
            continue
        elif m["moment"] == "granted":
            out[m["id"]].update(status="granted", decided_by=m.get("decided_by"),
                                expires=m.get("expires"), decided_at=m.get("at"))
        elif m["moment"] == "denied":
            out[m["id"]].update(status="denied", decided_by=m.get("decided_by"),
                                reason=m.get("reason"), decided_at=m.get("at"))
    return out


def _append(path: Path, moment: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(moment, sort_keys=True) + "\n")


def _owner_role(name: str, kind: str, action: str) -> str:
    """The role a denial escalates to. Organizations: data_owners via the
    action's object type. Packs: the ontology object type's owner when
    present, else the action's approver_roles."""
    if kind == "organization":
        org = orgpolicy.read_org(name)
        obj = orgpolicy.object_for_action(org, action)
        owner = (org.get("data_owners") or {}).get(obj) if obj else None
        if owner:
            return f"role:{owner}"
        chains = {c["action"]: list(c["chain"]) for c in org.get("approver_chains", [])}
        if chains.get(action):
            return f"role:{chains[action][0]}"
        raise HTTPException(422, f"no owning role derivable for {action} in this org")
    pack = load_pack(name)
    adef = (pack.ontology.get("actions", {}) or {}).get(action)
    if not adef:
        raise HTTPException(422, f"{name} has no action {action}")
    types = {**(pack.ontology.get("object_types", {}) or {}),
             **(pack.ontology.get("created_types", {}) or {})}
    obj_type = next((p.get("type") for p in (adef.get("params", {}) or {}).values()
                     if isinstance(p, dict) and p.get("type") in types), None)
    if obj_type is None:
        obj_type = (adef.get("creates", {}) or {}).get("type")
    owner = (types.get(obj_type, {}) or {}).get("owner") if obj_type else None
    if owner:
        return f"role:{owner}"
    roles = adef.get("approver_roles") or []
    if roles:
        return f"role:{roles[0]}"
    raise HTTPException(422, f"no owning role derivable for {action} in {name}")


_PARAM_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def _object_param(name: str, kind: str, action: str) -> str:
    """The action's object-typed parameter name, so the grant's when clause
    matches the env act() actually builds (params.item, not params.object_id).
    Organizations have no ontology, so they keep the generic key."""
    if kind != "pack":
        return "object_id"
    adef = (load_pack(name).ontology.get("actions", {}) or {}).get(action) or {}
    pack = load_pack(name)
    types = {**(pack.ontology.get("object_types", {}) or {}),
             **(pack.ontology.get("created_types", {}) or {})}
    for pname, p in (adef.get("params", {}) or {}).items():
        t = p.get("type") if isinstance(p, dict) else p
        if t in types and _PARAM_NAME.match(pname):
            return pname
    return "object_id"


def _audit(name: str, actor: str, action: str, status: str,
           params: dict[str, Any], policy: str | None,
           approved_by: str | None = None) -> str:
    """Write the moment to the hash-chained audit log when a store exists.
    Returns how the moment was recorded."""
    db = PackPaths(name).ontology_db
    if not db.exists():
        return "jsonl"
    from .ontology.store import OntologyStore

    store = OntologyStore(db)
    try:
        store.audit(actor=actor, on_behalf_of=None, action=f"escalate:{action}",
                    status=status, params=params, objects=[], before=None,
                    after=None, evidence=[], policy=policy,
                    approved_by=approved_by, idempotency_key=None, action_run=None)
    finally:
        store.con.close()
    return "jsonl+audit"


router = APIRouter()


@router.post("/workspaces/{name}/escalations", status_code=201)
def escalate(name: str, body: EscalateBody) -> dict[str, Any]:
    kind, path = _workspace(name)
    for value, pattern, what in ((body.principal, _PRINCIPAL, "principal"),
                                 (body.action, _ACTION, "action"),
                                 (body.object_id, _OBJECT, "object_id"),
                                 (body.denying_rule, _RULE, "denying_rule")):
        if not pattern.match(value):
            raise HTTPException(422, f"{what} has an unexpected shape")
    owner = _owner_role(name, kind, body.action)
    esc_id = f"esc-{sum(1 for m in _moments(path) if m['moment'] == 'escalated') + 1:04d}"
    at = datetime.now(timezone.utc).isoformat()
    record = {"id": esc_id, "workspace": name, "kind": kind,
              "principal": body.principal, "action": body.action,
              "object_id": body.object_id, "denying_rule": body.denying_rule,
              "owner_role": owner, "at": at, "ts": round(time.time(), 6)}
    _append(path, {"moment": "escalated", **record})
    recorded = _audit(name, body.principal, body.action, "escalated",
                      params={"object_id": body.object_id,
                              "denying_rule": body.denying_rule,
                              "escalation": esc_id},
                      policy=body.denying_rule)
    note = ("recorded in the JSONL log and the hash-chained audit log"
            if recorded == "jsonl+audit"
            else "this workspace has no ontology store; the JSONL log is the record")
    return {**record, "status": "pending", "recorded": recorded, "note": note}


@router.get("/workspaces/{name}/escalations")
def escalations(name: str) -> list[dict[str, Any]]:
    _, path = _workspace(name)
    return [r for r in _records(path).values() if r["status"] == "pending"]


@router.post("/workspaces/{name}/escalations/{esc_id}/decide")
def decide(name: str, esc_id: str, body: EscalationDecideBody) -> dict[str, Any]:
    kind, path = _workspace(name)
    rec = _records(path).get(esc_id)
    if rec is None:
        raise HTTPException(404, f"no escalation {esc_id}")
    if rec["status"] != "pending":
        raise HTTPException(409, f"{esc_id} is already {rec['status']}")
    if not _PRINCIPAL.match(body.decided_by):
        raise HTTPException(422, "decided_by has an unexpected shape")
    at = datetime.now(timezone.utc).isoformat()

    if body.decision == "grant":
        if not body.expires or not _valid_date(body.expires):
            raise HTTPException(422, "a grant needs expires as an iso date, like 2026-11-01")
        pname = _object_param(name, kind, rec["action"])
        rule = {"id": esc_id, "effect": "permit",
                "principal": rec["principal"],
                "action": f"action:{rec['action']}",
                "when": (f"params.{pname} == '{rec['object_id']}' "
                         f"and now < '{body.expires}'"),
                "expires": body.expires, "granted_by": body.decided_by,
                "escalation": esc_id}
        # Record before permit: if the audit write fails, the grant must not
        # exist on file with no record of who made it.
        _append(path, {"moment": "granted", "id": esc_id, "at": at,
                       "decided_by": body.decided_by, "expires": body.expires,
                       "rule_id": rule["id"]})
        recorded = _audit(name, body.decided_by, rec["action"], "escalation_granted",
                          params={"object_id": rec["object_id"],
                                  "escalation": esc_id, "expires": body.expires},
                          policy=rule["id"], approved_by=body.decided_by)
        orgpolicy.upsert_rules(name, [rule], actor=body.decided_by,
                               note=f"escalation {esc_id} granted until {body.expires}")
        return {"id": esc_id, "status": "granted", "rule": rule,
                "expires": body.expires, "recorded": recorded,
                "note": f"the grant dies at the start of {body.expires} UTC"}

    if body.decision == "deny":
        _append(path, {"moment": "denied", "id": esc_id, "at": at,
                       "decided_by": body.decided_by, "reason": body.reason})
        recorded = _audit(name, body.decided_by, rec["action"], "escalation_denied",
                          params={"object_id": rec["object_id"],
                                  "escalation": esc_id, "reason": body.reason},
                          policy=rec["denying_rule"], approved_by=body.decided_by)
        return {"id": esc_id, "status": "denied", "reason": body.reason,
                "recorded": recorded}

    raise HTTPException(422, "decision must be grant or deny")
