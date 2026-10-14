"""Export one pack's run as a single JSON snapshot for the web demo.

The demo is static (Cloudflare Pages, S3 or any CDN), so everything it shows
comes from this file: the legacy mess, the medallion and its quality gates,
reconciliation, the ontology graph, agent decisions with full traces, and evals.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import data_root
from .ontology.store import OntologyStore
from .packs import Pack
from .tracing import load_trace


def _load(pack: Pack, name: str) -> Any:
    p = pack.paths.reports / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _legacy_samples(pack: Pack) -> list[dict[str, Any]]:
    out = []
    for src in pack.spec["sources"] + pack.spec.get("legacy_reports", []):
        sample = ""
        try:
            if src["kind"] == "fixed_width":
                sample = "\n".join((pack.paths.legacy / src["file"]).read_text().splitlines()[:4])
            elif src["kind"] in ("csv", "jsonl"):
                files = sorted(pack.paths.legacy.glob(src["glob"]))
                if files:
                    sample = f"# {files[0].name} (+{len(files) - 1} more files)\n" + \
                             "\n".join(files[0].read_text().splitlines()[:4])
            elif src["kind"] == "sqlite":
                sample = src["query"]
        except Exception:
            pass
        out.append({"name": src["name"], "system": src.get("system"), "label": src.get("label", src["name"]),
                    "kind": src["kind"], "sample": sample})
    return out


def _traces_for(pack: Pack, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for tid in ids:
        f = pack.paths.traces / f"{tid}.jsonl"
        if tid and f.exists():
            out[tid] = load_trace(f)
    return out


def export_pack(pack: Pack, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or data_root() / "_web"
    out_dir.mkdir(parents=True, exist_ok=True)
    store = OntologyStore(pack.paths.ontology_db, read_only=True)
    q = lambda s, *a: store.con.execute(s, list(a)).fetchall()  # noqa: E731

    merges = []
    for t, oid, members, method, conf in q("select type, id, members, method, confidence from merge_log "
                                           "where json_array_length(members) > 1 order by json_array_length(members) desc, type, method desc limit 40"):
        o = store.get(t, oid) or {}
        merges.append({"type": t, "id": oid, "title": o.get("_title"), "members": json.loads(members),
                       "method": method, "confidence": conf, "provenance": o.get("_prov", {})})
    review = [dict(zip(("type", "a", "b", "score", "reason"), r))
              for r in q("select type, a, b, score, reason from review_queue limit 20")]

    agents = _load(pack, "agents") or {}
    showcase_ids: list[str] = []
    graph_nodes, graph_edges = {}, []
    for uc in agents.values():
        seen_labels = set()
        for r in uc["runs"]:
            if r["diagnosis"] not in seen_labels:
                seen_labels.add(r["diagnosis"])
                showcase_ids += [r["trace_id"], r.get("resume_trace_id") or ""]
                # neighborhood of the target objects, for the graph view
                for oid in r["target"].values():
                    o = store.get(store.find_type(oid) or "", oid)
                    if not o:
                        continue
                    graph_nodes[oid] = {"id": oid, "type": o["_type"], "title": o["_title"]}
                    for nb in store.neighbors(oid)[:8]:
                        no = store.get(nb["type"], nb["id"])
                        if no:
                            graph_nodes[nb["id"]] = {"id": nb["id"], "type": nb["type"], "title": no["_title"]}
                            a, b = (oid, nb["id"]) if nb["direction"] == "out" else (nb["id"], oid)
                            graph_edges.append({"from": a, "to": b, "link": nb["link"]})
    # outbox worker traces link back to action runs; include the ones for showcase runs
    worker = []
    for f in pack.paths.traces.glob("*.jsonl"):
        spans = load_trace(f)
        if spans and spans[0]["name"] == "outbox.worker":
            worker.append(spans)
    traces = _traces_for(pack, [t for t in showcase_ids if t])

    audit = [dict(zip(("seq", "actor", "on_behalf_of", "action", "status", "approved_by", "hash"), r))
             for r in q("select seq, actor, on_behalf_of, action, status, approved_by, hash from audit_log "
                        "order by seq desc limit 40")]
    objects_by_type = {t: c for t, c in q("select type, count(*) from objects group by 1 order by 2 desc")}
    gt_path = pack.paths.ground_truth
    snapshot = {
        "pack": pack.name, "title": pack.title, "industry": pack.spec.get("industry"),
        "tagline": pack.spec.get("tagline"), "story": pack.spec.get("story"), "decision": pack.spec.get("decision"),
        "aws_mapping": pack.spec.get("aws_mapping", {}),
        "planted": json.loads(gt_path.read_text()).get("planted", []) if gt_path.exists() else [],
        "legacy": _legacy_samples(pack),
        "migrate": _load(pack, "migrate"), "reconcile": _load(pack, "reconcile"),
        "ontology": {"report": _load(pack, "ontology"), "schema": {
            "object_types": {k: {"owner": v.get("owner"), "description": v.get("description", ""),
                                 "sources": [s["system"] for s in v["sources"]]}
                             for k, v in pack.ontology["object_types"].items()},
            "created_types": pack.ontology.get("created_types", {}),
            "link_types": {k: {"from": v["from"], "to": v["to"], "description": v.get("description", "")}
                           for k, v in pack.ontology.get("link_types", {}).items()},
            "functions": {k: v.get("description", "") for k, v in pack.ontology.get("functions", {}).items()},
            "actions": {k: {"description": v.get("description", ""), "requires_approval": v.get("requires_approval", False),
                            "side_effect": (v.get("side_effect") or {}).get("system")}
                        for k, v in pack.ontology.get("actions", {}).items()}},
            "objects_by_type": objects_by_type, "merges": merges, "review_queue": review,
            "graph": {"nodes": list(graph_nodes.values()), "edges": graph_edges[:120]}},
        "policies": pack.policies.get("rules", []),
        "agents": agents, "traces": traces, "worker_traces": worker[:6],
        "audit": audit, "audit_chain_intact": store.verify_audit_chain(),
        "evals": _load(pack, "evals"),
    }
    store.con.close()
    path = out_dir / f"{pack.name}.json"
    path.write_text(json.dumps(snapshot, default=str))
    return path
