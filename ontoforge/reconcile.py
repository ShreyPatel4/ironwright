"""Reconciliation: does the new platform match what the legacy system reported?

The bar for a migration is not "is the new number right" but "does it match the
old one, and is every difference explained". Each rule compares a legacy query
(over raw legacy sources or the old system's own report files) with a lake query,
joined on a business key. Differences are matched, within tolerance, explained
by a documented legacy quirk, or unexplained. Any unexplained row blocks cutover.
"""
from __future__ import annotations

from typing import Any

from .expr import evaluate
from .lake import Lakehouse
from .legacy import read_source
from .packs import Pack
from .tracing import Tracer


def reconcile(pack: Pack, tracer: Tracer) -> dict[str, Any]:
    lake = Lakehouse(pack)
    con = lake.connect()
    for src in pack.spec["sources"]:
        con.register(f"legacy_{src['name']}", read_source(src, pack.paths.legacy))
    for rep in pack.spec.get("legacy_reports", []):
        con.register(f"legacy_{rep['name']}", read_source(rep, pack.paths.legacy))

    out = []
    with tracer.span("reconcile", pack=pack.name):
        for rule in pack.spec.get("reconcile", []):
            with tracer.span("reconcile.rule", rule=rule["name"]) as sp:
                key = rule["key"]
                meas = rule["measures"]  # {measure: tolerance}
                legacy = con.execute(rule["legacy_sql"]).fetchdf()
                new = con.execute(rule["lake_sql"]).fetchdf()
                lk = {tuple(str(r[k]) for k in key): r for _, r in legacy.iterrows()}
                nk = {tuple(str(r[k]) for k in key): r for _, r in new.iterrows()}
                rows, counts = [], {"match": 0, "within_tolerance": 0, "explained": 0, "unexplained": 0}
                for k in sorted(set(lk) | set(nk)):
                    a, b = lk.get(k), nk.get(k)
                    rec: dict[str, Any] = {"key": dict(zip(key, k))}
                    status = "match"
                    for m, tol in meas.items():
                        av = float(a[m]) if a is not None and a[m] is not None else None
                        bv = float(b[m]) if b is not None and b[m] is not None else None
                        rec[f"legacy_{m}"], rec[f"new_{m}"] = av, bv
                        if av is None or bv is None:
                            status = "unexplained"
                        elif abs(av - bv) > 1e-9:
                            if abs(av - bv) <= float(tol):
                                status = "within_tolerance" if status == "match" else status
                            else:
                                status = "unexplained"
                    if status == "unexplained":
                        env = {"legacy": {m: rec[f"legacy_{m}"] for m in meas},
                               "new": {m: rec[f"new_{m}"] for m in meas}, "key": rec["key"]}
                        for kd in rule.get("known_differences", []):
                            try:
                                if evaluate(kd["when"], env):
                                    status, rec["reason"] = "explained", kd["reason"]
                                    break
                            except Exception:
                                continue
                    rec["status"] = status
                    counts[status] += 1
                    if status != "match":
                        rows.append(rec)
                passed = counts["unexplained"] == 0
                sp.set(passed=passed, **counts)
                out.append({"name": rule["name"], "key": key, "measures": list(meas), "passed": passed,
                            "counts": counts, "differences": rows[:50],
                            "known_differences": rule.get("known_differences", [])})
    return {"rules": out, "cutover_ready": all(r["passed"] for r in out)}
