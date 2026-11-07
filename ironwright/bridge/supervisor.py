"""The supervisor: a LangGraph that keeps one sub-agent's change honest on its way to a layer.

```mermaid
flowchart LR
  D[draft: sub-agent writes the change] --> S[stage: sandbox on a sample]
  S --> E[evaluate: deterministic gates]
  E -->|failed, attempts left| D
  E -->|passed, or 3 failures: stuck| X[escalate: ONE approval card]
  X --> A[approval: interrupt only]
  A --> T{settle: read the card store}
  T -->|no decision yet| A
  T -->|rejected with a reason| D
  T -->|approved| P[promote: act order, supervisor watching]
  P --> O[observe: monitors on]
```

Rules this graph keeps:
- The supervisor writes no pipeline code. The sub-agent drafts; the gate decides.
- The approval node does nothing but interrupt(). LangGraph reruns it from the top on
  resume, so every side effect lives in escalate before it and settle after it.
- The resume payload is never read. settle reads the decision from the card store, and
  promote checks the approver, the requester and the hash again, so a forged resume, a
  forged card line or an agent signature all stop short of a live folder.
- Every node is a span and an audit row (the approval node only a span, since a row
  written there would be a side effect before interrupt()).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypedDict

import pyarrow as pa
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import evaluate as ev
from . import gate
from .gate import BridgeStore, Change, GateError, State

SUPERVISOR = "agent:supervisor"
EVALUATOR = "agent:evaluator"
MONITORS = ["freshness", "drift", "dq"]


class SubAgent(Protocol):
    name: str

    def draft(self, attempt: int, feedback: dict[str, Any] | None) -> Change: ...


class RunState(TypedDict, total=False):
    run_id: str
    requested_by: str
    card_id: str
    refused: str


class Supervisor:
    def __init__(self, store: BridgeStore, agent: SubAgent, bronze: dict[str, pa.Table], *,
                 policy: dict[str, Any], writer: Callable[[str, dict[str, str]], None] | None = None,
                 live_dir: Path | None = None, live_models: Callable[[], dict[str, str]] | None = None,
                 sample_rows: int = gate.SAMPLE_ROWS, compliance_policy: dict[str, Any] | None = None,
                 coverage: Callable[[Change, gate.Staged], float] | None = None,
                 pii: dict[str, str] | None = None, source: dict[str, Any] | None = None,
                 upstream: Callable[[], dict[str, dict[str, str]]] | None = None):
        self.store, self.agent, self.bronze, self.policy = store, agent, bronze, policy
        self.writer, self.live_dir = writer, live_dir
        self.sample_rows, self.compliance_policy, self.coverage = sample_rows, compliance_policy, coverage
        self.pii = pii if pii is not None else ev.pii_tags(bronze)
        self.source = source or {}
        self._live_models = live_models
        self._upstream = upstream
        self._staged: dict[str, gate.Staged] = {}

    # -------------------------------------------------------- helpers

    def live_models(self, layer: str) -> dict[str, str]:
        if self._live_models is not None:
            return self._live_models()
        return self._live_folder(layer)

    def upstream(self, layer: str) -> dict[str, dict[str, str]]:
        """The live models of the layers below this one, which a staged change reads."""
        if self._upstream is not None:
            return self._upstream()
        below = gate.LAYERS[: gate.LAYERS.index(layer)] if layer in gate.LAYERS else ()
        return {low: self._live_folder(low) for low in below}

    def _live_folder(self, layer: str) -> dict[str, str]:
        folder = (self.live_dir / layer) if self.live_dir else None
        if not folder or not folder.is_dir():
            return {}
        return {p.name: p.read_text() for p in sorted(folder.glob("*.sql"))}

    def _baseline(self, run: dict[str, Any]) -> float:
        """Coverage never drops: the floor is the last promoted change to the same tables."""
        best = 0.0
        for other in self.store.runs():
            if other["run_id"] == run["run_id"] or other["state"] not in (State.PROMOTED, State.OBSERVED):
                continue
            if set(other["tables"]) & set(run["tables"]) and other.get("evaluation"):
                best = max(best, float(other["evaluation"]["gates"]["lineage"]["after"]))
        return best

    def _step(self, node: str, run_id: str | None, **detail: Any) -> None:
        self.store.audit(kind="step", node=node, run_id=run_id, actor=SUPERVISOR, status=node, **detail)

    def _staged_for(self, run: dict[str, Any]) -> gate.Staged:
        staged = self._staged.get(run["run_id"])
        if staged is None:  # a resumed process: stage again, it is a sample in memory
            staged = gate.stage(run["change"], self.bronze, run["run_id"], self.sample_rows,
                                upstream=self.upstream(run["layer"]))
            self._staged[run["run_id"]] = staged
        return staged

    def _drop_staged(self, run_id: str) -> None:
        staged = self._staged.pop(run_id, None)
        if staged is not None:
            staged.close()

    def _change(self, attempt: int, feedback: dict[str, Any] | None, requested_by: str) -> Change:
        change = self.agent.draft(attempt, feedback)
        # The sub-agent cannot pick who it is or who asked.
        change.agent, change.requested_by = self.agent.name, requested_by
        return change

    # -------------------------------------------------------- the graph

    def graph(self) -> StateGraph:
        tracer = self.store.tracer

        def node(name: str):
            def wrap(fn):
                def inner(s: RunState) -> dict[str, Any]:
                    with tracer.span(f"supervisor.{name}", run_id=s.get("run_id"), agent=self.agent.name):
                        return fn(s)
                return inner
            return wrap

        @node("draft")
        def draft(s: RunState) -> dict[str, Any]:
            rid = s["run_id"]
            try:
                run = self.store.run(rid)
            except GateError:
                change = self._change(1, None, s["requested_by"])
                gate.open_run(self.store, change, self.sample_rows, source=self.source, run_id=rid)
                self._step("draft", rid, attempt=1)
                return {}
            last = run.get("evaluation") or {}
            feedback: dict[str, Any] = {"attempt": run["attempt"], "failed": [g for g, v in last.get("gates", {}).items()
                                                                              if not v["passed"]],
                                        "gates": last.get("gates", {}), "change": run["change"]}
            if run.get("rejected"):
                feedback["rejected"] = run["rejected"]
            change = self._change(run["attempt"], feedback, run["requested_by"])
            gate.set_change(self.store, rid, change)
            if run.get("rejected"):
                run = self.store.run(rid)
                run["rejections"] = run.get("rejections", []) + [run.pop("rejected")]
                self.store.put_run(run)
            self._step("draft", rid, attempt=run["attempt"])
            return {}

        @node("stage")
        def stage(s: RunState) -> dict[str, Any]:
            rid = s["run_id"]
            self._drop_staged(rid)
            gate.transition(self.store, rid, State.STAGED, actor=self.agent.name)
            staged = self._staged_for(self.store.run(rid))
            self._step("stage", rid, schema=staged.schema, inputs=staged.inputs,
                       tables={t: m["rows"] for t, m in staged.tables.items()}, errors=sorted(staged.errors))
            return {}

        @node("evaluate")
        def evaluate(s: RunState) -> dict[str, Any]:
            rid = s["run_id"]
            run = self.store.run(rid)
            staged = self._staged_for(run)
            change = Change.from_dict(run["change"])
            scores = ev.evaluate(change, staged, self.pii, baseline_coverage=self._baseline(run),
                                 coverage=self.coverage, compliance_policy=self.compliance_policy)
            gate.record_evaluation(self.store, rid, scores, actor=EVALUATOR)
            self._step("evaluate", rid, passed=scores["passed"], evidence=scores["evidence"])
            if not scores["passed"] and run["attempt"] < gate.MAX_ATTEMPTS:
                self._drop_staged(rid)
                gate.retry(self.store, rid, actor=SUPERVISOR)
            return {}

        def after_evaluate(s: RunState) -> str:
            return "draft" if self.store.run(s["run_id"])["state"] == State.DRAFTED else "escalate"

        @node("escalate")
        def escalate(s: RunState) -> dict[str, Any]:
            rid = s["run_id"]
            run = self.store.run(rid)
            staged = self._staged_for(run)
            card = gate.package_card(self.store, rid, staged, run["evaluation"], self.live_models(run["layer"]),
                                     self.pii, actor=SUPERVISOR)
            self._drop_staged(rid)
            self._step("escalate", rid, card_id=card["card_id"], card_kind=card["kind"])
            return {"card_id": card["card_id"]}

        @node("approval")
        def approval(s: RunState) -> dict[str, Any]:
            # nothing before interrupt(): this node reruns from the top on resume.
            # The value it returns is not read; settle reads the card store.
            interrupt({"card_id": s["card_id"], "run_id": s["run_id"]})
            return {}

        @node("settle")
        def settle(s: RunState) -> dict[str, Any]:
            run = self.store.run(s["run_id"])
            card = self.store.card(s["card_id"])
            self._step("settle", s["run_id"], card_id=s["card_id"], state=run["state"],
                       decided=bool(card.get("decision")))
            return {}

        def after_settle(s: RunState) -> str:
            state = self.store.run(s["run_id"])["state"]
            if state == State.APPROVED:
                return "promote"
            if state == State.DRAFTED:
                return "draft"
            return "approval"

        @node("promote")
        def promote(s: RunState) -> dict[str, Any]:
            rid = s["run_id"]
            run = self.store.run(rid)
            with tracer.span("supervisor.watch", run_id=rid, action=f"bridge.promote.{run['layer']}") as w:
                try:
                    res = gate.promote(self.store, rid, policy=self.policy, live_dir=self.live_dir,
                                       writer=self.writer, actor=self.agent.name)
                except GateError as e:
                    w.event("refused", reason=str(e))
                    self._step("promote", rid, refused=str(e))
                    return {"refused": str(e)}
                if res["status"] != "promoted":
                    w.event("refused", reason=res.get("reason"))
                    self._step("promote", rid, refused=res.get("reason"))
                    return {"refused": f"policy denied: {res.get('reason')}"}
                if self.live_dir is not None:
                    live = self.live_models(run["layer"])
                    w.set(deployed_matches=all(live.get(f) == sql for f, sql in run["change"]["models"].items()))
                w.set(audit_intact=self.store.verify_audit_chain())
                self._step("promote", rid, audit_seq=res["audit_seq"], approved_by=res["approved_by"])
            return {}

        def after_promote(s: RunState) -> str:
            return END if s.get("refused") else "observe"

        @node("observe")
        def observe(s: RunState) -> dict[str, Any]:
            gate.observe(self.store, s["run_id"], monitors=MONITORS, actor=SUPERVISOR)
            self._step("observe", s["run_id"], monitors=MONITORS)
            return {}

        g = StateGraph(RunState)
        for name, fn in [("draft", draft), ("stage", stage), ("evaluate", evaluate), ("escalate", escalate),
                         ("approval", approval), ("settle", settle), ("promote", promote), ("observe", observe)]:
            g.add_node(name, fn)
        g.add_edge(START, "draft")
        g.add_edge("draft", "stage")
        g.add_edge("stage", "evaluate")
        g.add_conditional_edges("evaluate", after_evaluate, {"draft": "draft", "escalate": "escalate"})
        g.add_edge("escalate", "approval")
        g.add_edge("approval", "settle")
        g.add_conditional_edges("settle", after_settle, {"promote": "promote", "draft": "draft",
                                                        "approval": "approval"})
        g.add_conditional_edges("promote", after_promote, {"observe": "observe", END: END})
        g.add_edge("observe", END)
        return g

    # -------------------------------------------------------- running it

    def _invoke(self, run_id: str, value: Any) -> dict[str, Any]:
        conn = sqlite3.connect(self.store.root / "checkpoints.sqlite", check_same_thread=False)
        try:
            app = self.graph().compile(checkpointer=SqliteSaver(conn))
            cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 80}
            try:
                out = app.invoke(value, cfg)
            finally:
                self._drop_staged(run_id)
            values = app.get_state(cfg).values
        finally:
            conn.close()
        run = self.store.run(run_id)
        return {"run_id": run_id, "state": run["state"], "card_id": run.get("card_id"),
                "interrupted": "__interrupt__" in out, "refused": values.get("refused")}

    def start(self, requested_by: str) -> dict[str, Any]:
        run_id = gate.new_run_id()
        with self.store.tracer.span("supervisor.run", run_id=run_id, agent=self.agent.name,
                                    requested_by=requested_by):
            return self._invoke(run_id, {"run_id": run_id, "requested_by": requested_by})

    def resume(self, run_id: str, payload: Any = None) -> dict[str, Any]:
        """Wake the graph after a human decided in the inbox. The payload is carried for
        LangGraph and never read."""
        self.store.run(run_id)  # a bad or foreign id stops here
        with self.store.tracer.span("supervisor.resume", run_id=run_id):
            return self._invoke(run_id, Command(resume=payload if payload else {"resumed": True}))


# ---------------------------------------------------------------- the first sub-agent

class SilverBuilder:
    """Silver models from the silver draft (F2), one per drafted source. It drafts what the
    human kept in the Map step and learns only from the gate's feedback: a column the
    compliance check found unmasked comes back hashed on the next attempt."""
    name = "agent:silver-builder"

    def __init__(self, made: dict[str, Any], workspace: dict[str, Any], declined: list[str] | None = None,
                 renames: dict[str, dict[str, str]] | None = None, purpose: str = "silver models for the ontology and Ask"):
        from ..ingest import silver_draft as sd
        self.made, self.workspace = made, workspace
        self.declined, self.renames = list(declined or []), dict(renames or {})
        self.purpose = purpose
        self.files = sd.models(made, workspace, self.declined, self.renames)
        self.mask: set[tuple[str, str]] = set()

    def draft(self, attempt: int, feedback: dict[str, Any] | None) -> Change:
        # What the last change hashed stays hashed, so a restart or a rejection never
        # unlearns a mask. What compliance found unmasked is hashed from now on.
        for table, cols in ((feedback or {}).get("change") or {}).get("masking", {}).items():
            self.mask |= {(table, c) for c in cols}
        for ref in ((feedback or {}).get("gates", {}).get("compliance", {}) or {}).get("unmasked", []):
            table, _, col = ref.partition(".")
            self.mask.add((table, col))
        specs = {s["name"]: s for s in self.workspace["sources"]}
        models, contract, tests, dq, masking = {}, {}, [], [], {}
        drafted = {s["file"]: s for s in self.made["sources"] if "error" not in s}
        for file_name, sql in sorted(self.files.items()):
            table = gate.model_table(file_name)
            src = drafted.get(file_name, {})
            renames = self.renames.get(src.get("name", ""), {})
            spec_cols = {c["name"]: c for c in specs.get(src.get("name", ""), {}).get("columns", [])}
            cols = [(renames.get(p["name"], p["name"]), spec_cols.get(p["name"], {}).get("type"))
                    for p in src.get("columns", []) if p["name"] in spec_cols]
            hide = sorted(c for t, c in self.mask if t == table and c in {n for n, _ in cols})
            if hide:
                swaps = ", ".join(f'sha256(cast("{c}" as varchar)) as "{c}"' for c in hide)
                sql = f"select * replace ({swaps}) from (\n{sql.rstrip()}\n) as drafted\n"
                masking[table] = {c: "hash" for c in hide}
            models[file_name] = sql
            contract[table] = [{"name": n} for n, _ in cols]
            key = renames.get(src.get("key") or "", src.get("key"))
            if key:
                tests.append({"name": f"{table}_{key}_unique",
                              "sql": f'select "{key}" from "{table}" group by 1 having count(*) > 1'})
                dq.append({"table": table, "column": key, "check": "not_null"})
        note = f"attempt {attempt}" + (f", hashing {', '.join(f'{t}.{c}' for t, c in sorted(self.mask))}"
                                       if self.mask else "")
        return Change(layer="silver", models=models, contract=contract, tests=tests, dq=dq, masking=masking,
                      purpose=self.purpose, note=note)


# ---------------------------------------------------------------- the run board

def board(store: BridgeStore | None) -> dict[str, Any]:
    """Layers by agents, with the queue, retries and escalations. Reads only the store."""
    if store is None:
        return {"layers": list(gate.LAYERS), "agents": [], "cells": [], "queue": [], "runs": []}
    runs = store.runs()
    cards = store.cards()
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for r in runs:
        cell = cells.setdefault((r["layer"], r["agent"]), {"layer": r["layer"], "agent": r["agent"], "runs": 0,
                                                           "states": {}, "retries": 0, "escalations": 0})
        cell["runs"] += 1
        cell["states"][r["state"]] = cell["states"].get(r["state"], 0) + 1
        cell["retries"] += sum(1 for h in r["history"] if h["from"] == State.EVALUATED and h["to"] == State.DRAFTED)
        cell["escalations"] += len(r.get("cards", []))
    queue = [{k: c.get(k) for k in ("card_id", "run_id", "kind", "layer", "agent", "requested_by", "tables",
                                    "content_hash", "passed", "created")}
             for c in cards if c.get("decision") is None
             and next((r for r in runs if r["run_id"] == c["run_id"]), {}).get("card_id") == c["card_id"]]
    summaries = [{"run_id": r["run_id"], "layer": r["layer"], "agent": r["agent"], "state": r["state"],
                  "attempt": r["attempt"], "cycle": r["cycle"], "tables": r["tables"],
                  "requested_by": r["requested_by"], "card_id": r.get("card_id"),
                  "evaluations": [{k: e[k] for k in ("attempt", "cycle", "passed", "failed")} for e in r["evaluations"]],
                  "created": r.get("created")} for r in reversed(runs)]
    return {"layers": list(gate.LAYERS), "agents": sorted({r["agent"] for r in runs}),
            "cells": sorted(cells.values(), key=lambda c: (gate.LAYERS.index(c["layer"]), c["agent"])),
            "queue": queue, "runs": summaries}
