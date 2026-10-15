"""Discovery: what the estate did not know about itself. Sketch 01, live.

Four finding classes, every one derived from stores and reports the
engine already writes. The derivation NEVER reads ground truth; the test
reads ground truth to prove the derivation finds the planted facts
without being told they were planted. On an estate nobody sabotaged,
empty classes stay empty rather than inventing noise.
"""
from __future__ import annotations

import json
from typing import Any

from .ontology.store import OntologyStore
from .packs import Pack


def _identity(store: OntologyStore) -> list[dict[str, Any]]:
    out = []
    rows = store.con.execute(
        "select type, id, members, method, confidence from merge_log order by type, id"
    ).fetchall()
    routine = 0
    for type_, id_, members_json, method, confidence in rows:
        members = json.loads(members_json)
        systems = [m.split(":", 1)[0] for m in members]
        dup_same_system = len(systems) != len(set(systems))
        # A clean two-system match is resolution doing its job, not a
        # discovery. Findings are the surprising shapes: duplicates inside
        # one system, or identities fragmented across three or more records.
        if not dup_same_system and len(members) <= 2:
            routine += 1
            continue
        title_row = store.con.execute(
            "select title from objects where type = ? and id = ?", [type_, id_]).fetchone()
        title = title_row[0] if title_row else id_
        out.append({
            "class": "identity",
            "severity": "high" if dup_same_system else "review",
            "title": (f"{title} exists {len(members)} times"
                      + (", twice in one system" if dup_same_system else " across systems")),
            "method": method, "confidence": confidence,
            "evidence": {"type": type_, "id": id_, "members": members},
        })
    out.append({"class": "identity", "severity": "info",
                "title": f"{routine} identities resolved cleanly across two systems",
                "method": "merge_log", "evidence": {"routine_merges": routine}})
    for type_, a, b, score, reason, _ts in store.con.execute(
            "select type, a, b, score, reason, ts from review_queue order by ts").fetchall():
        out.append({"class": "identity", "severity": "review",
                    "title": f"{type_} {a} and {b} might be the same ({reason})",
                    "method": "review_queue", "confidence": score,
                    "evidence": {"type": type_, "a": a, "b": b}})
    return out


def _contradictions(pack: Pack) -> list[dict[str, Any]]:
    path = pack.paths.reports / "reconcile.json"
    if not path.exists():
        return []
    out = []
    for rule in json.loads(path.read_text()).get("rules", []):
        counts = rule.get("counts", {})
        if counts.get("unexplained", 0):
            out.append({"class": "contradiction", "severity": "high",
                        "title": f"{rule['name']}: {counts['unexplained']} unexplained differences",
                        "method": "reconcile", "evidence": {"rule": rule["name"],
                                                            "differences": rule.get("differences", [])}})
        elif counts.get("explained", 0):
            out.append({"class": "contradiction", "severity": "info",
                        "title": f"{rule['name']}: {counts['explained']} explained, 0 unexplained",
                        "method": "reconcile", "evidence": {"rule": rule["name"],
                                                            "differences": rule.get("differences", [])}})
    return out


def _orphans_and_dormant(pack: Pack) -> list[dict[str, Any]]:
    path = pack.paths.reports / "migrate.json"
    if not path.exists():
        return []
    report = json.loads(path.read_text())
    out = []
    for write in report.get("writes", []):
        for check in write.get("checks", []):
            if not check.get("passed"):
                sev = "high" if check.get("severity") == "block" else "review"
                out.append({"class": "orphan", "severity": sev,
                            "title": f"{write['table']}: {check['check']} failing "
                                     f"({check.get('failing_rows', '?')} rows)",
                            "method": f"quality:{check.get('kind')}",
                            "evidence": {"table": write["table"], "check": check}})
        if not write.get("published", True):
            out.append({"class": "orphan", "severity": "high",
                        "title": f"{write['table']}: load quarantined, never published",
                        "method": "wap", "evidence": {"table": write["table"],
                                                      "branch": write.get("branch")}})
    return out


def derive(pack: Pack) -> dict[str, Any]:
    db = pack.paths.ontology_db
    findings: list[dict[str, Any]] = []
    if db.exists():
        store = OntologyStore(db)
        try:
            findings += _identity(store)
        finally:
            store.con.close()
    findings += _contradictions(pack)
    findings += _orphans_and_dormant(pack)
    order = {"high": 0, "review": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["class"], f["title"]))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["class"]] = counts.get(f["class"], 0) + 1
    return {"findings": findings, "counts": counts,
            "note": "Derived from the stores and reports this workspace already keeps. "
                    "Empty classes stay empty; nothing here is invented."}
