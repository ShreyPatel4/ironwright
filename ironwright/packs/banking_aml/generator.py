"""Harborview Bank: a fictional community bank triaging anti-money-laundering alerts.

Legacy systems:
  CUSTMAST.dat        core banking customer master (mainframe, fixed width), "LAST, FIRST M", DOB YYYYMMDD
  ACCTMAST.dat        core banking accounts (fixed width)
  olb.db              online banking users (SQLite): "First Last", DOB MM/DD/YYYY, a duplicate login
  txns/TXN_2026MM.csv monthly transaction extracts with free-text counterparty names
  aml_alerts.db       the legacy rules engine's alerts (SQLite)
  reports/CTRRPT.dat  the mainframe's monthly currency transaction report count

Planted:
  identity   name formats differ; a duplicate online login; two different people both named
             "Maria Garcia" with different dates of birth (must NOT merge); one shell counterparty
             spelled four ways across unrelated customers
  compliance the legacy CTR job checked single cash transactions over $10,000; the rule is the
             daily aggregate per customer, so the legacy report undercounts
  alerts     true structuring, rapid pass-through, a counterparty network, and false positives
             from a cash-heavy restaurant, a payroll account and a one-off family remittance
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

AS_OF = datetime(2026, 9, 28)
START = datetime(2026, 7, 1)
FIRST = ["James", "Maria", "Robert", "Linda", "David", "Susan", "Daniel", "Karen", "Anthony", "Nancy",
         "Mark", "Lisa", "Steven", "Betty", "Paul", "Sandra", "Kevin", "Donna", "Brian", "Carol",
         "George", "Ruth", "Edward", "Sharon", "Ronald", "Laura", "Jason", "Amy", "Ryan", "Angela"]
LAST = ["Smith", "Johnson", "Lee", "Brown", "Nguyen", "Patel", "Kim", "Lopez", "Wilson", "Anderson",
        "Thomas", "Moore", "Martin", "Clark", "Lewis", "Walker", "Hall", "Young", "Allen", "King",
        "Wright", "Scott", "Green", "Baker", "Adams", "Nelson", "Hill", "Campbell", "Mitchell", "Roberts"]
BUSINESSES = [("Golden Wok Restaurant", "5812"), ("Lakeview Payroll Services", "8721"), ("Summit Auto Repair", "7538"),
              ("Bayside Dental Group", "8021"), ("Corner Market Deli", "5411"), ("Pioneer Roofing", "1761")]
COUNTRIES_OK = ["US", "CA", "MX", "GB", "DE"]
HIGH_RISK = ["XR", "XQ"]   # fictional codes standing in for a high-risk jurisdiction list
SHELL_VARIANTS = ["Nordic Trade Ltd", "NORDIC TRADE LIMITED", "Nordic Trade Ltd.", "Nordik Trade Ltd"]


def generate(out: Path, seed: int = 23) -> dict:
    rng = random.Random(seed)
    for sub in ("txns", "reports"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    gt: dict = {"entities": {}, "usecases": {"alert_triage": {}}, "planted": []}

    # ---------------- customers ----------------
    custs = []
    for i in range(54):
        fn, ln = FIRST[i % 30], LAST[(i * 7) % 30]
        dob = datetime(1950 + rng.randint(0, 50), rng.randint(1, 12), rng.randint(1, 28))
        custs.append({"cust_nbr": f"{4400000000 + i * 37:010d}", "first": fn, "last": ln, "mi": chr(65 + i % 26),
                      "dob": dob, "type": "I", "industry": "", "risk": rng.choice("LLLMMH"),
                      "kyc": AS_OF - timedelta(days=rng.randint(30, 3000)), "addr": f"{rng.randint(10, 9999)} {rng.choice(['Oak', 'Main', 'Pine', 'Lake'])} St"})
    # the name collision: two different Maria Garcias
    for k in range(2):
        custs.append({"cust_nbr": f"{4400090000 + k:010d}", "first": "Maria", "last": "Garcia", "mi": "L" if k == 0 else "E",
                      "dob": datetime(1971, 3, 14) if k == 0 else datetime(1988, 11, 2), "type": "I", "industry": "",
                      "risk": "L", "kyc": AS_OF - timedelta(days=400), "addr": f"{120 + k * 300} Harbor Rd"})
    for j, (bn, mcc) in enumerate(BUSINESSES):
        custs.append({"cust_nbr": f"{4400200000 + j * 11:010d}", "first": "", "last": bn, "mi": "",
                      "dob": datetime(2000 + j, 1, 1), "type": "B", "industry": mcc, "risk": "M",
                      "kyc": AS_OF - timedelta(days=rng.randint(300, 2000)), "addr": f"{rng.randint(1, 900)} Commerce Way"})
    olb, cust_clusters = [], []
    for i, c in enumerate(custs):
        members = [f"core:{c['cust_nbr']}"]
        if c["type"] == "I" and i % 6 != 5:
            uid = f"OB-{30000 + i * 3}"
            nm = f"{c['first']} {c['last']}"
            if i % 4 == 1:
                nm = nm.upper()
            olb.append({"ob_user_id": uid, "full_name": nm, "dob": c["dob"].strftime("%m/%d/%Y"),
                        "email": f"{c['first'].lower()}.{c['last'].lower()}{i}@mail.example"})
            members.append(f"olb:{uid}")
            if i == 8:  # duplicate login
                olb.append({"ob_user_id": "OB-99008", "full_name": f"{c['first']} {c['mi']}. {c['last']}",
                            "dob": c["dob"].strftime("%m/%d/%Y"), "email": f"{c['first'].lower()}{i}@mail2.example"})
                members.append("olb:OB-99008")
        cust_clusters.append(sorted(members))
    gt["entities"]["Customer"] = cust_clusters
    gt["planted"] += [
        {"kind": "identity", "what": "two different customers are both named Maria Garcia, with different dates of birth"},
        {"kind": "identity", "what": "one customer has two online banking logins"},
        {"kind": "identity", "what": "a shell counterparty is spelled four ways across unrelated customers"},
    ]

    # ---------------- accounts ----------------
    accts = []
    for i, c in enumerate(custs):
        n = 2 if (c["type"] == "I" and i % 3 == 0) else 1
        for k in range(n):
            accts.append({"acct_nbr": f"{700100000000 + i * 101 + k:012d}", "cust_nbr": c["cust_nbr"],
                          "product": "BUS" if c["type"] == "B" else ("SAV" if k else "CHK"),
                          "open_date": (c["kyc"] - timedelta(days=rng.randint(0, 200))).strftime("%Y%m%d"), "status": "A"})
    acct_of = {}
    for a in accts:
        acct_of.setdefault(a["cust_nbr"], []).append(a["acct_nbr"])

    # ---------------- background transactions ----------------
    txns = []
    tid = 0

    def tx(acct, ts, amt, direction, channel, cp="", country="US"):
        nonlocal tid
        tid += 1
        txns.append({"txn_id": f"T{tid:08d}", "acct_nbr": acct, "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                     "amount": f"{amt:.2f}", "direction": direction, "channel": channel,
                     "counterparty_name": cp, "counterparty_country": country})

    employers = ["Acme Logistics", "Riverside Health", "City of Harborview", "Northwind Schools"]
    for a in accts:
        c = next(c for c in custs if c["cust_nbr"] == a["cust_nbr"])
        days = (AS_OF - START).days
        for d in range(0, days, 14):
            if c["type"] == "I" and a["product"] == "CHK":
                tx(a["acct_nbr"], START + timedelta(days=d, hours=9), rng.uniform(1400, 3900), "CR", "ACH", rng.choice(employers))
        for _ in range(rng.randint(15, 35)):
            ts = START + timedelta(days=rng.uniform(0, days))
            tx(a["acct_nbr"], ts, rng.uniform(8, 450), "DR", "CARD", rng.choice(["Grocer", "Fuel Stop", "Pharmacy", "Online Retail", "Utility Co"]))
        if c["type"] == "B":
            for d in range(days):
                tx(a["acct_nbr"], START + timedelta(days=d, hours=18), rng.uniform(600, 2500), "CR", "CASH")

    # ---------------- planted alert scenarios ----------------
    alerts = []
    aid = 0

    def alert(acct, rule, label, when, score):
        nonlocal aid
        aid += 1
        a_id = f"AML-{26000 + aid}"
        alerts.append({"alert_id": a_id, "acct_nbr": acct, "rule_code": rule, "alert_date": when.strftime("%Y-%m-%d"),
                       "score": score, "status": "OPEN"})
        gt["usecases"]["alert_triage"][a_id] = label

    indiv = [c for c in custs[:54]]
    rng.shuffle(indiv)
    # true structuring: several cash deposits just under $10k within ~10 days
    for c in indiv[:6]:
        acct = acct_of[c["cust_nbr"]][0]
        base = AS_OF - timedelta(days=rng.randint(8, 20))
        for k in range(rng.randint(4, 6)):
            tx(acct, base + timedelta(days=k * 2, hours=rng.randint(9, 16)), rng.uniform(9050, 9850), "CR", "CASH")
        alert(acct, "STRUCTURING", "structuring", base + timedelta(days=10), rng.randint(70, 95))
    # true rapid movement: big wire in, nearly all out within 48h to one or two counterparties
    for c in indiv[6:10]:
        acct = acct_of[c["cust_nbr"]][0]
        t0 = AS_OF - timedelta(days=rng.randint(5, 25))
        amt = rng.uniform(40000, 120000)
        tx(acct, t0, amt, "CR", "WIRE", "Westbrook Holdings", "GB")
        tx(acct, t0 + timedelta(hours=rng.randint(6, 40)), amt * rng.uniform(0.9, 0.98), "DR", "WIRE",
           rng.choice(["Coastal Ventures SA", "Delmar Consulting"]), rng.choice(COUNTRIES_OK))
        alert(acct, "RAPID_MOVEMENT", "rapid_movement", t0 + timedelta(days=2), rng.randint(65, 90))
    # counterparty network: unrelated customers wiring to the same shell (spelled differently) in a high-risk jurisdiction
    shell_cluster = []
    for k, c in enumerate(indiv[10:13]):
        acct = acct_of[c["cust_nbr"]][0]
        t0 = AS_OF - timedelta(days=rng.randint(6, 30))
        name = SHELL_VARIANTS[k]
        shell_cluster.append(f"txn:{name}")
        tx(acct, t0, rng.uniform(15000, 30000), "DR", "WIRE", name, "XR")
        alert(acct, "HIGH_RISK_GEO", "network_risk", t0 + timedelta(days=1), rng.randint(60, 85))
    # a fourth spelling used once by a business, never alerted
    tx(acct_of[custs[61]["cust_nbr"]][0], AS_OF - timedelta(days=40), 7200, "DR", "WIRE", SHELL_VARIANTS[3], "XR")
    shell_cluster.append(f"txn:{SHELL_VARIANTS[3]}")
    # false positive: restaurant deposits cash just under $10k every few days, consistently, for months
    rest = acct_of[custs[56]["cust_nbr"]][0]
    for d in range(0, (AS_OF - START).days, 3):
        tx(rest, START + timedelta(days=d, hours=22), rng.uniform(8800, 9700), "CR", "CASH")
    alert(rest, "STRUCTURING", "business_as_usual", AS_OF - timedelta(days=3), 72)
    # false positive: payroll company, client payment in and payroll out to many employees
    pay = acct_of[custs[57]["cust_nbr"]][0]
    t0 = AS_OF - timedelta(days=9)
    tx(pay, t0, 96000, "CR", "WIRE", "Bayside Dental Group", "US")
    for e in range(24):
        tx(pay, t0 + timedelta(hours=20), 96000 * 0.95 / 24, "DR", "ACH", f"Employee {e + 1:02d}", "US")
    alert(pay, "RAPID_MOVEMENT", "business_as_usual", t0 + timedelta(days=1), 66)
    # false positives: one-off family remittances and a car sale
    for c in indiv[13:17]:
        acct = acct_of[c["cust_nbr"]][0]
        t0 = AS_OF - timedelta(days=rng.randint(5, 40))
        if rng.random() < 0.5:
            tx(acct, t0, rng.uniform(800, 2400), "DR", "WIRE", f"{c['last']} family", "MX")
            alert(acct, "HIGH_RISK_GEO", "low_risk", t0 + timedelta(days=1), rng.randint(40, 55))
        else:
            tx(acct, t0, rng.uniform(10500, 14000), "CR", "CASH")
            alert(acct, "LARGE_CASH", "low_risk", t0 + timedelta(days=1), rng.randint(35, 50))
    gt["entities"]["Counterparty"] = [sorted(shell_cluster)]
    gt["planted"] += [
        {"kind": "alerts", "what": f"{len(alerts)} open alerts: 13 true positives (structuring, pass-through, shell network) and false positives"},
        {"kind": "compliance", "what": "legacy CTR job counted single cash transactions over $10,000 instead of the daily aggregate per customer"},
    ]

    # legacy CTR report: single transactions over 10k only (the bug)
    ctr: dict[str, int] = {}
    for t in txns:
        if t["channel"] == "CASH" and t["direction"] == "CR" and float(t["amount"]) > 10000:
            m = t["ts"][:7]
            ctr[m] = ctr.get(m, 0) + 1

    # ---------------- write ----------------
    with open(out / "CUSTMAST.dat", "w") as fh:
        for c in custs:
            nm = f"{c['last'].upper()}, {c['first'].upper()} {c['mi']}".strip(", ") if c["type"] == "I" else c["last"].upper()
            fh.write(f"{c['cust_nbr']:<10}{nm[:30]:<30}{c['dob']:%Y%m%d}{c['addr'][:30]:<30}{c['type']:<1}"
                     f"{c['industry']:<4}{c['risk']:<1}{c['kyc']:%Y%m%d}\n")
    with open(out / "ACCTMAST.dat", "w") as fh:
        for a in accts:
            fh.write(f"{a['acct_nbr']:<12}{a['cust_nbr']:<10}{a['product']:<4}{a['open_date']:<8}{a['status']:<1}\n")
    db = sqlite3.connect(out / "olb.db")
    db.executescript("drop table if exists users; create table users (ob_user_id text, full_name text, dob text, email text);")
    for u in olb:
        db.execute("insert into users values (?,?,?,?)", [u["ob_user_id"], u["full_name"], u["dob"], u["email"]])
    db.commit()
    db.close()
    db = sqlite3.connect(out / "aml_alerts.db")
    db.executescript("drop table if exists alerts; create table alerts (alert_id text, acct_nbr text, rule_code text, alert_date text, score int, status text);")
    for a in alerts:
        db.execute("insert into alerts values (?,?,?,?,?,?)", [a[k] for k in ("alert_id", "acct_nbr", "rule_code", "alert_date", "score", "status")])
    db.commit()
    db.close()
    by_month: dict[str, list] = {}
    for t in sorted(txns, key=lambda t: t["ts"]):
        by_month.setdefault(t["ts"][:7].replace("-", ""), []).append(t)
    for m, rows in by_month.items():
        with open(out / "txns" / f"TXN_{m}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    with open(out / "reports" / "CTRRPT.dat", "w") as fh:
        for m in sorted({t["ts"][:7] for t in txns}):
            fh.write(f"{m:<7}{ctr.get(m, 0):05d}\n")
    gt["meta"] = {"as_of": AS_OF.isoformat(), "customers": len(custs), "accounts": len(accts), "txns": len(txns),
                  "alerts": len(alerts)}
    (out / "_ground_truth.json").write_text(json.dumps(gt, indent=2))
    return gt
