"""Cedar-style authorization: default deny, forbid always beats permit.

Rules live in each pack's policies.yaml:
  - id: agents-may-read
    effect: permit
    principal: "agent:*"
    action: "function:*"
  - id: never-close-alerts
    effect: forbid
    principal: "agent:*"
    action: "action:close_alert"
  - id: small-orders-only
    effect: permit
    principal: "agent:retail-triage"
    action: "action:create_replenishment_order"
    when: "params.qty <= 200"

The check runs when a tool is called, outside the model, so a prompt injection
cannot talk its way past it. AWS mapping: AgentCore Policy (Cedar).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

from .expr import evaluate


@dataclass
class Decision:
    allowed: bool
    reason: str
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "matched": self.matched}


class PolicyEngine:
    def __init__(self, spec: dict[str, Any]):
        self.rules = spec.get("rules", [])

    def authorize(self, principal: str, action: str, env: dict[str, Any] | None = None) -> Decision:
        env = env or {}
        permits, forbids = [], []
        for r in self.rules:
            if not (fnmatch(principal, r.get("principal", "*")) and fnmatch(action, r.get("action", "*"))):
                continue
            if r.get("when"):
                # A when clause that cannot evaluate skips the rule, for
                # permits and forbids alike: dry-runs pass partial envs, and
                # erroring a forbid into a match would deny every dry-run.
                # Consequence: a conditional forbid must be written so its
                # clause evaluates in any env act() builds.
                try:
                    if not evaluate(r["when"], env):
                        continue
                except Exception:
                    continue
            (forbids if r["effect"] == "forbid" else permits).append(r.get("id", r["action"]))
        if forbids:
            return Decision(False, f"forbidden by {forbids[0]}", forbids)
        if permits:
            return Decision(True, f"permitted by {permits[0]}", permits)
        return Decision(False, "default deny: no rule permits this", [])
