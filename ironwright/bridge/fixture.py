"""A generated bridge workspace: 3 messy sources, a gold contract with 2 marts, and the truth.

Every planted problem is written to `_ground_truth.json` as the status the bridge must
report for that gold column, and the eval holds the report to it exactly: each planted
gap is reported, and nothing else is. The truth is written from what this file plants,
never from running the planner.

| Gold column | Planted | Expected |
|---|---|---|
| dim_policy.limit_amount | a third of the limits are the words NO LIMIT, so the file column reads as text | partial: type |
| dim_policy.policy_status | no source holds it. claims.claim_state is a decoy a fuzzy match would take | gap |
| dim_adjuster.adjuster_email | adjusters.email repeats one address | partial: key |
| dim_adjuster.team | adjusters.team is empty on every row | partial: values |
| fct_claim_payment.currency | no source holds it. claims.currency_note is a decoy | gap |
| fct_claim_payment.adjuster_region | adjusters.region holds it, and nothing joins claims to adjusters | partial: join |

Everything else is covered: by the same name, by an abbreviation (paid_amt), or by the
table's own prefix (adjusters.email for adjuster_email). Money comes as "$1,234.50", claim
policy numbers lose their leading zeros, and the policy file is pipe delimited.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import yaml

POLICIES = 40
CLAIMS = 120
ADJUSTERS = 6

CONTRACT: dict[str, Any] = {
    "kind": "gold-contract",
    "version": 1,
    "use_cases": [
        {"id": "claims-leakage", "audience": "claims ops leads",
         "question": "Which open claims are paid above policy limit, by adjuster, weekly?",
         "freshness": "daily by 06:00", "consumer": {"type": "bi", "target": "powerbi"}},
        {"id": "adjuster-load", "audience": "regional claims managers",
         "question": "How much does each adjuster pay out per region and team?",
         "freshness": "weekly", "consumer": {"type": "file", "target": "sftp drop"}},
    ],
    "dimensions": [
        {"name": "dim_policy", "grain": "one row per policy", "columns": [
            {"name": "policy_no", "type": "string", "key": True},
            {"name": "holder_name", "type": "string"},
            {"name": "limit_amount", "type": "decimal(14,2)"},
            {"name": "policy_status", "type": "string"}]},
        {"name": "dim_adjuster", "grain": "one row per adjuster", "columns": [
            {"name": "adjuster_email", "type": "string", "key": True},
            {"name": "region", "type": "string"},
            {"name": "team", "type": "string"}]},
    ],
    "facts": [
        {"name": "fct_claim_payment", "grain": "one row per payment", "columns": [
            {"name": "claim_id", "type": "string"},
            {"name": "policy_no", "type": "string"},
            {"name": "adjuster_email", "type": "string"},
            {"name": "paid_amount", "type": "decimal(14,2)"},
            {"name": "paid_on", "type": "date"},
            {"name": "currency", "type": "string"},
            {"name": "adjuster_region", "type": "string"}]},
    ],
    "marts": [
        {"name": "mart_claims_leakage", "from": ["fct_claim_payment", "dim_policy"], "use_case": "claims-leakage",
         "rules": ["paid_amount > limit_amount"],
         "access": {"roles": ["member"], "masking": {"adjuster_email": "hash"}}},
        {"name": "mart_adjuster_load", "from": ["fct_claim_payment", "dim_adjuster"], "use_case": "adjuster-load",
         "access": {"roles": ["admin"]}},
    ],
}

# What this file plants, by gold column. Anything not named here is covered.
PLANTED: dict[str, dict[str, str]] = {
    "dim_policy.limit_amount": {"status": "partial", "missing": "type"},
    "dim_policy.policy_status": {"status": "gap"},
    "dim_adjuster.adjuster_email": {"status": "partial", "missing": "key"},
    "dim_adjuster.team": {"status": "partial", "missing": "values"},
    "fct_claim_payment.currency": {"status": "gap"},
    "fct_claim_payment.adjuster_region": {"status": "partial", "missing": "join"},
}
FILES = ("claims.csv", "policies.txt", "adjusters.tsv")


def _truth() -> dict[str, Any]:
    tables = {t["name"]: t for t in CONTRACT["dimensions"] + CONTRACT["facts"]}
    gold = {f"{t}.{c['name']}": PLANTED.get(f"{t}.{c['name']}", {"status": "covered"})
            for t, d in tables.items() for c in d["columns"]}
    marts: dict[str, dict[str, str]] = {}
    for m in CONTRACT["marts"]:
        seen: set[str] = set()
        for t in m["from"]:
            for c in tables[t]["columns"]:
                if c["name"] not in seen:
                    seen.add(c["name"])
                    marts[f"{m['name']}.{c['name']}"] = gold[f"{t}.{c['name']}"]
    return {"gold": gold, "marts": marts,
            "decoys": {"dim_policy.policy_status": "claims.claim_state", "fct_claim_payment.currency": "claims.currency_note"}}


def _money(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def generate(out: Path, seed: int = 7) -> dict[str, Any]:
    """Writes the three files, gold.yaml and _ground_truth.json into out. Same seed, same bytes."""
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    regions = ["north", "south", "east", "west"]
    adjusters = [f"adj{i}@claims.example" for i in range(1, ADJUSTERS + 1)]
    rows = ["email\tregion\tteam\tdesk_phone"]
    for i, a in enumerate(adjusters):
        rows.append(f"{a}\t{regions[i % 4]}\t\t555-01{i:02d}")
    rows.append(f"{adjusters[0]}\t{regions[1]}\t\t555-0199")  # planted: the address repeats
    (out / "adjusters.tsv").write_text("\n".join(rows) + "\n")

    rows = ["policy_no|holder_name|limit_amount|issued"]
    limits = {}
    for i in range(1, POLICIES + 1):
        cents = rng.randrange(50_000, 2_000_000) * 100
        limits[i] = cents
        limit = "NO LIMIT" if i % 3 == 0 else f"{cents / 100:.2f}"  # planted: words among numbers
        rows.append(f"{i:06d}|Holder {i:03d}|{limit}|2024-{1 + i % 12:02d}-{1 + i % 28:02d}")
    (out / "policies.txt").write_text("\n".join(rows) + "\n")

    rows = ["claim_id,policy_no,adjuster_email,paid_amt,paid_on,claim_state,currency_note"]
    for i in range(1, CLAIMS + 1):
        pol = rng.randrange(1, POLICIES + 1)
        cents = rng.randrange(1_000, 3_000_000)
        state = rng.choice(["open", "closed", "reopened"])
        note = rng.choice(["paid by check", "wire", "ach"])
        rows.append(f"C{i:05d},{pol},{rng.choice(adjusters)},\"{_money(cents)}\","
                    f"2025-{1 + i % 12:02d}-{1 + i % 28:02d},{state},{note}")
    (out / "claims.csv").write_text("\n".join(rows) + "\n")

    (out / "gold.yaml").write_text(yaml.safe_dump(CONTRACT, sort_keys=False))
    truth = _truth()
    (out / "_ground_truth.json").write_text(json.dumps(truth, indent=1, sort_keys=True))
    return truth
