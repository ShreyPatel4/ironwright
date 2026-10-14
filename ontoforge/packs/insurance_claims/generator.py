"""Granite Mutual: a fictional homeowners insurer, the week after a hail storm.

Legacy systems:
  POLMAST.dat            policy admin system (mainframe, fixed width), policy "0000012345", covered perils as a list
  claims/FNOL_*.csv      first notice of loss drops from phone, app and agents; policy as "GM-POL-0012345"
  claims_hist.db         prior claims history (SQLite)
  cat.db                 catastrophe events with affected ZIP codes (SQLite)
  ADJUSTERS.csv          adjuster roster with region and open caseload
  reports/CLMRPT.dat     the mainframe's "claims reported per day" report

Planted:
  identity   the same loss reported twice (phone and app) with different claim numbers
  formats    policy numbers formatted two ways
  surge      a hail catastrophe (CAT-26-HL07) floods intake for three days
  coverage   losses before the policy started, perils the policy does not cover
  fraud      new policies with a loss inside 30 days and two or more prior claims
  report     the legacy report counts duplicate reports as separate claims
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 9, 28)
CAT = {"code": "CAT-26-HL07", "peril": "HAIL", "start": date(2026, 9, 18), "end": date(2026, 9, 19)}
CAT_ZIPS = [f"802{n:02d}" for n in range(1, 16)]
OTHER_ZIPS = [f"80{n}" for n in range(301, 340)]
PERIL_SETS = ["WND,HAIL,FIRE,WATR", "WND,HAIL,FIRE", "FIRE,WATR,THFT", "WND,HAIL,FIRE,THFT"]
FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew"]
LAST = ["Reed", "Fox", "Lane", "Hart", "Cole", "Park", "Shaw", "Diaz", "Holt", "Ward", "Bell", "Cruz"]


def generate(out: Path, seed: int = 31) -> dict:
    rng = random.Random(seed)
    for sub in ("claims", "reports"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    gt: dict = {"entities": {}, "usecases": {"claim_triage": {}}, "planted": []}

    policies = []
    for i in range(240):
        zip_ = rng.choice(CAT_ZIPS) if i < 120 else rng.choice(OTHER_ZIPS)
        eff = AS_OF - timedelta(days=rng.randint(40, 330))   # current term, so the policy is in force
        policies.append({"policy_nbr": f"{12000 + i * 7:010d}", "holder": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                         "zip": zip_, "perils": rng.choice(PERIL_SETS), "eff": eff, "exp": eff + timedelta(days=365),
                         "limit": rng.choice([250000, 350000, 500000]), "deductible": rng.choice([500, 1000, 2500])})
    prior = []

    claims, claim_clusters = [], []
    seq = 0

    def fnol(pol, loss_d, peril, amount, label, channel=None, reported=None, zip_=None, dup=False, bad_clock=False):
        nonlocal seq
        seq += 1
        ref = f"FN-26-{100000 + seq * 3}"
        reported = reported or min(AS_OF, loss_d + timedelta(days=rng.randint(0, 3)))
        row = {"claim_ref": ref, "policy_ref": f"GM-POL-{int(pol['policy_nbr']):07d}", "reporter": pol["holder"],
               "loss_date": loss_d.isoformat(), "peril": peril, "loss_zip": zip_ or pol["zip"],
               "est_amount": f"{amount:.2f}", "channel": channel or rng.choice(["PHONE", "APP", "AGENT"]),
               "reported_date": reported.isoformat(),
               "description": {"HAIL": "Hail damage to roof and siding", "WND": "Wind damage, shingles missing",
                               "FIRE": "Kitchen fire", "WATR": "Burst pipe, water damage", "THFT": "Garage break-in",
                               "FLD": "Basement flooding after storm"}[peril]}
        claims.append(row)
        members = [f"fnol:{ref}"]
        if dup:
            seq += 1
            ref2 = f"FN-26-{100000 + seq * 3}"
            rep2 = date.fromisoformat(row["reported_date"]) + timedelta(days=1)
            if bad_clock:   # the app's clock bug: the duplicate is stamped the day before the loss
                rep2 = loss_d - timedelta(days=1)
                gt.setdefault("quarantined", []).append(f"fnol:{ref2}")
            claims.append({**row, "claim_ref": ref2, "channel": "APP", "reported_date": rep2.isoformat()})
            if not bad_clock:
                members.append(f"fnol:{ref2}")
        claim_clusters.append(sorted(members))
        if label:
            gt["usecases"]["claim_triage"][members[0] if len(members) == 1 else sorted(members)[0]] = label
        return row

    # hail surge: covered, small claims to fast-track and big ones for adjusters
    cat_pols = [p for p in policies if p["zip"] in CAT_ZIPS and "HAIL" in p["perils"]]
    rng.shuffle(cat_pols)
    for k, p in enumerate(cat_pols[:24]):
        loss = CAT["start"] + timedelta(days=rng.randint(0, 1))
        amt = rng.uniform(1800, 4900) if k < 14 else rng.uniform(9000, 38000)
        fnol(p, loss, "HAIL", amt, "fast_track" if k < 14 else "large_loss", dup=(k in (2, 5, 9, 17)),
             bad_clock=(k in (5, 17)), reported=loss + timedelta(days=rng.randint(0, 2)))
    # coverage questions: loss before the policy started, or peril not covered
    others = [p for p in policies if p not in cat_pols[:24]]
    rng.shuffle(others)
    for p in others[:3]:
        p["eff"] = AS_OF - timedelta(days=rng.randint(3, 8))
        p["exp"] = p["eff"] + timedelta(days=365)
        fnol(p, p["eff"] - timedelta(days=rng.randint(2, 10)), "WND", rng.uniform(3000, 15000), "coverage_question")
    for p in others[3:6]:
        fnol(p, AS_OF - timedelta(days=rng.randint(2, 9)), "FLD", rng.uniform(6000, 40000), "coverage_question")
    # fraud signals: brand-new policy, loss within 30 days, two or more prior claims
    for p in others[6:10]:
        p["eff"] = AS_OF - timedelta(days=rng.randint(15, 25))
        p["exp"] = p["eff"] + timedelta(days=365)
        for j in range(rng.randint(2, 3)):
            prior.append({"legacy_claim_no": f"L{rng.randint(100000, 999999)}", "policy_nbr": p["policy_nbr"],
                          "loss_date": (p["eff"] - timedelta(days=200 + j * 150)).isoformat(),
                          "paid_amount": round(rng.uniform(4000, 22000), 2), "peril": rng.choice(["THFT", "WATR", "FIRE"])})
        fnol(p, AS_OF - timedelta(days=rng.randint(2, 6)), rng.choice(["THFT", "FIRE"]), rng.uniform(12000, 30000),
             "siu_referral")
    # ordinary non-cat claims, covered
    for p in others[10:16]:
        peril = rng.choice([x for x in p["perils"].split(",") if x != "HAIL"])
        amt = rng.uniform(1200, 4500) if rng.random() < 0.5 else rng.uniform(8000, 25000)
        fnol(p, AS_OF - timedelta(days=rng.randint(2, 12)), peril, amt, "fast_track" if amt <= 5000 else "large_loss")
    # older, already-closed claims for context (not targets)
    for p in others[16:60]:
        prior.append({"legacy_claim_no": f"L{rng.randint(100000, 999999)}", "policy_nbr": p["policy_nbr"],
                      "loss_date": (AS_OF - timedelta(days=rng.randint(400, 1400))).isoformat(),
                      "paid_amount": round(rng.uniform(800, 9000), 2), "peril": rng.choice(["WATR", "FIRE", "WND"])})

    gt["entities"]["Claim"] = claim_clusters
    gt["planted"] += [
        {"kind": "identity", "what": "4 hail losses were reported twice (phone and app) with different claim numbers"},
        {"kind": "formats", "what": "policies are 0000012345 on the mainframe and GM-POL-0012345 in FNOL"},
        {"kind": "surge", "what": f"{CAT['code']} hail event on {CAT['start']} to {CAT['end']} floods intake"},
        {"kind": "coverage", "what": "3 losses before the policy start date, 3 flood losses on policies without flood cover"},
        {"kind": "fraud", "what": "4 new policies with a large loss inside 30 days and 2 or more prior claims"},
        {"kind": "report", "what": "the legacy claims-per-day report counts duplicate reports as separate claims"},
        {"kind": "feeds", "what": "the claims app stamped 2 duplicate reports with a date before the loss (clock bug); they must be quarantined, not published"},
    ]

    # ---------------- write ----------------
    with open(out / "POLMAST.dat", "w") as fh:
        for p in policies:
            fh.write(f"{p['policy_nbr']:<10}{p['holder'][:30]:<30}{p['zip']:<5}{p['perils']:<20}{p['eff']:%Y%m%d}"
                     f"{p['exp']:%Y%m%d}{p['limit']:09d}{p['deductible']:06d}A\n")
    by_day: dict[str, list] = {}
    for c in claims:
        by_day.setdefault(c["reported_date"], []).append(c)
    for d, rows in sorted(by_day.items()):
        with open(out / "claims" / f"FNOL_{d.replace('-', '')}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    with open(out / "reports" / "CLMRPT.dat", "w") as fh:
        for d, rows in sorted(by_day.items()):
            fh.write(f"{d:<10}{len(rows):05d}\n")
    db = sqlite3.connect(out / "claims_hist.db")
    db.executescript("drop table if exists prior_claims; create table prior_claims (legacy_claim_no text, policy_nbr text, loss_date text, paid_amount real, peril text);")
    for r in prior:
        db.execute("insert into prior_claims values (?,?,?,?,?)", [r[k] for k in ("legacy_claim_no", "policy_nbr", "loss_date", "paid_amount", "peril")])
    db.commit()
    db.close()
    db = sqlite3.connect(out / "cat.db")
    db.executescript("drop table if exists cat_events; create table cat_events (cat_code text, peril text, start_date text, end_date text, zips text);")
    db.execute("insert into cat_events values (?,?,?,?,?)", [CAT["code"], CAT["peril"], CAT["start"].isoformat(),
                                                             CAT["end"].isoformat(), ",".join(CAT_ZIPS)])
    db.commit()
    db.close()
    with open(out / "ADJUSTERS.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["adjuster_id", "name", "region", "open_claims", "authority_limit"])
        for k in range(8):
            w.writerow([f"ADJ{k + 1:03d}", f"{FIRST[k]} {LAST[k + 2]}", "CAT" if k < 4 else "METRO",
                        rng.randint(8, 30), 50000 if k % 2 else 25000])
    gt["meta"] = {"as_of": AS_OF.isoformat(), "policies": len(policies), "fnol_rows": len(claims)}
    (out / "_ground_truth.json").write_text(json.dumps(gt, indent=2))
    return gt
