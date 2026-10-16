"""Sketch 04: the org chart drives policy.

A created organization carries data owners and approver chains as facts.
This module derives policy from those facts, in the same rule schema the
packs' hand-written policies.yaml uses, so the existing PolicyEngine
consumes it unchanged. An org edit becomes a governance change through a
reviewed diff, never a silent one.

What generate() emits, per action the org knows about:
- A permit for the data owner of the action's object type. When the action
  has an approver chain, the permit carries a when clause requiring
  approved_by to be the chain's first role. That is the approval wiring:
  without that approver the engine answers default deny.
- A forbid for an irreversible action that has no approver chain.

How an action maps to an object type, from org data alone: an explicit
org["action_objects"] mapping wins when present. Otherwise the object
type's name tokens must appear in order inside the action name
(delist_item maps to item). Failing that, if the action's chain starts
with a role that owns exactly one object type, that type is used
(propose_facing_change maps to planogram via space_planning_lead).

Applied policy lives in policies.generated.yaml inside the workspace's
data directory, never inside ironwright/packs/ and never touching a
hand-written policies.yaml. Every apply appends one JSON line to
policies.generated.log.jsonl: who, when, and the diff. Escalation grants
(rule ids starting with esc-) share the file and survive regeneration.
A hand-written rule always wins a conflict, because forbid beats permit
in the engine and packs never load generated permits by default.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import PACKS_DIR, data_root
from .packs import list_packs, load_pack

GENERATED_FILE = "policies.generated.yaml"
LOG_FILE = "policies.generated.log.jsonl"
ESCALATION_PREFIX = "esc-"
# Verbs that make an action irreversible in practice. An org may override
# with an explicit irreversible_actions list in org.json.
DESTRUCTIVE_VERBS = ("close", "delete", "delist", "purge", "terminate")

_HEADER = ("# Generated from org.json by ironwright.orgpolicy. Do not hand-edit.\n"
           "# Changes go through the preview and apply flow; the log sits beside this file.\n")


# --------------------------------------------------------- org derivation

def _subseq(needle: list[str], hay: list[str]) -> bool:
    """True when needle appears as a contiguous run inside hay."""
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def object_for_action(org: dict[str, Any], action: str) -> str | None:
    """The object type an action touches, derived from org data alone."""
    explicit = org.get("action_objects") or {}
    if action in explicit:
        return explicit[action]
    owners = org.get("data_owners") or {}
    tokens = action.split("_")
    matches = [t for t in owners if _subseq(t.split("_"), tokens)]
    if matches:
        matches.sort(key=lambda t: (-len(t.split("_")), t))
        return matches[0]
    chains = {c["action"]: list(c["chain"]) for c in org.get("approver_chains", [])}
    chain = chains.get(action)
    if chain:
        owned = sorted(t for t, r in owners.items() if r == chain[0])
        if len(owned) == 1:
            return owned[0]
    return None


def action_catalog(org: dict[str, Any]) -> list[str]:
    """Every action the org references: chains, role approves lists, and
    any explicit action_objects or irreversible_actions keys."""
    actions: set[str] = {c["action"] for c in org.get("approver_chains", [])}
    for role in org.get("roles", []):
        actions.update(role.get("approves", []))
    actions.update(org.get("action_objects") or {})
    actions.update(org.get("irreversible_actions") or [])
    return sorted(actions)


def irreversible_actions(org: dict[str, Any]) -> set[str]:
    explicit = org.get("irreversible_actions")
    if explicit is not None:
        return set(explicit)
    return {a for a in action_catalog(org) if a.split("_")[0] in DESTRUCTIVE_VERBS}


def generate(org: dict[str, Any]) -> dict[str, Any]:
    """Derive the policy set from the org. Deterministic: the same org
    always gives byte-identical output once dumped with sorted keys."""
    owners = org.get("data_owners") or {}
    chains = {c["action"]: list(c["chain"]) for c in org.get("approver_chains", [])}
    irreversible = irreversible_actions(org)
    rules: list[dict[str, Any]] = []
    for action in action_catalog(org):
        chain = chains.get(action)
        obj = object_for_action(org, action)
        if obj and owners.get(obj):
            rule = {"id": f"own-{obj}-{action}", "effect": "permit",
                    "principal": f"role:{owners[obj]}", "action": f"action:{action}"}
            if chain:
                rule["when"] = f"approved_by == 'role:{chain[0]}'"
            rules.append(rule)
        if not chain and action in irreversible:
            rules.append({"id": f"forbid-{action}", "effect": "forbid",
                          "principal": "*", "action": f"action:{action}"})
    rules.sort(key=lambda r: r["id"])
    return {"default": "deny", "rules": rules}


# ------------------------------------------------------------------- diff

def diff(old_rules: list[dict[str, Any]], new_rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare two rule sets by rule id. A rule whose body changed shows up
    as the new version in added and the old version in removed."""
    old = {r["id"]: r for r in old_rules}
    new = {r["id"]: r for r in new_rules}
    return {
        "added": [new[i] for i in sorted(new) if old.get(i) != new[i]],
        "removed": [old[i] for i in sorted(old) if new.get(i) != old[i]],
        "unchanged": [new[i] for i in sorted(new) if old.get(i) == new[i]],
    }


# ------------------------------------------------------- files on disk

def _org_dir(name: str) -> Path:
    d = data_root() / name
    if name in list_packs() or not (d / "org.json").exists():
        raise FileNotFoundError(name)
    return d


def read_org(name: str) -> dict[str, Any]:
    return json.loads((_org_dir(name) / "org.json").read_text())


def _target_dir(name: str) -> Path:
    """Where generated policy lives: always the workspace's data directory.
    For a pack that is data_root()/<pack>, beside its run artifacts. The
    pack source tree under ironwright/packs/ is never a write target."""
    if name in list_packs():
        d = (data_root() / name).resolve()
    else:
        d = _org_dir(name).resolve()
    packs = PACKS_DIR.resolve()
    if d == packs or packs in d.parents:
        raise RuntimeError("refusing to write generated policy into ironwright/packs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def applied_rules(name: str) -> list[dict[str, Any]]:
    """Every rule currently applied, escalation grants included."""
    path = _target_dir(name) / GENERATED_FILE
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text()) or {}).get("rules", [])


def generated_rules(name: str) -> list[dict[str, Any]]:
    """The applied rules that came from generation, grants excluded."""
    return [r for r in applied_rules(name) if not r["id"].startswith(ESCALATION_PREFIX)]


def active_rules(name: str, now: str | None = None) -> list[dict[str, Any]]:
    """Applied rules with expired grants dropped. The when clause already
    fails closed inside the engine; this filter keeps dead rules out of
    any engine the platform builds from the file."""
    now = now or datetime.now(timezone.utc).isoformat()
    return [r for r in applied_rules(name)
            if not r.get("expires") or now < str(r["expires"])]


def combined_rules(name: str, now: str | None = None) -> list[dict[str, Any]]:
    """Hand-written rules first, then the active generated ones. Forbid
    beats permit in the engine, so a hand-written forbid always wins."""
    hand = load_pack(name).policies.get("rules", []) if name in list_packs() else []
    return hand + active_rules(name, now=now)


def _write(name: str, all_rules: list[dict[str, Any]], actor: str,
           change: dict[str, Any], note: str) -> None:
    d = _target_dir(name)
    body = yaml.safe_dump({"default": "deny", "rules": all_rules}, sort_keys=True)
    (d / GENERATED_FILE).write_text(_HEADER + body)
    line = {"ts": round(time.time(), 6),
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": actor, "note": note,
            "added": [r["id"] for r in change["added"]],
            "removed": [r["id"] for r in change["removed"]],
            "unchanged": len(change["unchanged"])}
    with (d / LOG_FILE).open("a") as f:
        f.write(json.dumps(line, sort_keys=True) + "\n")


def apply(workspace_name: str, rules: list[dict[str, Any]],
          approved_by: str = "user:console", note: str = "regenerated from org.json") -> dict[str, Any]:
    """Apply a regenerated rule set. Escalation grants already on file are
    kept; the diff reported covers only the generated rules."""
    old_all = applied_rules(workspace_name)
    kept = [r for r in old_all if r["id"].startswith(ESCALATION_PREFIX)]
    old_gen = [r for r in old_all if not r["id"].startswith(ESCALATION_PREFIX)]
    change = diff(old_gen, rules)
    merged = sorted(rules + kept, key=lambda r: r["id"])
    _write(workspace_name, merged, approved_by, change, note)
    return change


def upsert_rules(workspace_name: str, new_rules: list[dict[str, Any]],
                 actor: str, note: str = "") -> dict[str, Any]:
    """Merge rules into the applied set by id. The escalation grant path."""
    old = applied_rules(workspace_name)
    merged = {r["id"]: r for r in old}
    for r in new_rules:
        merged[r["id"]] = r
    all_rules = [merged[i] for i in sorted(merged)]
    change = diff(old, all_rules)
    _write(workspace_name, all_rules, actor, change, note)
    return change


# ----------------------------------------------------------------- router

class ApplyBody(BaseModel):
    approved_by: str


router = APIRouter()


def _org_or_404(name: str) -> dict[str, Any]:
    if name in list_packs():
        raise HTTPException(404, f"{name} is a pack; packs keep their hand-written policies.yaml")
    try:
        return read_org(name)
    except FileNotFoundError:
        raise HTTPException(404, f"no organization named {name}")


@router.get("/workspaces/{name}/orgpolicy/preview")
def orgpolicy_preview(name: str) -> dict[str, Any]:
    org = _org_or_404(name)
    new = generate(org)["rules"]
    old = generated_rules(name)
    return {"workspace": name, "rules": new, "diff": diff(old, new),
            "applied": len(old)}


@router.post("/workspaces/{name}/orgpolicy/apply")
def orgpolicy_apply(name: str, body: ApplyBody) -> dict[str, Any]:
    org = _org_or_404(name)
    change = apply(name, generate(org)["rules"], approved_by=body.approved_by)
    return {"workspace": name, "applied": True, "diff": change,
            "file": GENERATED_FILE, "log": LOG_FILE}
