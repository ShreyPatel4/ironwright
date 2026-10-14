"""Ontology runtime: the only door apps and agents use.

Functions read (governed queries over objects + lake). Actions write, and every
action goes through the same path:

  validate params -> load objects -> preconditions -> policy -> approval gate
  -> idempotency -> edits (versioned) -> outbox (legacy write-back) -> audit

Write-back to legacy is never a dual write: the edit and the outbox row commit
together, a worker delivers with retries, and a compensating action rolls the
object back if the legacy system finally rejects it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

from ..expr import evaluate
from ..lake import Lakehouse
from ..packs import Pack
from ..policy import PolicyEngine
from ..tracing import Tracer
from .store import OntologyStore


class ActionError(Exception):
    pass


def _plain(v: Any) -> Any:
    """Make query values JSON friendly (dates, decimals, numpy scalars)."""
    from decimal import Decimal
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            return v.item()
        except Exception:
            return v
    return v


class Ontology:
    def __init__(self, pack: Pack, tracer: Tracer | None = None, attach_lake: bool = True,
                 db_path=None, read_only: bool = False):
        self.pack = pack
        self.spec = pack.ontology
        self.tracer = tracer or Tracer(None)
        self.store = OntologyStore(db_path or pack.paths.ontology_db, read_only=read_only)
        self.policy = PolicyEngine(pack.policies)
        if attach_lake:
            lake = Lakehouse(pack)
            for ident in lake.tables():
                ns, name = ident.split(".")
                data = lake.read(ident)
                self.store.con.register(f"_{ns}_{name}", data)
                if ns == "bronze" and "_run_id" in data.column_names:
                    self.store.con.execute(f"create or replace temp view {ns}_{name} as select * from _{ns}_{name} "
                                           f"where _run_id = (select max(_run_id) from _{ns}_{name})")
                else:
                    self.store.con.execute(f"create or replace temp view {ns}_{name} as select * from _{ns}_{name}")

    # ---------------- helpers ----------------
    def keys(self, type_: str, id_: str) -> dict[str, str]:
        rows = self.store.con.execute("select system, source_key from crosswalk where type = ? and id = ?",
                                      [type_, id_]).fetchall()
        return {s: k for s, k in rows}

    def describe(self) -> dict[str, Any]:
        return {
            "object_types": {k: {"description": v.get("description", ""), "owner": v.get("owner"),
                                 "sources": [s["system"] for s in v["sources"]]}
                             for k, v in self.spec["object_types"].items()},
            "link_types": {k: {"from": v["from"], "to": v["to"], "description": v.get("description", "")}
                           for k, v in self.spec.get("link_types", {}).items()},
            "functions": {k: {"description": v.get("description", ""), "params": v.get("params", {})}
                          for k, v in self.spec.get("functions", {}).items()},
            "actions": {k: {"description": v.get("description", ""), "params": v.get("params", {}),
                            "requires_approval": v.get("requires_approval", False),
                            "side_effect": (v.get("side_effect") or {}).get("system")}
                        for k, v in self.spec.get("actions", {}).items()},
        }

    def _check_param(self, name: str, ptype: Any, value: Any) -> Any:
        t = ptype["type"] if isinstance(ptype, dict) else ptype
        if t in self.spec["object_types"]:
            obj = self.store.get(t, str(value))
            if obj is None:
                raise ActionError(f"{name}: no {t} with id {value}")
            return str(value)
        if t == "int":
            v = int(value)
        elif t == "float":
            v = float(value)
        elif t == "bool":
            v = bool(value)
        else:
            v = str(value)
        if isinstance(ptype, dict):
            if "min" in ptype and v < ptype["min"]:
                raise ActionError(f"{name}={v} below minimum {ptype['min']}")
            if "max" in ptype and v > ptype["max"]:
                raise ActionError(f"{name}={v} above maximum {ptype['max']}")
            if "values" in ptype and v not in ptype["values"]:
                raise ActionError(f"{name}={v} not one of {ptype['values']}")
        return v

    def _env(self, params_spec: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        env: dict[str, Any] = {"params": dict(params)}
        for pname, ptype in params_spec.items():
            t = ptype["type"] if isinstance(ptype, dict) else ptype
            if t in self.spec["object_types"] and pname in params:
                base = pname[:-3] if pname.endswith("_id") else pname
                env[base] = self.store.get(t, params[pname]) or {}
                env[f"{base}_keys"] = self.keys(t, params[pname])
        return env

    def masked(self, obj: dict[str, Any] | None, principal: str) -> dict[str, Any] | None:
        """Property-level masking: fields listed as sensitive are hidden unless policy allows
        read_sensitive on that object type for this principal."""
        if not obj:
            return obj
        sens = self.spec["object_types"].get(obj.get("_type"), {}).get("sensitive", [])
        if not sens:
            return obj
        if self.policy.authorize(principal, f"read_sensitive:{obj['_type']}", {"principal": principal}).allowed:
            return obj
        return {k: ("***" if k in sens and v is not None else v) for k, v in obj.items()}

    # ---------------- functions ----------------
    def call(self, fn: str, args: dict[str, Any], principal: str = "user:anonymous") -> dict[str, Any]:
        fdef = self.spec.get("functions", {}).get(fn)
        if not fdef:
            raise ActionError(f"unknown function {fn}")
        with self.tracer.span("ontology.function", function=fn, principal=principal) as sp:
            decision = self.policy.authorize(principal, f"function:{fn}", {"params": args, "principal": principal})
            sp.set(policy="ALLOW" if decision.allowed else "DENY")
            if not decision.allowed:
                sp.event("policy.denied", rule=decision.reason)
                raise PermissionError(decision.reason)
            clean = {p: self._check_param(p, t, args[p]) if p in args else
                     (t.get("default") if isinstance(t, dict) else None) for p, t in fdef.get("params", {}).items()}
            with self.tracer.span("warehouse.query", engine="duckdb") as q:
                cur = self.store.con.execute(fdef["sql"], {k: v for k, v in clean.items() if f"${k}" in fdef["sql"]})
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                q.set(rows=len(rows))
            rows = [{k: _plain(v) for k, v in r.items()} for r in rows]
            eid = f"{fn}#{hashlib.sha1(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()[:8]}"
            sp.set(evidence_id=eid, rows=len(rows))
            summary = rows[0] if (fdef.get("returns") == "one" and rows) else ({} if fdef.get("returns") == "one" else None)
            return {"id": eid, "function": fn, "args": clean, "rows": rows, "summary": summary}

    # ---------------- actions ----------------
    def act(self, action: str, params: dict[str, Any], actor: str, on_behalf_of: str | None = None,
            idempotency_key: str | None = None, approved_by: str | None = None,
            evidence: list[str] | None = None) -> dict[str, Any]:
        adef = self.spec.get("actions", {}).get(action)
        if not adef:
            raise ActionError(f"unknown action {action}")
        run_id = f"ar-{uuid.uuid4().hex[:10]}"
        idem = idempotency_key or f"{action}:{json.dumps(params, sort_keys=True, default=str)}"
        with self.tracer.span("ontology.action", action=action, actor=actor, idempotency_key=idem) as sp:
            prior = self.store.con.execute("select result from idempotency where key = ?", [idem]).fetchone()
            if prior:
                sp.event("idempotent_replay", key=idem)
                return {**json.loads(prior[0]), "replayed": True}

            def finish(status: str, reason: str, **extra: Any) -> dict[str, Any]:
                rec = self.store.audit(actor=actor, on_behalf_of=on_behalf_of, action=action, status=status,
                                       params=params, evidence=evidence or [], approved_by=approved_by,
                                       idempotency_key=idem, action_run=run_id,
                                       policy=extra.pop("policy", None), objects=extra.pop("objects", None),
                                       before=extra.pop("before", None), after=extra.pop("after", None))
                result = {"action_run": run_id, "action": action, "status": status, "reason": reason,
                          "audit_seq": rec["seq"], **extra}
                if status in ("applied", "applied_pending_sync"):
                    self.store.con.execute("insert or replace into idempotency values (?, ?, ?, ?)",
                                           [idem, run_id, json.dumps(result, default=str), time.time()])
                sp.set(status=status)
                return result

            # 1. authorize first, so unauthorized callers learn nothing about parameters or object state
            try:
                env = self._env(adef.get("params", {}), {k: str(v) if isinstance(v, str) else v for k, v in params.items()})
            except Exception:
                env = {"params": dict(params)}
            decision = self.policy.authorize(actor, f"action:{action}", {**env, "principal": actor})
            if not decision.allowed:
                sp.event("policy.denied", rule=decision.reason)
                return finish("denied", decision.reason, policy=decision.to_dict())
            sp.event("policy.allowed", rule=decision.reason)

            # 2. validate parameters
            try:
                clean = {p: self._check_param(p, t, params[p]) for p, t in adef.get("params", {}).items() if p in params}
                missing = [p for p, t in adef.get("params", {}).items()
                           if p not in params and not (isinstance(t, dict) and t.get("optional"))]
                if missing:
                    raise ActionError(f"missing params {missing}")
            except (ActionError, ValueError) as e:
                sp.event("invalid_params", error=str(e))
                return finish("rejected", f"invalid parameters: {e}", policy=decision.to_dict())
            env = self._env(adef.get("params", {}), clean)

            # 3. preconditions on current object state (checked again when an approved action executes)
            for pc in adef.get("preconditions", []):
                ok = False
                try:
                    ok = bool(evaluate(pc["when"], env))
                except Exception:
                    ok = False
                if not ok:
                    sp.event("precondition_failed", check=pc["when"])
                    return finish("rejected", f"precondition failed: {pc.get('message', pc['when'])}",
                                  policy=decision.to_dict())

            if adef.get("requires_approval") and not approved_by:
                sp.event("approval_required", roles=adef.get("approver_roles", []))
                return finish("needs_approval", "this action needs human approval",
                              policy=decision.to_dict(), approver_roles=adef.get("approver_roles", []))

            # edits, versioned
            before, after, touched = {}, {}, []
            for ed in adef.get("edits", []):
                pname = ed["object"]
                t = adef["params"][pname]["type"] if isinstance(adef["params"][pname], dict) else adef["params"][pname]
                oid = clean[pname]
                obj = self.store.get(t, oid) or {}
                before[oid] = {k: v for k, v in obj.items() if not k.startswith("_") or k == "_pending_sync"}
                new = dict(before[oid])
                for prop, expr in ed["set"].items():
                    new[prop] = evaluate(str(expr), env)
                if adef.get("side_effect"):
                    new["_pending_sync"] = True
                if "_prov" in obj:
                    new["_prov"] = obj["_prov"]
                self.store.upsert(t, oid, new, obj.get("_title", oid), changed_by=actor, action_run=run_id)
                after[oid] = new
                touched.append({"type": t, "id": oid})
            created = None
            if adef.get("creates"):
                c = adef["creates"]
                cid = f"{c.get('prefix', c['type'][:3].upper())}-{uuid.uuid4().hex[:8]}"
                props = {k: evaluate(str(v), env) for k, v in c["props"].items()}
                self.store.upsert(c["type"], cid, props, str(props.get(c.get("title", "name"), cid)),
                                  changed_by=actor, action_run=run_id)
                for lk in c.get("links", []):
                    self.store.con.execute("insert into links values (?, ?, ?, ?, ?, ?)",
                                           [lk["link"], c["type"], cid, lk["to_type"], clean[lk["param"]], "{}"])
                created = {"type": c["type"], "id": cid}
                touched.append(created)
                sp.event("object_created", type=c["type"], id=cid)

            status = "applied"
            if adef.get("side_effect"):
                se = adef["side_effect"]
                payload = {k: evaluate(str(v), env) for k, v in se.get("payload", {}).items()}
                ob_id = f"ob-{uuid.uuid4().hex[:10]}"
                self.store.con.execute("insert into outbox values (?, ?, ?, ?, 'pending', 0, ?, ?, null)",
                                       [ob_id, run_id, se["system"], json.dumps(payload, default=str),
                                        time.time(), time.time()])
                carrier = Tracer.current()
                sp.event("outbox.enqueue", outbox_id=ob_id, system=se["system"],
                         traceparent=f"00-{carrier.trace_id}-{carrier.span_id}-01" if carrier else None)
                status = "applied_pending_sync"
            return finish(status, "applied", policy=decision.to_dict(), objects=touched, before=before,
                          after=after, created=created)

    # ---------------- outbox worker ----------------
    def deliver_outbox(self, max_attempts: int = 3) -> dict[str, int]:
        """Deliver pending write-backs to the legacy systems' intake queues.
        Failures are simulated from pack settings so the compensation path is exercised."""
        wb = self.pack.spec.get("writeback", {})
        down = set(wb.get("down_systems", []))
        rate = float(wb.get("transient_failure_rate", 0.0))
        db = sqlite3.connect(self.pack.paths.legacy / "writeback.db")
        db.execute("create table if not exists intake (system text, outbox_id text, payload text, received_at real)")
        counts = {"delivered": 0, "failed_compensated": 0}
        pending = self.store.con.execute(
            "select id, action_run, system, payload, attempts from outbox where status = 'pending'").fetchall()
        for ob_id, run, system, payload, attempts in pending:
            with self.tracer.span("outbox.worker", outbox_id=ob_id, system=system) as sp:
                sp.link(run, "continues action run")
                ok = False
                for attempt in range(attempts + 1, max_attempts + 1):
                    h = int(hashlib.sha1(f"{ob_id}:{attempt}".encode()).hexdigest(), 16) % 1000 / 1000
                    rejected = system in down
                    if wb.get("reject_when"):
                        try:
                            rejected = rejected or bool(evaluate(wb["reject_when"],
                                                                 {"system": system, "payload": json.loads(payload)}))
                        except Exception:
                            pass
                    if rejected:
                        sp.event("retry", attempt=attempt, reason="legacy system rejected the write")
                        continue
                    if h < rate:
                        sp.event("retry", attempt=attempt, reason="timeout")
                        continue
                    db.execute("insert into intake values (?, ?, ?, ?)", [system, ob_id, payload, time.time()])
                    db.commit()
                    ok = True
                    break
                self.store.con.execute("update outbox set status = ?, attempts = ?, updated = ? where id = ?",
                                       ["delivered" if ok else "failed", max_attempts, time.time(), ob_id])
                audit_row = self.store.con.execute(
                    "select actor, action, objects, before from audit_log where action_run = ? order by seq desc limit 1",
                    [run]).fetchone()
                objs = json.loads(audit_row[2]) if audit_row and audit_row[2] else []
                before = json.loads(audit_row[3]) if audit_row and audit_row[3] else {}
                if ok:
                    for o in objs:
                        cur = self.store.get(o["type"], o["id"])
                        if cur and cur.get("_pending_sync"):
                            props = {k: v for k, v in cur.items() if not k.startswith("_") or k == "_prov"}
                            self.store.upsert(o["type"], o["id"], props, cur["_title"], "outbox.worker", run)
                    self.store.audit(actor="outbox.worker", action=f"deliver:{system}", status="delivered",
                                     action_run=run, objects=objs)
                    counts["delivered"] += 1
                else:
                    # compensate: restore every edited object to its before-image
                    for oid, props in before.items():
                        t = self.store.find_type(oid)
                        if t:
                            restored = {k: v for k, v in props.items() if k != "_pending_sync"}
                            cur = self.store.get(t, oid) or {}
                            if "_prov" in cur:
                                restored["_prov"] = cur["_prov"]
                            self.store.upsert(t, oid, restored, cur.get("_title", oid), "compensation", run)
                    sp.event("compensated", objects=len(before))
                    sp.status = "error"
                    self.store.audit(actor="outbox.worker", action=f"compensate:{system}", status="compensated",
                                     action_run=run, objects=objs, after=before)
                    counts["failed_compensated"] += 1
        db.close()
        return counts
