"""Decision agent on LangGraph.

  intake -> gather -> diagnose -> narrate -> propose -> [approval] -> execute -> done

Design rules from the prep guide, applied for real:
- State holds IDs and small summaries, never whole records, so checkpoints stay small.
- The agent never touches tables. It only calls ontology functions (read) and
  ontology actions (write), and every call passes the policy engine.
- Diagnosis is deterministic rules over evidence; the LLM only writes the
  narrative, and every claim must cite an evidence ID or a guardrail rewrites it.
- The approval node does nothing before interrupt(). On resume LangGraph reruns
  the interrupted node from the top, so side effects live in propose and execute,
  never in the node that pauses.
- Checkpoints are keyed by thread_id in SQLite, so a review can take days.
AWS mapping: this graph would run on AgentCore Runtime; functions and actions
would be exposed through AgentCore Gateway as MCP tools with Cedar policies.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..expr import evaluate
from ..llm import LLM
from ..ontology.runtime import Ontology
from ..packs import Pack
from ..tracing import Tracer


class AgentState(TypedDict, total=False):
    usecase: str
    target: dict[str, Any]
    gt_key: str
    objects: dict[str, dict[str, Any]]
    evidence: dict[str, dict[str, Any]]
    diagnosis: dict[str, Any]
    narrative: str
    grounded: bool
    proposal: dict[str, Any]
    review: dict[str, Any]
    outcome: dict[str, Any]
    events: list[str]


def _fmt(template: str, env: dict[str, Any]) -> str:
    def rep(m: re.Match) -> str:
        try:
            v = evaluate(m.group(1), env)
        except Exception:
            return m.group(0)
        if isinstance(v, float):
            return f"{v:,.2f}".rstrip("0").rstrip(".")
        return str(v)
    return re.sub(r"\{([^{}]+)\}", rep, template)


def _slim(ev: dict[str, Any]) -> dict[str, Any]:
    return {"id": ev["id"], "function": ev["function"], "summary": ev["summary"],
            "rows": ev["rows"][:12], "row_count": len(ev["rows"])}


def build_graph(pack: Pack, usecase: str, ont: Ontology, llm: LLM, tracer: Tracer):
    uc = pack.usecases["usecases"][usecase]
    principal = uc["agent"]
    on_behalf = uc.get("on_behalf_of")

    def node(name):
        def deco(fn):
            def wrapped(state: AgentState) -> dict[str, Any]:
                with tracer.span(f"node.{name}"):
                    return fn(state)
            return wrapped
        return deco

    @node("intake")
    def intake(s: AgentState) -> dict[str, Any]:
        objs = {}
        for p, t in uc["target_params"].items():
            o = ont.masked(ont.store.get(t, s["target"][p]), principal) or {}
            objs[p] = {k: v for k, v in o.items() if k not in ("_prov",)}
        return {"objects": objs, "events": []}

    @node("gather")
    def gather(s: AgentState) -> dict[str, Any]:
        env = {"target": s["target"]}
        ev = {}
        for name, spec in uc["evidence"].items():
            args = {k: evaluate(str(v), env) for k, v in spec["args"].items()}
            with tracer.span(f"tool.{spec['fn']}", via="tool-layer", principal=principal):
                ev[name] = _slim(ont.call(spec["fn"], args, principal=principal))
        return {"evidence": ev}

    def _env(s: AgentState) -> dict[str, Any]:
        return {"target": s["target"], "objects": s.get("objects", {}),
                "evidence": {k: {"summary": v["summary"] or {}, "rows": v["rows"], "row_count": v["row_count"],
                                 "id": v["id"]} for k, v in s.get("evidence", {}).items()},
                "diagnosis": s.get("diagnosis", {})}

    @node("diagnose")
    def diagnose(s: AgentState) -> dict[str, Any]:
        env = _env(s)
        for rule in uc["diagnoses"]:
            try:
                hit = bool(evaluate(rule["when"], env))
            except Exception:
                hit = False
            if hit:
                Tracer.current().event("diagnosis", label=rule["label"])
                return {"diagnosis": {"label": rule["label"], "confidence": rule.get("confidence", 0.6),
                                      "explain": _fmt(rule.get("explain", ""), env).strip(),
                                      "cite": [s["evidence"][c]["id"] for c in rule.get("cite", []) if c in s["evidence"]],
                                      "action": rule.get("action")}}
        return {"diagnosis": {"label": "no_decision", "confidence": 0.0, "explain": "No rule matched.",
                              "cite": [], "action": None}}

    @node("narrate")
    def narrate(s: AgentState) -> dict[str, Any]:
        d = s["diagnosis"]
        cites = d["cite"]
        template = f"{d['explain']} " + " ".join(f"[ev:{c}]" for c in cites)
        nar = uc.get("narrative", {})
        if llm.offline:
            return {"narrative": template.strip(), "grounded": True}
        facts = json.dumps({k: {"id": v["id"], "summary": v["summary"], "sample_rows": v["rows"][:5]}
                            for k, v in s["evidence"].items()}, default=str)[:6000]
        prompt = (f"Decision: {uc['decision']}\nObjects: {json.dumps(s['objects'], default=str)[:1500]}\n"
                  f"Evidence (cite as [ev:ID]): {facts}\nDiagnosis: {d['label']} ({d['explain']})\n"
                  f"Write 3 to 5 plain sentences for {nar.get('audience', 'the reviewer')}. Cite every fact with "
                  f"its [ev:ID]. Do not invent numbers.")
        with tracer.span("llm.call", model=llm.model, provider=llm.provider) as sp:
            res = llm.complete(nar.get("system", "You are a careful operations analyst."), prompt)
            sp.set(tokens_in=res.tokens_in, tokens_out=res.tokens_out, latency_ms=res.latency_ms)
        text = res.text or ""
        cited = set(re.findall(r"\[ev:([^\]]+)\]", text))
        known = {v["id"] for v in s["evidence"].values()}
        grounded = bool(text) and set(cites) <= cited and cited <= known
        if not grounded:
            Tracer.current().event("guardrail.rewrite", reason="missing or unknown citations")
            return {"narrative": template.strip(), "grounded": False}
        return {"narrative": text.strip(), "grounded": True}

    def _args(s: AgentState, spec: dict[str, Any]) -> dict[str, Any]:
        env = _env(s)
        return {k: evaluate(str(v), env) for k, v in spec.get("args", {}).items()}

    @node("propose")
    def propose(s: AgentState) -> dict[str, Any]:
        spec = s["diagnosis"].get("action")
        if not spec:
            return {"proposal": {"status": "no_action"}}
        attempts = []
        for cand in [spec] + ([spec["fallback"]] if spec.get("fallback") else []):
            args = _args(s, cand)
            res = ont.act(cand["name"], args, actor=principal, on_behalf_of=on_behalf,
                          idempotency_key=f"{s['gt_key']}:{cand['name']}", evidence=s["diagnosis"]["cite"])
            attempts.append({"action": cand["name"], "args": args, "status": res["status"], "reason": res["reason"]})
            if res["status"] != "denied":
                return {"proposal": {**res, "args": args, "attempts": attempts}}
        return {"proposal": {"status": "denied", "attempts": attempts, "action": spec["name"]}}

    def route(s: AgentState) -> str:
        return "approval" if s["proposal"].get("status") == "needs_approval" else "done"

    @node("approval")
    def approval(s: AgentState) -> dict[str, Any]:
        # nothing before interrupt(): this node reruns from the top on resume
        decision = interrupt({"action": s["proposal"]["action"], "args": s["proposal"]["args"],
                              "narrative": s["narrative"], "diagnosis": s["diagnosis"]["label"],
                              "approver_roles": s["proposal"].get("approver_roles", [])})
        return {"review": decision}

    @node("execute")
    def execute(s: AgentState) -> dict[str, Any]:
        r = s.get("review", {})
        if not r.get("approved"):
            return {"outcome": {"status": "rejected_by_reviewer", "by": r.get("by"), "note": r.get("note")}}
        p = s["proposal"]
        res = ont.act(p["action"], p["args"], actor=principal, on_behalf_of=on_behalf,
                      idempotency_key=f"{s['gt_key']}:{p['action']}:approved", approved_by=r.get("by"),
                      evidence=s["diagnosis"]["cite"])
        return {"outcome": {**res, "approved_by": r.get("by")}}

    @node("done")
    def done(s: AgentState) -> dict[str, Any]:
        if s.get("outcome"):
            return {}
        return {"outcome": s.get("proposal", {"status": "no_action"})}

    g = StateGraph(AgentState)
    for name, fn in [("intake", intake), ("gather", gather), ("diagnose", diagnose), ("narrate", narrate),
                     ("propose", propose), ("approval", approval), ("execute", execute), ("done", done)]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "gather")
    g.add_edge("gather", "diagnose")
    g.add_edge("diagnose", "narrate")
    g.add_edge("narrate", "propose")
    g.add_conditional_edges("propose", route, {"approval": "approval", "done": "done"})
    g.add_edge("approval", "execute")
    g.add_edge("execute", "done")
    g.add_edge("done", END)
    return g


def run_usecase(pack: Pack, usecase: str, tracer: Tracer, limit: int | None = None,
                auto_review: bool = True, llm: LLM | None = None) -> dict[str, Any]:
    llm = llm or LLM()
    uc = pack.usecases["usecases"][usecase]
    ont = Ontology(pack, tracer)
    targets = ont.store.con.execute(uc["targets_sql"]).fetchdf().to_dict("records")
    if limit:
        targets = targets[:limit]
    conn = sqlite3.connect(pack.paths.checkpoints, check_same_thread=False)
    saver = SqliteSaver(conn)
    app = build_graph(pack, usecase, ont, llm, tracer).compile(checkpointer=saver)
    runs = []
    for t in targets:
        gt_key = str(t.pop("gt_key"))
        thread = f"{usecase}:{gt_key}:{int(time.time())}"
        cfg = {"configurable": {"thread_id": thread}}
        t0 = time.time()
        with tracer.span("agent.run", usecase=usecase, thread_id=thread, acting_for=uc.get("on_behalf_of"),
                         agent=uc["agent"], llm=llm.model) as sp:
            first_trace = sp.trace_id
            out = app.invoke({"usecase": usecase, "target": t, "gt_key": gt_key}, cfg)
            interrupted = "__interrupt__" in out
            if interrupted:
                sp.event("interrupt", reason="needs_approval", checkpoint_thread=thread)
        resumed = None
        if interrupted and auto_review:
            st = app.get_state(cfg).values
            rv = uc.get("review", {})
            env = {"diagnosis": st["diagnosis"], "proposal": st["proposal"], "target": st["target"]}
            ok = bool(evaluate(rv.get("approve_when", "True"), env))
            decision = {"approved": ok, "by": rv.get("reviewer", "user:sim.reviewer"),
                        "note": "approved by simulated reviewer rule" if ok else "rejected by simulated reviewer rule"}
            with tracer.span("agent.resume", usecase=usecase, thread_id=thread) as sp2:
                sp2.link(first_trace, "resume after human review, same thread_id")
                out = app.invoke(Command(resume=decision), cfg)
                resumed = sp2.trace_id
        final = app.get_state(cfg).values
        runs.append({
            "thread_id": thread, "gt_key": gt_key, "target": t,
            "objects": {k: v.get("_title") for k, v in final.get("objects", {}).items()},
            "diagnosis": final.get("diagnosis", {}).get("label"),
            "confidence": final.get("diagnosis", {}).get("confidence"),
            "cited": final.get("diagnosis", {}).get("cite", []),
            "evidence_ids": [v["id"] for v in final.get("evidence", {}).values()],
            "narrative": final.get("narrative"), "grounded": final.get("grounded"),
            "proposal": {k: final.get("proposal", {}).get(k) for k in ("action", "status", "args", "attempts")},
            "review": final.get("review"),
            "outcome": {k: final.get("outcome", {}).get(k) for k in ("status", "action", "reason", "approved_by", "created")},
            "interrupted": interrupted, "trace_id": first_trace, "resume_trace_id": resumed,
            "latency_ms": round((time.time() - t0) * 1000, 1),
        })
    conn.close()
    delivered = ont.deliver_outbox()
    ont.store.con.close()
    return {"usecase": usecase, "title": uc.get("title"), "decision": uc.get("decision"),
            "agent": uc["agent"], "llm": llm.model, "runs": runs, "outbox": delivered}
