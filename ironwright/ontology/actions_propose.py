"""A human authors a governed action on a tenant workspace. Nothing else makes one.

The templates are data (action_templates.yaml). A draft is a template, one accepted
object type, who may ask, and whether a second human approves. Accept stores what
the human picked, a permit in policies.yaml, and an audit row. The full action is
built from the template again on every load, so the file on disk only ever holds
choices, and each choice is checked again before it is used.

```mermaid
flowchart LR
  T[action_templates.yaml] --> D[draft]
  H[human picks type, role, approval] --> D
  D --> A{owner or admin accepts}
  A --> F[actions.yaml + policies.yaml + audit row]
  F --> S[servable pack: act, approvals, Ask offers]
```

Why actions.yaml and not ontology.yaml: T2's validator lets a tenant's ontology.yaml
hold objects and links only, and an ontology accept rewrites that file whole. A
generated read lives in code for the same reason (see propose.py).

Rules decide, the LLM narrates. Ask may offer an action only when a rule matched:
the question names one of the template's keywords, the answer cited a read of that
object, and the live policy permits the caller. The offer is a button. It never fires.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any

import duckdb
import yaml

from .. import workspaces_tenant as wt
from ..packs import Pack
from ..policy import PolicyEngine
from .propose import StoreBusy, _replace_file, _snake, askable
from .store import OntologyStore

TEMPLATES_FILE = Path(__file__).with_name("action_templates.yaml")
ACTIONS_FILE = "actions.yaml"
RULE_PREFIX = "tenant-action-"
ROLES_FROM = {"member": ["member", "admin", "owner"], "admin": ["admin", "owner"], "owner": ["owner"]}
APPROVER_ROLES = ["owner", "admin"]
RESERVED = frozenset({"propose", "accept"})
MAX_VALUES = 20

_NAME = re.compile(r"[a-z][a-z0-9_]{2,62}")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}")
_WORD = re.compile(r"[a-z]+")
_CHOICES = {"template", "object_type", "request_role", "requires_approval", "values", "allowed_statuses", "name"}


class DraftError(ValueError):
    """A choice that cannot make an action. The message is safe to show."""


class StaleDraft(Exception):
    """The draft id is not what these choices make."""


@cache
def templates() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(TEMPLATES_FILE.read_text())


def catalog() -> list[dict[str, Any]]:
    return [{"name": n, "description": t["description"].replace("{thing}", "object"),
             "params": {p: {k: v for k, v in s.items() if k != "values"} for p, s in t["params"].items()},
             "picks_values": any(s.get("values") == "picked" for s in t["params"].values()),
             "needs": t.get("needs")}
            for n, t in sorted(templates().items())]


# ---------------------------------------------------------------- the draft

def _values(raw: Any, where: str) -> list[str]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_VALUES:
        raise DraftError(f"{where}: give 1 to {MAX_VALUES} values")
    for v in raw:
        if not isinstance(v, str) or not _VALUE.fullmatch(v):
            raise DraftError(f"{where}: a value is letters, digits, spaces, dots, dashes or underscores, at most 40")
    return list(dict.fromkeys(raw))


def _properties(ontology: dict[str, Any], type_name: str) -> set[str]:
    tdef = ontology["object_types"][type_name]
    return {f for s in tdef.get("sources", []) for f in s.get("fields", {})}


def _choices(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DraftError("a draft is an object of choices")
    unknown = sorted(set(raw) - _CHOICES)
    if unknown:
        raise DraftError(f"unknown choice: {', '.join(unknown)}")
    return {k: v for k, v in raw.items() if v is not None}


def draft(ontology: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Pure over the accepted ontology and the human's choices. Same in, same id out."""
    c = _choices(raw)
    tname = c.get("template")
    if not isinstance(tname, str) or tname not in templates():
        raise DraftError(f"template: one of {', '.join(sorted(templates()))}")
    tpl = templates()[tname]
    otype = c.get("object_type")
    if not isinstance(otype, str) or otype not in (ontology.get("object_types") or {}):
        raise DraftError("object_type: one of the accepted object types")
    role = c.get("request_role", "member")
    if role not in ROLES_FROM:
        raise DraftError(f"request_role: one of {', '.join(ROLES_FROM)}. A viewer only reads.")
    approval = c.get("requires_approval", True)
    if not isinstance(approval, bool):
        raise DraftError("requires_approval: true or false")
    snake = _snake(otype)
    name = c.get("name", f"{tname}_{snake}")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in RESERVED:
        raise DraftError("name: lowercase letters, digits and underscores, and not propose or accept")
    props = _properties(ontology, otype)
    needs = tpl.get("needs")
    if needs and needs not in props:
        raise DraftError(f"{tname}: {otype} has no {needs} property")

    obj = f"{snake}_id"
    params: dict[str, Any] = {obj: otype}
    picked = None
    for pname, spec in tpl["params"].items():
        spec = dict(spec)
        if spec.get("values") == "picked":
            spec["values"] = picked = _values(c.get("values"), "values")
        params[pname] = spec
    preconditions = [{"when": f"len({snake}) > 0", "message": f"the {otype} exists"}]
    allowed = c.get("allowed_statuses")
    if allowed is not None:
        if "status" not in props:
            raise DraftError(f"allowed_statuses: {otype} has no status property")
        allowed = _values(allowed, "allowed_statuses")
        listed = ", ".join(repr(v) for v in allowed)  # each value passed _VALUE: no quote can reach the expression
        preconditions.append({"when": f"{snake}.status in [{listed}]",
                              "message": f"the {otype} status is one of {', '.join(allowed)}"})
    roles = ROLES_FROM[role]
    rule = {"id": f"{RULE_PREFIX}{name}", "effect": "permit", "principal": "user:*", "action": f"action:{name}",
            "when": f"role in [{', '.join(repr(r) for r in roles)}]"}
    thing = snake.replace("_", " ")
    out = {"name": name, "template": tname, "object_type": otype,
           "description": tpl["description"].replace("{thing}", thing),
           "params": params, "preconditions": preconditions,
           "edits": [{"object": obj, "set": dict(tpl["set"])}],
           "requires_approval": approval, "approver_roles": list(APPROVER_ROLES) if approval else [],
           "request_roles": roles, "policy_rule": rule,
           "choices": {"template": tname, "object_type": otype, "request_role": role, "requires_approval": approval,
                       "name": name, **({"values": picked} if picked is not None else {}),
                       **({"allowed_statuses": allowed} if allowed is not None else {})}}
    out["id"] = "ad-" + hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()[:16]
    out["plain"] = _plain(out, thing)
    return out


def _who(roles: list[str]) -> str:
    words = [f"{r}s" for r in roles]
    return words[0].capitalize() if len(words) == 1 else ", ".join(words[:-1]).capitalize() + f" and {words[-1]}"


def _plain(d: dict[str, Any], thing: str) -> str:
    what = d["description"][0].lower() + d["description"][1:].rstrip(".")
    text = f"{_who(d['request_roles'])} in this org may ask to {what}."
    if d["requires_approval"]:
        text += (" Nothing changes until an owner or admin who did not ask approves it."
                 " An agent can never approve.")
    else:
        text += " It applies at once, with no second person."
    if len(d["preconditions"]) > 1:
        text += f" It only runs when {d['preconditions'][1]['message']}."
    text += " Everyone else is refused, and every request lands in the audit log."
    return text


def action_def(d: dict[str, Any]) -> dict[str, Any]:
    """What act() runs. Built from the template every time, never read from disk."""
    return {"description": d["description"], "params": d["params"], "preconditions": d["preconditions"],
            "edits": d["edits"], "requires_approval": d["requires_approval"], "approver_roles": d["approver_roles"],
            "template": d["template"], "object_type": d["object_type"]}


# ---------------------------------------------------------------- what a workspace holds

def _read_choices(pack: Pack) -> list[Any]:
    try:
        raw = yaml.safe_load(wt._read_small(pack.dir / ACTIONS_FILE, ACTIONS_FILE))
    except FileNotFoundError:
        return []
    found = (raw or {}).get("actions") if isinstance(raw, dict) else None
    return found if isinstance(found, list) else []


def accepted(pack: Pack) -> list[dict[str, Any]]:
    """Each stored choice made into a draft again. One that no longer fits the
    ontology (its type was dropped, say) is left out, so it cannot be called."""
    out = []
    for raw in _read_choices(pack):
        try:
            out.append(draft(pack.ontology, raw))
        except (DraftError, TypeError, KeyError):
            continue
    return out


def servable(pack: Pack) -> Pack:
    """The pack a serving route runs on: generated reads plus accepted actions."""
    pack = askable(pack)
    if not pack.ontology.get("object_types"):
        return pack
    actions = {d["name"]: action_def(d) for d in accepted(pack)}
    return replace(pack, ontology={**pack.ontology, "actions": actions})


def accept(org_id: str, slug: str, raw: Any, draft_id: str, accepted_by: str) -> dict[str, Any]:
    """Check everything, take the store, then write. A refusal leaves the workspace as it was."""
    pack = wt.load_workspace(org_id, slug)
    if not pack.ontology.get("object_types"):
        raise DraftError("accept an ontology first: an action acts on an accepted object type")
    d = draft(pack.ontology, raw)
    if d["id"] != draft_id:
        raise StaleDraft(draft_id)
    kept = [c for c in _read_choices(pack) if not (isinstance(c, dict) and c.get("name") == d["name"])]
    rules = [r for r in pack.policies.get("rules") or [] if r.get("id") != d["policy_rule"]["id"]]
    policies = {"default": "deny", "rules": [*rules, d["policy_rule"]]}
    wt._policies(policies)
    try:
        store = OntologyStore(pack.paths.ensure().ontology_db)
    except duckdb.IOException as e:
        raise StoreBusy() from e
    try:
        _replace_file(pack.dir / ACTIONS_FILE, yaml.safe_dump({"actions": [*kept, d["choices"]]}, sort_keys=False))
        _replace_file(pack.dir / "policies.yaml", yaml.safe_dump(policies, sort_keys=False))
        rec = store.audit(actor=accepted_by, action="action.accept", status="applied",
                          params={"draft_id": d["id"], "action": d["name"], "choices": d["choices"],
                                  "policy_rule": d["policy_rule"]})
    finally:
        store.con.close()
    return {"action": d["name"], "draft_id": d["id"], "audit_seq": rec["seq"]}


# ---------------------------------------------------------------- serving helpers

def object_param(adef: dict[str, Any], types: dict[str, Any]) -> str | None:
    for pname, p in (adef.get("params") or {}).items():
        t = p.get("type") if isinstance(p, dict) else p
        if t in types:
            return pname
    return None


def env_for(principal: str, role: str | None, params: dict[str, Any]) -> dict[str, Any]:
    return {"params": params, "principal": principal, "role": role,
            "now": datetime.now(timezone.utc).isoformat()}


def preview(ont: Any, action: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Before and after for each object the action would edit. Reads only."""
    from ..expr import evaluate

    adef = ont.spec.get("actions", {}).get(action)
    if not adef:
        return []
    try:
        env = ont._env(adef.get("params", {}), params)
        out = []
        for ed in adef.get("edits", []):
            t = adef["params"][ed["object"]]
            t = t["type"] if isinstance(t, dict) else t
            oid = str(params[ed["object"]])
            obj = ont.store.get(t, oid) or {}
            before = {k: v for k, v in obj.items() if not k.startswith("_")}
            after = {**before, **{p: evaluate(str(e), env) for p, e in ed["set"].items()}}
            out.append({"type": t, "id": oid, "before": before, "after": after})
        return out
    except Exception:  # noqa: BLE001  a preview that cannot evaluate shows nothing rather than a guess
        return []


def offers(pack: Pack, engine: PolicyEngine, entry: dict[str, Any], principal: str, role: str | None
           ) -> list[dict[str, Any]]:
    """Buttons for an answered question. Rules pick them; the model's text is never read."""
    if entry.get("status") != "answered":
        return []
    words = set(_WORD.findall(str(entry.get("question", "")).lower()))
    cited = set(entry.get("citations") or [])
    types = pack.ontology.get("object_types") or {}
    reads = {f"get_{_snake(t)}": t for t in types}
    out = []
    for call in entry.get("calls") or []:
        otype = reads.get(call.get("function"))
        oid = (call.get("args") or {}).get("id")
        if otype is None or oid is None or call.get("evidence_id") not in cited:
            continue
        for name, adef in sorted((pack.ontology.get("actions") or {}).items()):
            if adef.get("object_type") != otype or not words & set(templates()[adef["template"]]["keywords"]):
                continue
            pname = object_param(adef, types)
            decision = engine.authorize(principal, f"action:{name}", env_for(principal, role, {pname: str(oid)}))
            if not decision.allowed:
                continue
            out.append({"action": name, "label": adef["description"], "params": {pname: str(oid)},
                        "fields": {p: v for p, v in adef["params"].items() if p != pname},
                        "requires_approval": adef["requires_approval"], "evidence": [call["evidence_id"]],
                        "rule": decision.matched[0]})
    return out
