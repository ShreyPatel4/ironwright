"""The evaluator and the compliance check. Deterministic gates decide; nothing here asks a model.

| Gate | Passes when |
|---|---|
| tests | every test query returns no rows |
| contract | every contract column exists with a matching type |
| dq | the row-weighted DQ pass rate meets the threshold |
| lineage | coverage toward the target columns is at least what it was |
| cost | output rows over sampled input rows stays inside the budget |
| compliance | a purpose is declared, and every PII column is masked, proved on the rows, unless the purpose allows its class |

Each gate gets an evidence id from the hash of its own result, so the same change on the same
sample scores the same and cites the same ids twice.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import duckdb
import pyarrow as pa

from ..tracing import REDACT_KEYS
from .gate import Change, Staged

DQ_THRESHOLD = 0.95
COST_BUDGET = 1.5

# Name rules, most specific first. A column a profile tags itself wins over these.
_PII_NAMES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"(^|_)e_?mail(_|$)|email")),
    ("phone", re.compile(r"(^|_)(phone|mobile|cell|tel|telephone|fax)(_|$)")),
    ("national_id", re.compile(r"(^|_)(ssn|sin|nino|national_id|tax_id|tin|passport)(_|$)")),
    ("birth_date", re.compile(r"(^|_)(dob|birth|birthdate|birth_date|date_of_birth)(_|$)")),
    ("account", re.compile(r"(account_number|card_number|iban|routing_number|acct_no)")),
    ("address", re.compile(r"(^|_)(address|street|addr|postcode|postal_code|zip|zipcode)(_|$)")),
    ("person_name", re.compile(r"^(holder|insured|claimant|policyholder|patient|beneficiary)$|"
                               r"(^|_)(first|last|full|middle|given|sur|customer|insured|holder|claimant|"
                               r"contact|person|patient|member|employee|owner)_?name$")),
]
_EMAIL_VALUE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)


def _tag(name: str) -> str | None:
    low = name.lower()
    if low in REDACT_KEYS:
        return {"ssn": "national_id", "dob": "birth_date", "email": "email", "phone": "phone"}.get(low, "account")
    return next((cls for cls, rx in _PII_NAMES if rx.search(low)), None)


def pii_tags(source: dict[str, Any], sensitive: list[str] | None = None) -> dict[str, str]:
    """{column: class}. `source` is either bronze tables by name, or a profile from the
    profiler ({files: [{columns: [...]}]}). A profile column that carries its own `pii` tag
    keeps it. An ontology's `sensitive` list tags its names as well."""
    tags: dict[str, str] = {}
    if "files" in source and isinstance(source.get("files"), list):
        for f in source["files"]:
            for c in f.get("columns", []):
                cls = c.get("pii") or _tag(c["name"])
                if cls:
                    tags[c["name"]] = cls
    else:
        for t in source.values():
            for name in t.column_names:
                cls = _tag(name)
                if cls is None and pa.types.is_string(t.schema.field(name).type):
                    vals = [v for v in t.column(name).slice(0, 50).to_pylist() if v]
                    if vals and sum(bool(_EMAIL_VALUE.match(str(v).strip())) for v in vals) / len(vals) >= 0.8:
                        cls = "email"
                if cls:
                    tags[name] = cls
    for name in sensitive or []:
        tags.setdefault(name, "sensitive")
    return tags


def _evidence(gate: str, result: dict[str, Any]) -> str:
    return f"ev:{gate}:" + hashlib.sha1(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _family(t: str) -> str:
    t = t.lower().strip()
    if t.startswith(("decimal", "numeric")):
        return "decimal"
    for fam, names in (("string", ("string", "varchar", "text", "char")),
                       ("integer", ("integer", "int", "bigint", "smallint", "tinyint", "hugeint", "int64", "int32")),
                       ("double", ("double", "float", "real", "float64")),
                       ("boolean", ("boolean", "bool")),
                       ("timestamp", ("timestamp",)), ("date", ("date",))):
        if t in names or t.startswith(names[0]) and fam == "timestamp":
            return fam
    return t


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def _tests(change: Change, staged: Staged) -> dict[str, Any]:
    failed = []
    for t in change.tests:
        try:
            rows = staged.con.execute(f"select count(*) from ({t['sql']})").fetchone()[0]
        except duckdb.Error:
            rows = -1
        if rows != 0:
            failed.append(t["name"])
    return {"passed": not failed and not staged.errors, "ran": len(change.tests), "failed": failed,
            "model_errors": sorted(staged.errors)}


def _contract(change: Change, staged: Staged) -> dict[str, Any]:
    missing, mismatched = [], []
    for table, cols in sorted(change.contract.items()):
        have = {c["name"]: c["type"] for c in staged.tables.get(table, {}).get("columns", [])}
        for c in cols:
            ref = f"{table}.{c['name']}"
            if c["name"] not in have:
                missing.append(ref)
            elif c.get("type") and _family(c["type"]) != _family(have[c["name"]]):
                mismatched.append(ref)
    total = sum(len(v) for v in change.contract.values())
    held = total - len(missing) - len(mismatched)
    return {"passed": not missing and not mismatched, "fit": round(held / total, 4) if total else 1.0,
            "missing": missing, "mismatched": mismatched}


def _dq(change: Change, staged: Staged, threshold: float) -> dict[str, Any]:
    checks, good, total = [], 0, 0
    for rule in change.dq:
        table, col, kind = rule["table"], rule["column"], rule["check"]
        meta = staged.tables.get(table)
        if meta is None or col not in {c["name"] for c in meta["columns"]}:
            checks.append({**rule, "passed": False, "rows": 0, "good": 0})
            total += 1
            continue
        rows = meta["rows"]
        q = f'"{col}"'
        if kind == "not_null":
            ok = _count(staged.con, f'select count({q}) from "{table}"')
        elif kind == "unique":
            ok = rows - (_count(staged.con, f'select count({q}) from "{table}"')
                         - _count(staged.con, f'select count(distinct {q}) from "{table}"'))
        else:
            ok = 0
        checks.append({**rule, "passed": ok == rows, "rows": rows, "good": ok})
        good += ok
        total += rows
    rate = round(good / total, 4) if total else 1.0
    return {"passed": rate >= threshold, "rate": rate, "threshold": threshold, "checks": checks}


def default_coverage(change: Change, staged: Staged) -> float:
    """Share of the contract's target columns the staged models actually produce. B1's
    lineage report replaces this through the `coverage` argument."""
    want = [(t, c["name"]) for t, cols in change.contract.items() for c in cols]
    if not want:
        return 1.0
    have = {(t, c["name"]) for t, m in staged.tables.items() for c in m["columns"]}
    return round(sum(w in have for w in want) / len(want), 4)


def _cost(staged: Staged, budget: float) -> dict[str, Any]:
    rows_in = sum(staged.inputs.values())
    rows_out = sum(m["rows"] for m in staged.tables.values())
    ratio = round(rows_out / rows_in, 4) if rows_in else 0.0
    return {"passed": ratio <= budget, "ratio": ratio, "budget": budget, "rows_in": rows_in, "rows_out": rows_out}


def _compliance(change: Change, staged: Staged, pii: dict[str, str], policy: dict[str, Any]) -> dict[str, Any]:
    purpose = change.purpose.strip() or None
    allowed = set((policy.get("purposes") or {}).get(purpose or "", []))
    raw_values = set(staged.raw_pii(pii))
    found, masked, unmasked, allowed_through = [], [], [], []
    for table in sorted(staged.tables):
        # By name, or by carrying raw PII values under another name.
        for name, cls in sorted(staged.pii_columns(table, pii).items()):
            ref = f"{table}.{name}"
            found.append({"column": ref, "class": cls})
            declared = (change.masking.get(table) or {}).get(name)
            vals = {str(r[name]) for r in staged.rows(table) if r.get(name) is not None}
            if declared and not (vals & raw_values):
                masked.append(ref)
            elif cls in allowed:
                allowed_through.append(ref)
            else:
                unmasked.append(ref)
    return {"passed": purpose is not None and not unmasked, "purpose": purpose, "pii": found,
            "masked": masked, "unmasked": unmasked, "allowed_by_purpose": allowed_through}


def evaluate(change: Change, staged: Staged, pii: dict[str, str], *, dq_threshold: float = DQ_THRESHOLD,
             cost_budget: float = COST_BUDGET, baseline_coverage: float = 0.0,
             coverage: Callable[[Change, Staged], float] | None = None,
             compliance_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    after = float((coverage or default_coverage)(change, staged))
    gates = {
        "tests": _tests(change, staged),
        "contract": _contract(change, staged),
        "dq": _dq(change, staged, dq_threshold),
        "lineage": {"passed": after >= baseline_coverage, "before": baseline_coverage, "after": after},
        "cost": _cost(staged, cost_budget),
        "compliance": _compliance(change, staged, pii, compliance_policy or {}),
    }
    for name, g in gates.items():
        g["evidence"] = _evidence(name, g)
    return {"passed": all(g["passed"] for g in gates.values()), "gates": gates,
            "evidence": [g["evidence"] for g in gates.values()], "content_hash": change.content_hash()}
