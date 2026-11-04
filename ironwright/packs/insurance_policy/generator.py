"""Quillback Casualty: a fictional mid-size property and casualty carrier.

Legacy systems:
  POLADMIN.dat            policy transactions (fixed width): new, endorse, cancel, renew
  COVADMIN.dat            coverage transactions (fixed width), one contract per peril
  BILLING.csv             billing events, the only place a lapse is recorded
  claims/CLAIMS_*.jsonl   claims feed, policy as "QB-123456"
  AGENCY_BOOK.csv         each agency's spreadsheet, same person spelled its own way
  reports/EXPOSURE.dat    the policy admin report: total limit per coverage

Valid time is wall clock at the insured's location, because a policy form starts at 12:01 AM
local. Transaction time is when the row was booked.

Planted, each with a machine readable entry in _ground_truth.json:
  overlapping_coverage       two contracts on one policy and peril cover the same days
  endorsement_out_of_order   a lower sequence endorsement booked after a higher one
  lapsed_policy_paid_claim   billing lapsed the policy, policy admin never heard, a claim got paid
  duplicate_insured          one person on two policies through two agencies
  limit_cents_vs_dollars     a converted book stores limits in cents, with no flag saying so
  retro_dated_cancellation   a cancellation booked weeks after its effective date
  orphan_coverage_rows       coverage rows naming a policy that does not exist
  wrong_effective_timezone   effective times labeled UTC on policies in Colorado
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

AS_OF = date(2026, 9, 30)
STATES = {"CO": "MT", "TX": "CT", "OH": "ET"}
AGENCIES = {"AG01": "Agency One", "AG02": "Agency Two", "AG03": "Agency Three", "AG04": "Agency Four"}
PERILS = ["HAIL", "WIND", "WATER", "THEFT"]          # FIRE is on every policy
LIMITS = [100000, 250000, 500000, 1000000]           # what a policy normally starts with
DEDUCTIBLES = [500, 1000, 2500]
FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew", "Sam", "Robin"]
LAST = ["Reed", "Fox", "Lane", "Hart", "Cole", "Park", "Shaw", "Diaz", "Holt", "Ward", "Bell", "Cruz", "Nash", "Vale"]
NOON = timedelta(hours=12)


def _start(d: date) -> datetime:
    """Policy forms start at 12:01 AM."""
    return datetime.combine(d, time(0, 1))


def _stamp(t: datetime) -> str:
    return t.strftime("%Y%m%d%H%M")


def generate(out: Path, seed: int = 7) -> dict:
    rng = random.Random(seed)
    (out / "claims").mkdir(parents=True, exist_ok=True)
    (out / "reports").mkdir(parents=True, exist_ok=True)

    seen_ids = set()

    def person(name: tuple[str, str] | None = None) -> dict:
        while True:
            dob = date(rng.randint(1945, 2000), rng.randint(1, 12), rng.randint(1, 28))
            last4 = f"{rng.randint(0, 9999):04d}"
            if (dob, last4) not in seen_ids:
                seen_ids.add((dob, last4))
                break
        first, last = name or (rng.choice(FIRST), rng.choice(LAST))
        return {"first": first, "last": last, "dob": dob, "last4": last4}

    policies: list[dict] = []
    policy_rows: list[dict] = []
    coverage_rows: list[dict] = []
    billing_rows: list[dict] = []
    agency_rows: list[dict] = []
    claims: list[dict] = []
    clusters: list[list[str]] = []
    labels: dict[str, str] = {}
    counters = {"txn": 0, "cov": 0, "claim": 0}

    def new_id(kind: str, prefix: str, width: int) -> str:
        counters[kind] += 1
        return f"{prefix}{counters[kind]:0{width}d}"

    def policy_txn(p: dict, seq: int, kind: str, frm: datetime, to: datetime, booked: datetime, status: str = "A") -> None:
        policy_rows.append({"txn_id": new_id("txn", "T", 9), "policy_nbr": p["nbr"], "seq": seq, "txn_type": kind,
                            "state": p["state"], "eff_tz": p["tz_label"], "valid_from": frm, "valid_to": to,
                            "booked_ts": booked, "status": status, "insured_id": p["insured_id"],
                            "insured_name": p["pas_name"], "insured_dob": p["who"]["dob"], "insured_last4": p["who"]["last4"]})

    def coverage_txn(p_nbr: str, tz: str, cov_id: str, seq: int, kind: str, code: str, limit: int, deductible: int,
                     frm: datetime, to: datetime, booked: datetime) -> None:
        coverage_rows.append({"cov_id": cov_id, "policy_nbr": p_nbr, "seq": seq, "txn_type": kind, "coverage_code": code,
                              "limit_amt": limit, "deductible": deductible, "eff_tz": tz, "valid_from": frm,
                              "valid_to": to, "booked_ts": booked})

    def add_coverage(p: dict, code: str, limit: int | None = None, frm: datetime | None = None,
                     to: datetime | None = None, booked: datetime | None = None) -> dict:
        cov = {"id": new_id("cov", "C", 7), "code": code, "limit": limit or rng.choice(LIMITS),
               "deductible": rng.choice(DEDUCTIBLES), "from": frm or p["from"], "to": to or p["to"]}
        coverage_txn(p["nbr"], p["tz_label"], cov["id"], 1, "NEW", code, cov["limit"], cov["deductible"], cov["from"],
                     cov["to"], booked or p["booked"])
        p["coverages"].setdefault(code, []).append(cov)
        return cov

    def add_policy(eff: date | None = None, agency: str | None = None, who: dict | None = None, state: str | None = None,
                   tz_label: str | None = None, codes: list[str] | None = None) -> dict:
        i = len(policies)
        who = who or person()
        state = state or rng.choice(list(STATES))
        agency = agency or rng.choice(list(AGENCIES))
        eff = eff or AS_OF - timedelta(days=rng.randint(60, 320))
        p = {"nbr": f"{100000 + i * 13:010d}", "who": who, "state": state, "agency": agency,
             "agent": f"{agency}-A{rng.randint(1, 3)}", "insured_id": f"PI{10000 + i:06d}",
             "client_key": f"{agency}-{20000 + i * 3}", "tz_label": tz_label or STATES[state], "from": _start(eff),
             "to": _start(eff + timedelta(days=365)), "booked": _start(eff) - timedelta(days=rng.randint(2, 12)) + NOON,
             "pas_name": f"{who['first']} {who['last']}".upper(), "coverages": {}}
        policies.append(p)
        policy_txn(p, 1, "NEW", p["from"], p["to"], p["booked"])
        for code in codes or ["FIRE", *rng.sample(PERILS, rng.randint(1, 3))]:
            add_coverage(p, code)
        for k in range(2):
            billing_rows.append({"invoice_id": f"INV{10000 + len(billing_rows)}", "policy_nbr": p["nbr"], "event_type": "INVOICE_PAID",
                                 "event_date": (p["from"].date() + timedelta(days=30 * k)).isoformat(),
                                 "amount": f"{rng.uniform(80, 420):.2f}"})
        agency_rows.append({"agency_code": agency, "agent_id": p["agent"], "agent_name": f"Agent {p['agent'][-2:]} of {AGENCIES[agency]}",
                            "client_key": p["client_key"], "client_name": f"{who['last']}, {who['first']}",
                            "dob": f"{who['dob'].month}/{who['dob'].day}/{who['dob'].year}", "gov_id": f"***-**-{who['last4']}",
                            "policy_nbr": p["nbr"]})
        return p

    def members(p: dict) -> list[str]:
        return [f"pas:{p['insured_id']}", f"agency:{p['client_key']}"]

    def billing_event(p: dict, kind: str, day: date) -> None:
        billing_rows.append({"invoice_id": f"INV{10000 + len(billing_rows)}", "policy_nbr": p["nbr"], "event_type": kind,
                             "event_date": day.isoformat(), "amount": ""})

    def claim(p: dict, peril: str, loss: datetime, est: float, label: str, paid: float = 0.0) -> str:
        cid = f"CL-26-{10000 + new_id_int('claim') * 7}"
        reported = min(_start(AS_OF), loss + timedelta(days=rng.randint(1, 4)))
        claims.append({"claim_id": cid, "policy_ref": f"QB-{int(p['nbr'])}", "loss_ts": loss.isoformat(), "peril": peril,
                       "est_amount": f"{est:.2f}", "paid_amount": f"{paid:.2f}", "reported_ts": reported.isoformat()})
        labels[f"claim:{cid}"] = label
        return cid

    def new_id_int(kind: str) -> int:
        counters[kind] += 1
        return counters[kind]

    def mid_term(p: dict, day: int, hour: int = 14) -> datetime:
        return p["from"] + timedelta(days=day, hours=hour)

    # Planted policies start early enough for every mid-term event to land before AS_OF.
    old = AS_OF - timedelta(days=320)

    # ---------- overlapping coverage: a second HAIL contract booked on top of the first ----------
    overlap_expected = []
    overlap_claim = ""
    for k in range(2):
        p = add_policy(eff=old, codes=["FIRE", "HAIL"])
        first = p["coverages"]["HAIL"][0]
        frm = p["from"] + timedelta(days=100)
        second = add_coverage(p, "HAIL", frm=frm, booked=frm + NOON)
        overlap_expected.append("|".join(sorted([first["id"], second["id"]])))
        if k == 0:
            overlap_claim = claim(p, "HAIL", mid_term(p, 130), 9000, "review_overlap")

    # ---------- endorsement out of order: seq 3 books first, seq 2 turns up five days later ----------
    p = add_policy(eff=old, codes=["FIRE", "WIND"])
    cov = p["coverages"]["FIRE"][0]
    # limits picked here so a stale reading is visible, not from the random pool
    coverage_rows[:] = [r for r in coverage_rows if r["cov_id"] != cov["id"]]
    coverage_txn(p["nbr"], p["tz_label"], cov["id"], 1, "NEW", "FIRE", 100000, cov["deductible"], p["from"], p["to"], p["booked"])
    seq3 = p["from"] + timedelta(days=90)
    seq2 = p["from"] + timedelta(days=60)
    coverage_txn(p["nbr"], p["tz_label"], cov["id"], 3, "END", "FIRE", 300000, cov["deductible"], seq3, p["to"], seq3 + NOON)
    coverage_txn(p["nbr"], p["tz_label"], cov["id"], 2, "END", "FIRE", 200000, cov["deductible"], seq2, p["to"], seq3 + timedelta(days=5) + NOON)
    out_of_order = {"expected": [cov["id"]], "right_limit": 300000, "naive_limit": 200000,
                    "gap_ts": mid_term(p, 70).isoformat(),
                    "claim": claim(p, "FIRE", mid_term(p, 150), 250000, "covered")}

    # ---------- lapsed in billing, never in policy admin, claim paid anyway ----------
    lapse_expected = []
    for k in range(2):
        p = add_policy(eff=old, codes=["FIRE", "THEFT"])
        billing_event(p, "LAPSE", (p["from"] + timedelta(days=200)).date())
        lapse_expected.append(claim(p, "THEFT", mid_term(p, 230 + k), 6000, "review_lapsed", paid=5000))
    p = add_policy(eff=old, codes=["FIRE", "THEFT"])   # lapsed and the claim is still open: review, but not a paid defect
    billing_event(p, "LAPSE", (p["from"] + timedelta(days=200)).date())
    claim(p, "THEFT", mid_term(p, 240), 4000, "review_lapsed")
    p = add_policy(eff=old, codes=["FIRE", "THEFT"])   # decoy: reinstated before the loss
    billing_event(p, "LAPSE", (p["from"] + timedelta(days=100)).date())
    billing_event(p, "REINSTATE", (p["from"] + timedelta(days=120)).date())
    reinstated_claim = claim(p, "THEFT", mid_term(p, 150), 5000, "covered", paid=4000)

    # ---------- duplicate insured: one person, two policies, two agencies ----------
    dup_expected, dup_clusters = [], []
    for agencies in (("AG01", "AG03"), ("AG02", "AG04"), ("AG01", "AG04")):
        who = person()
        a, b = add_policy(who=who, agency=agencies[0]), add_policy(who=who, agency=agencies[1])
        dup_expected.append("|".join(sorted([a["client_key"], b["client_key"]])))
        dup_clusters.append(sorted(members(a) + members(b)))
    who = person()   # decoy for the register: same person twice inside one agency merges, but is not across agencies
    a, b = add_policy(who=who, agency="AG02"), add_policy(who=who, agency="AG02")
    dup_clusters.append(sorted(members(a) + members(b)))
    resolved = {m for c in dup_clusters for m in c}

    twin_name = ("Morgan", "Reed")     # decoy: same name, different people
    twin_a, twin_b = add_policy(who=person(twin_name), agency="AG01"), add_policy(who=person(twin_name), agency="AG03")
    name_decoys = [[f"agency:{twin_a['client_key']}", f"agency:{twin_b['client_key']}"]]

    # ---------- limit stored in cents ----------
    cents_expected, cents_claim = [], ""
    for k in range(3):
        p = add_policy(eff=old, codes=["FIRE", "WATER"])
        cov = p["coverages"]["WATER"][0]
        cov["limit"] = 250000
        for r in coverage_rows:
            if r["cov_id"] == cov["id"]:
                r["limit_amt"] = 250000 * 100
        cents_expected.append(cov["id"])
        if k == 0:
            cents_claim = claim(p, "WATER", mid_term(p, 140), 300000, "review_over_limit")

    # ---------- retro-dated cancellation: effective day 150, booked day 190 ----------
    retro_expected, retro = [], {}
    for k in range(2):
        p = add_policy(eff=old, codes=["FIRE", "WIND"])
        eff_cancel = p["from"] + timedelta(days=150)
        booked = eff_cancel + timedelta(days=40, hours=10)
        policy_txn(p, 2, "CAN", eff_cancel, p["to"], booked, status="C")
        retro_expected.append(p["nbr"])
        if k == 0:
            loss = mid_term(p, 170)
            retro = {"claim": claim(p, "WIND", loss, 7000, "review_retro_cancelled"), "loss_ts": loss.isoformat(),
                     "booked_ts": booked.isoformat()}
    p = add_policy(eff=old, codes=["FIRE", "WIND"])   # decoy: cancelled the same day it took effect
    eff_cancel = p["from"] + timedelta(days=150)
    policy_txn(p, 2, "CAN", eff_cancel, p["to"], eff_cancel + timedelta(hours=9), status="C")
    same_day = p["nbr"]
    claim(p, "WIND", mid_term(p, 170), 3000, "review_cancelled")

    # ---------- orphan coverage rows ----------
    orphan_expected = []
    for k in range(3):
        cid = new_id("cov", "C", 7)
        ghost = f"{990000 + k:010d}"
        frm = _start(old)
        coverage_txn(ghost, "MT", cid, 1, "NEW", "HAIL", 250000, 1000, frm, frm + timedelta(days=365), frm + NOON)
        orphan_expected.append(cid)

    # ---------- effective times labeled UTC on Colorado policies ----------
    tz_expected, tz_claim, tz_rows = [], "", 0
    for k in range(3):
        p = add_policy(eff=AS_OF - timedelta(days=100 + k), state="CO", tz_label="UTC", codes=["FIRE", "HAIL"])
        tz_expected.append(p["nbr"])
        tz_rows += len(p["coverages"])
        if k == 0:
            tz_claim = claim(p, "HAIL", p["from"] - timedelta(hours=4), 8000, "review_no_coverage")

    # ---------- the ordinary book ----------
    book = [add_policy() for _ in range(110)]
    # decoy: renewals where one term ends the minute the next begins
    renewal_decoys = []
    for _ in range(5):
        p = add_policy(eff=AS_OF - timedelta(days=500), codes=["FIRE", "HAIL"])
        new_from, new_to = p["to"], p["to"] + timedelta(days=365)
        policy_txn(p, 2, "REN", new_from, new_to, new_from - timedelta(days=9), "A")
        for code in ("FIRE", "HAIL"):
            renewal_decoys.append(add_coverage(p, code, frm=new_from, to=new_to, booked=new_from - timedelta(days=9))["id"])
        claim(p, "HAIL", new_from + timedelta(days=rng.randint(10, 80), hours=9), rng.uniform(1500, 9000), "covered")
    # ordinary endorsements, booked in order
    for p in rng.sample(book, 18):
        cov = p["coverages"]["FIRE"][0]
        frm = p["from"] + timedelta(days=rng.randint(10, 40))
        new_limit = min(1000000, cov["limit"] * 2)
        cov["limit"] = new_limit
        coverage_txn(p["nbr"], p["tz_label"], cov["id"], 2, "END", "FIRE", new_limit, cov["deductible"], frm, p["to"], frm + NOON)
    # ordinary claims, covered
    rng.shuffle(book)
    for p in book[:36]:
        code = rng.choice(list(p["coverages"]))
        latest = (AS_OF - p["from"].date()).days - 3
        loss = mid_term(p, rng.randint(45, max(46, latest)), hour=rng.randint(6, 22))
        amount = rng.uniform(1200, 45000)
        claim(p, code, loss, amount, "covered", paid=round(amount * 0.8, 2) if rng.random() < 0.4 else 0.0)
    # a peril the policy never bought
    for p in book[36:40]:
        missing = next(c for c in ["THEFT", "WATER", "WIND", "HAIL"] if c not in p["coverages"]) if len(p["coverages"]) < 5 else None
        if missing:
            claim(p, missing, mid_term(p, 50), rng.uniform(2000, 15000), "review_no_coverage")

    # ---------- write ----------
    with open(out / "POLADMIN.dat", "w") as fh:
        for r in policy_rows:
            fh.write(f"{r['txn_id']:<10}{r['policy_nbr']:<10}{r['seq']:03d}{r['txn_type']:<3}{r['state']:<2}{r['eff_tz']:<3}"
                     f"{_stamp(r['valid_from'])}{_stamp(r['valid_to'])}{_stamp(r['booked_ts'])}{r['status']}"
                     f"{r['insured_id']:<8}{r['insured_name'][:30]:<30}{r['insured_dob']:%Y%m%d}{r['insured_last4']}\n")
    with open(out / "COVADMIN.dat", "w") as fh:
        for r in coverage_rows:
            fh.write(f"{r['cov_id']:<10}{r['policy_nbr']:<10}{r['seq']:03d}{r['txn_type']:<3}{r['coverage_code']:<6}"
                     f"{r['limit_amt']:012d}{r['deductible']:07d}{r['eff_tz']:<3}"
                     f"{_stamp(r['valid_from'])}{_stamp(r['valid_to'])}{_stamp(r['booked_ts'])}\n")
    with open(out / "BILLING.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(billing_rows[0]))
        w.writeheader()
        w.writerows(billing_rows)
    with open(out / "AGENCY_BOOK.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(agency_rows[0]))
        w.writeheader()
        w.writerows(agency_rows)
    by_month: dict[str, list[dict]] = {}
    for c in claims:
        by_month.setdefault(c["reported_ts"][:7].replace("-", ""), []).append(c)
    for month, rows in sorted(by_month.items()):
        (out / "claims" / f"CLAIMS_{month}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    # the legacy report takes the highest sequence row of each contract at face value, cents included
    known = {p["nbr"] for p in policies}
    top: dict[str, dict] = {}
    for r in coverage_rows:
        if r["policy_nbr"] in known and (r["cov_id"] not in top or r["seq"] > top[r["cov_id"]]["seq"]):
            top[r["cov_id"]] = r
    exposure: dict[str, int] = {}
    for r in top.values():
        exposure[r["coverage_code"]] = exposure.get(r["coverage_code"], 0) + r["limit_amt"]
    with open(out / "reports" / "EXPOSURE.dat", "w") as fh:
        for code, total in sorted(exposure.items()):
            fh.write(f"{code:<6}{total:016d}\n")

    for p in policies:
        if members(p)[0] not in resolved:
            clusters.append(sorted(members(p)))
    clusters += dup_clusters

    checks = "silver.coverages_current"
    planted = [
        {"kind": "overlapping_coverage", "what": "2 policies carry a second HAIL contract on top of the first",
         "expected": overlap_expected, "claim": overlap_claim,
         "check": f"{checks}:no_overlapping_coverage", "failing_rows": 2 * len(overlap_expected)},
        {"kind": "endorsement_out_of_order", "what": "an endorsement with sequence 2 was booked five days after sequence 3",
         "check": f"{checks}:endorsements_in_order", "failing_rows": 1, **out_of_order},
        {"kind": "lapsed_policy_paid_claim", "what": "billing lapsed 2 policies, policy admin never heard, and a claim was paid on each",
         "expected": lapse_expected, "check": "gold.coverage_verification:no_paid_claim_on_lapsed_policy",
         "failing_rows": len(lapse_expected)},
        {"kind": "duplicate_insured", "what": "3 people hold policies through two different agencies, spelled each agency's way",
         "expected": dup_expected},
        {"kind": "limit_cents_vs_dollars", "what": "a converted book stores 3 limits in cents, with no flag saying so",
         "expected": cents_expected, "claim": cents_claim, "right_limit": 250000, "raw_limit": 25000000,
         "check": f"{checks}:limit_in_dollars", "failing_rows": len(cents_expected)},
        {"kind": "retro_dated_cancellation", "what": "2 cancellations were booked 40 days after their effective date",
         "expected": retro_expected, "check": "silver.policies_current:no_retro_dated_cancellation",
         "failing_rows": len(retro_expected), **retro},
        {"kind": "orphan_coverage_rows", "what": "3 coverage rows name a policy that does not exist; they must be quarantined",
         "expected": orphan_expected, "check": "silver.coverage_versions:orphan_coverage_row",
         "failing_rows": len(orphan_expected)},
        {"kind": "wrong_effective_timezone", "what": "3 Colorado policies label 12:01 AM local as UTC, so inception reads as the evening before",
         "expected": tz_expected, "claim": tz_claim, "check": f"{checks}:effective_tz_matches_state", "failing_rows": tz_rows},
    ]
    gt = {"entities": {"Insured": clusters}, "usecases": {"coverage_triage": labels}, "planted": planted,
          "decoys": {"adjacent_renewals": renewal_decoys, "reinstated_before_loss": reinstated_claim,
                     "same_day_cancellation": same_day, "same_name_different_person": name_decoys},
          "meta": {"as_of": AS_OF.isoformat(), "policies": len(policies), "coverage_rows": len(coverage_rows),
                   "claims": len(claims)}}
    (out / "_ground_truth.json").write_text(json.dumps(gt, indent=2))
    return gt
