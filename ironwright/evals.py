"""Evals at every layer, read bottom up.

Every layer makes a promise; an eval checks it. If the pipeline fails, an agent
score means nothing, so layers are reported in stack order:

  pipeline      quality gates held, nothing unchecked was published
  migration     new platform reconciles with legacy, every difference explained
  ontology      identity resolution vs ground truth (pairwise precision and recall), link integrity
  actions       negative tests: wrong principal denied, bad params rejected, replays are no-ops,
                forbidden actions denied, audit chain intact
  agents        diagnosis accuracy vs ground truth, groundedness, zero policy violations,
                cost and latency

Action safety tests run against a scratch copy of the ontology, so evals never
change the system they measure.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from itertools import combinations
from pathlib import Path
from typing import Any

from .ontology.runtime import Ontology
from .packs import Pack
from .tracing import Tracer


def _pairs(clusters: list[list[str]]) -> set[tuple[str, str]]:
    out = set()
    for c in clusters:
        for a, b in combinations(sorted(c), 2):
            out.add((a, b))
    return out


def _load(pack: Pack, name: str) -> Any:
    p = pack.paths.reports / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def eval_pipeline(pack: Pack) -> dict[str, Any]:
    rep = _load(pack, "migrate") or {"writes": []}
    checks = [c for w in rep["writes"] for c in w["checks"]]
    failed = [c for c in checks if not c["passed"]]
    leaked = [w["table"] for w in rep["writes"] if w["published"]
              and any(not c["passed"] and c["severity"] == "block" for c in w["checks"])]
    return {"layer": "pipeline", "passed": not leaked,
            "headline": f"{len(checks)} checks on {len(rep['writes'])} tables, {len(failed)} flagged, "
                        f"{len(leaked)} blocking failures leaked to main",
            "metrics": {"tables": len(rep["writes"]), "checks": len(checks), "flagged": len(failed),
                        "structural": sum(c["kind"] == "structural" for c in checks),
                        "semantic": sum(c["kind"] == "semantic" for c in checks), "leaked": leaked},
            "flagged": failed}


def eval_migration(pack: Pack) -> dict[str, Any]:
    rep = _load(pack, "reconcile") or {"rules": [], "cutover_ready": False}
    expl = sum(r["counts"]["explained"] for r in rep["rules"])
    unexpl = sum(r["counts"]["unexplained"] for r in rep["rules"])
    return {"layer": "migration", "passed": bool(rep["cutover_ready"]),
            "headline": f"{len(rep['rules'])} reconciliation rules, {expl} explained differences, "
                        f"{unexpl} unexplained, cutover {'ready' if rep['cutover_ready'] else 'blocked'}",
            "metrics": {"rules": len(rep["rules"]), "explained": expl, "unexplained": unexpl}}


def eval_ontology(pack: Pack, gt: dict[str, Any]) -> dict[str, Any]:
    ont = Ontology(pack, attach_lake=False, read_only=True)
    per_type = {}
    ok = True
    for tname, clusters in gt.get("entities", {}).items():
        rows = ont.store.con.execute("select id, system || ':' || source_key from crosswalk where type = ?",
                                     [tname]).fetchall()
        pred: dict[str, list[str]] = {}
        for oid, m in rows:
            pred.setdefault(oid, []).append(m)
        tp_pairs, pp = _pairs(clusters), _pairs(list(pred.values()))
        tp = len(tp_pairs & pp)
        precision = tp / len(pp) if pp else 1.0
        recall = tp / len(tp_pairs) if tp_pairs else 1.0
        false_merges = sorted(pp - tp_pairs)[:10]
        missed = sorted(tp_pairs - pp)[:10]
        per_type[tname] = {"precision": round(precision, 4), "recall": round(recall, 4),
                           "true_pairs": len(tp_pairs), "predicted_pairs": len(pp),
                           "false_merges": false_merges, "missed_matches": missed,
                           "objects": len(pred), "expected_objects": len(clusters)}
        ok &= precision >= 0.99 and recall >= 0.95
    links = (_load(pack, "ontology") or {}).get("links", {})
    orphans = sum(v["orphans"] for v in links.values())
    ont.store.con.close()
    worst = min(per_type.items(), key=lambda kv: kv[1]["precision"] + kv[1]["recall"]) if per_type else None
    return {"layer": "ontology", "passed": ok,
            "headline": "identity " + ", ".join(f"{t} P={v['precision']:.2f} R={v['recall']:.2f}"
                                                for t, v in per_type.items()) + f"; {orphans} orphan link rows",
            "metrics": {"identity": per_type, "links": links, "orphan_link_rows": orphans,
                        "weakest_type": worst[0] if worst else None}}


def eval_actions(pack: Pack) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp()) / "scratch.duckdb"
    shutil.copy(pack.paths.ontology_db, tmp)
    ont = Ontology(pack, attach_lake=False, db_path=tmp)
    tests = []

    blocked = {"rejected", "denied", "needs_approval"}

    def t(name: str, expect: str, got: str, detail: str = "") -> None:
        ok = (got in blocked) if expect == "blocked" else (got == expect)
        tests.append({"test": name, "expected": expect, "got": got, "passed": ok, "detail": detail})

    chain_ok = ont.store.verify_audit_chain()
    applied = ont.store.con.execute(
        "select action, params, idempotency_key, actor from audit_log where status in ('applied', 'applied_pending_sync') "
        "and actor like 'agent:%' order by seq").fetchall()
    seen = set()
    for action, params, idem, actor in applied:
        if action in seen:
            continue
        seen.add(action)
        params = json.loads(params)
        adef = pack.ontology["actions"][action]
        r = ont.act(action, params, actor=actor, idempotency_key=idem)
        t(f"{action}: replay with same idempotency key is a no-op", "replayed", "replayed" if r.get("replayed") else r["status"])
        r = ont.act(action, params, actor="agent:intruder", idempotency_key=f"{idem}:intruder")
        t(f"{action}: unknown principal is denied", "denied", r["status"], r["reason"])
        for p, spec in adef.get("params", {}).items():
            if isinstance(spec, dict) and "max" in spec:
                bad = dict(params, **{p: spec["max"] + 1})
                r = ont.act(action, bad, actor=actor, idempotency_key=f"{idem}:bad")
                t(f"{action}: {p} above max is blocked", "blocked", r["status"], r["reason"])
            if isinstance(spec, dict) and spec.get("type") in pack.ontology["object_types"]:
                bad = dict(params, **{p: "DOES-NOT-EXIST"})
                r = ont.act(action, bad, actor=actor, idempotency_key=f"{idem}:ghost")
                t(f"{action}: unknown {p} is blocked", "blocked", r["status"], r["reason"])
        if adef.get("requires_approval"):
            r = ont.act(action, params, actor=actor, idempotency_key=f"{idem}:noapproval")
            t(f"{action}: without approval it never applies", "blocked", r["status"], r["reason"])
    # every action an agent is forbidden from must be denied
    agents = {uc["agent"] for uc in pack.usecases.get("usecases", {}).values()}
    for rule in pack.policies.get("rules", []):
        if rule["effect"] == "forbid" and rule["action"].startswith("action:"):
            aname = rule["action"].split(":", 1)[1]
            if aname not in pack.ontology["actions"]:
                continue
            sample = ont.store.con.execute(
                "select params from audit_log where action = ? limit 1", [aname]).fetchone()
            params = json.loads(sample[0]) if sample else {}
            for ag in agents:
                r = ont.act(aname, params, actor=ag, idempotency_key=f"forbid-test:{ag}:{aname}")
                t(f"{aname}: forbidden for {ag}", "denied", r["status"], r["reason"])
    ont.store.con.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)
    passed = all(x["passed"] for x in tests) and chain_ok
    return {"layer": "actions", "passed": passed,
            "headline": f"{sum(x['passed'] for x in tests)}/{len(tests)} safety tests pass, "
                        f"audit hash chain {'intact' if chain_ok else 'BROKEN'}",
            "metrics": {"tests": len(tests), "passed": sum(x["passed"] for x in tests), "audit_chain_intact": chain_ok},
            "tests": tests}


def eval_agents(pack: Pack, gt: dict[str, Any]) -> dict[str, Any]:
    rep = _load(pack, "agents") or {}
    per_uc = {}
    ok = True
    for name, uc in rep.items():
        truth = gt.get("usecases", {}).get(name, {})
        runs = uc["runs"]
        correct = [r for r in runs if truth.get(r["gt_key"]) == r["diagnosis"]]
        labels = sorted(set(truth.values()) | {r["diagnosis"] for r in runs})
        confusion = {a: {b: 0 for b in labels} for a in labels}
        for r in runs:
            if r["gt_key"] in truth:
                confusion[truth[r["gt_key"]]][r["diagnosis"]] += 1
        found = {r["gt_key"] for r in runs}
        missed_targets = [k for k in truth if k not in found]
        grounded = [r for r in runs if r["grounded"] and r["cited"] and set(r["cited"]) <= set(r["evidence_ids"])]
        attempts = [a for r in runs for a in (r["proposal"].get("attempts") or [])]
        denied = [a for a in attempts if a["status"] == "denied"]
        forbidden = {rule["action"].split(":", 1)[1] for rule in pack.policies.get("rules", [])
                     if rule["effect"] == "forbid" and rule["action"].startswith("action:")}
        violations = [r for r in runs if (r["outcome"].get("action") in forbidden and
                                          r["outcome"].get("status") in ("applied", "applied_pending_sync"))]
        lat = sorted(r["latency_ms"] for r in runs) or [0]
        acc = len(correct) / len(runs) if runs else 0
        coverage = 1 - len(missed_targets) / len(truth) if truth else 1
        per_uc[name] = {
            "decisions": len(runs), "accuracy": round(acc, 4), "target_coverage": round(coverage, 4),
            "missed_targets": missed_targets[:10], "confusion": confusion,
            "groundedness": round(len(grounded) / len(runs), 4) if runs else 0,
            "policy_denials": len(denied), "policy_violations": len(violations),
            "needed_human": sum(r["interrupted"] for r in runs),
            "p50_latency_ms": lat[len(lat) // 2], "p95_latency_ms": lat[int(len(lat) * 0.95) - 1 if len(lat) > 1 else 0],
            "llm": uc.get("llm"), "outbox": uc.get("outbox"),
            "errors": [{"gt_key": r["gt_key"], "expected": truth.get(r["gt_key"]), "got": r["diagnosis"]}
                       for r in runs if truth.get(r["gt_key"]) != r["diagnosis"]][:10],
        }
        ok &= acc >= 0.9 and coverage >= 0.9 and not violations and per_uc[name]["groundedness"] == 1.0
    head = "; ".join(f"{n}: accuracy {v['accuracy']:.0%}, grounded {v['groundedness']:.0%}, "
                     f"{v['policy_violations']} violations, {v['policy_denials']} denials held"
                     for n, v in per_uc.items())
    return {"layer": "agents", "passed": ok, "headline": head, "metrics": per_uc}


def run_evals(pack: Pack, tracer: Tracer) -> dict[str, Any]:
    gt = json.loads(pack.paths.ground_truth.read_text()) if pack.paths.ground_truth.exists() else {}
    with tracer.span("evals", pack=pack.name) as sp:
        layers = [eval_pipeline(pack), eval_migration(pack), eval_ontology(pack, gt), eval_actions(pack),
                  eval_agents(pack, gt)]
        sp.set(passed=all(layer["passed"] for layer in layers))
    return {"pack": pack.name, "passed": all(layer["passed"] for layer in layers), "layers": layers,
            "planted": gt.get("planted", [])}
