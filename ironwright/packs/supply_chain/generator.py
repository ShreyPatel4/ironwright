"""Corvane Distribution: a fictional B2B distributor shipping from four DCs.

Legacy systems:
  tms.db                 transportation system: shipments (order ref "SO-0012345"), carriers, a stale ETA
  SHIPCONF.dat           warehouse system shipment confirmations, fixed width, order "000012345"
  erp/ORDERS_*.csv       ERP orders, order number "12345", customer ids
  erp/CUSTOMERS.csv      ERP customer master with DUNS numbers
  crm.db                 CRM accounts: different ids, name variants, a duplicate account, some missing DUNS
  events/*.jsonl         carrier status events (at-least-once delivery, so duplicates)
  reports/DCSHIPRP.dat   the WMS nightly "shipped by DC" report (reconciliation target)

Planted: three order-id formats, customer duplicates and a look-alike, duplicate events,
TMS ETAs that were never updated after a delay, WMS test orders inside the legacy report,
and in-transit delays with five different causes (the agent's ground truth).
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

AS_OF = datetime(2026, 9, 28, 12, 0)
DCS = {"DAL1": "Dallas", "ATL2": "Atlanta", "CHI3": "Chicago", "RNO4": "Reno"}
CARRIERS = [("KTLX", "Kestle Freight Lines", 0.93), ("BRVN", "Bravon Logistics", 0.88),
            ("OSPR", "Osprey Express", 0.95), ("TRNC", "Trancor LTL", 0.86),
            ("MDLN", "Medlin Parcel", 0.91), ("HZLT", "Hazelton Truckload", 0.9)]
CITIES = ["Denver", "Phoenix", "Memphis", "Columbus", "Raleigh", "Boise", "Omaha", "Tampa", "Tulsa", "Fresno"]
NAMES = ["Allied Hardware", "Bluepeak Clinics", "Cobalt Foods", "Delta Office Supply", "Evergreen Grocers",
         "Fairway Medical", "Granite Builders", "Harbor Marine Supply", "Ironwood Furniture", "Juniper Pharmacy",
         "Keystone Auto Parts", "Lakeside Schools", "Monarch Hotels", "Northgate Labs", "Oakline Restaurants",
         "Pinecrest Hospitals", "Quarry Stone Works", "Riverbend Farms", "Summit Fitness", "Tidewater Electric",
         "Union Bakeries", "Vantage Dental", "Willow Pet Care", "Xenon Lighting", "Yardley Garden Centers",
         "Zephyr Cycles", "Atlas Uniforms", "Beacon Printing", "Cascade Water", "Driftwood Breweries"]


def generate(out: Path, seed: int = 11) -> dict:
    rng = random.Random(seed)
    for sub in ("erp", "events", "reports"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    gt: dict = {"entities": {}, "usecases": {"late_shipment_triage": {}}, "planted": []}

    # ---------------- customers: ERP vs CRM ----------------
    erp_c, crm_c, cust_clusters = [], [], []
    for i, nm in enumerate(NAMES):
        cid = f"{10040 + i * 3}"
        duns = f"{rng.randint(100000000, 999999999)}"
        erp_c.append({"customer_id": cid, "customer_name": nm + rng.choice([" Inc", " LLC", " Co", ""]),
                      "duns": duns, "city": rng.choice(CITIES), "tier": rng.choice(["GOLD", "SILVER", "BRONZE"])})
        acc = f"ACC-{i + 1:05d}"
        crm_name = nm.upper() if i % 4 == 0 else nm.replace(" ", "  ") if i % 7 == 0 else nm
        crm_c.append({"account_id": acc, "account_name": crm_name, "duns": None if i % 5 == 2 else duns,
                      "owner": rng.choice(["k.ito", "m.reyes", "s.okafor"])})
        members = [f"erp:{cid}", f"crm:{acc}"]
        if i == 3:
            crm_c.append({"account_id": "ACC-90003", "account_name": nm + " Company", "duns": duns, "owner": "k.ito"})
            members.append("crm:ACC-90003")
        cust_clusters.append(sorted(members))
    crm_c.append({"account_id": "ACC-90100", "account_name": "Allied Hardwood Flooring", "duns": "512200981",
                  "owner": "m.reyes"})
    cust_clusters.append(["crm:ACC-90100"])
    gt["entities"]["Customer"] = cust_clusters
    gt["planted"] += [
        {"kind": "identity", "what": "CRM has a duplicate account for Delta Office Supply (ACC-00004, ACC-90003)"},
        {"kind": "identity", "what": "6 CRM accounts have no DUNS and must match on name; 'Allied Hardwood Flooring' must not merge with 'Allied Hardware'"},
        {"kind": "identity", "what": "orders appear as SO-0012345 (TMS), 000012345 (WMS) and 12345 (ERP)"},
    ]

    # ---------------- orders and shipments ----------------
    orders, ships, events, ship_clusters = [], [], [], []
    causes = (["capacity_delay"] * 7 + ["carrier_breakdown"] * 6 + ["weather_delay"] * 7 + ["address_issue"] * 4 +
              ["minor_delay"] * 6)
    rng.shuffle(causes)
    n_in_transit_planted = 0
    for k in range(420):
        ono = 12000 + k * 7
        cust = rng.choice(erp_c)
        ship_d = AS_OF - timedelta(days=rng.uniform(0.3, 27), hours=rng.uniform(0, 8))
        transit_h = rng.choice([48, 72, 96, 120])
        promised = ship_d + timedelta(hours=transit_h)
        priority = rng.choices(["STD", "EXP", "CRIT"], [0.65, 0.25, 0.10])[0]
        carrier = rng.choice(CARRIERS)
        dc = rng.choice(list(DCS))
        in_transit = promised > AS_OF - timedelta(hours=6) and ship_d < AS_OF - timedelta(hours=6)
        cause = None
        if in_transit and causes:
            cause = causes.pop()
            if cause == "capacity_delay":
                priority = rng.choice(["EXP", "CRIT"])
            if cause == "minor_delay":
                priority = "STD"
        elif in_transit:
            pass
        orders.append({"order_no": str(ono), "customer_id": cust["customer_id"], "priority": priority,
                       "order_value_usd": round(rng.uniform(800, 42000), 2), "requested_date": f"{promised:%Y-%m-%d}",
                       "order_date": f"{ship_d - timedelta(days=1):%Y-%m-%d}"})
        tms_id = f"TMS-{70000 + k * 11}"
        wms_ref = f"W{dc[:3]}{k:06d}"
        latest_eta = promised
        ev_id = 0

        def ev(kind, ts, reason=None, eta=None, loc=None):
            nonlocal ev_id
            ev_id += 1
            events.append({"event_id": f"{tms_id}-E{ev_id:03d}", "scac": carrier[0], "shipment_ref": tms_id,
                           "event_type": kind, "ts": f"{ts:%Y-%m-%dT%H:%M:%SZ}", "reason_code": reason,
                           "new_eta": f"{eta:%Y-%m-%dT%H:%M:%SZ}" if eta else None, "location": loc or rng.choice(CITIES)})

        ev("PICKUP", ship_d, loc=DCS[dc])
        t = ship_d + timedelta(hours=8)
        while t < min(AS_OF, promised - timedelta(hours=4)):
            ev("IN_TRANSIT", t)
            t += timedelta(hours=rng.uniform(10, 16))
        stale_tms = False
        if cause:
            n_in_transit_planted += 1
            at = AS_OF - timedelta(hours=rng.uniform(2, 20))
            if cause == "capacity_delay":
                latest_eta = promised + timedelta(hours=rng.uniform(12, 22))
                ev("DELAY", at, "CAPACITY", latest_eta)
            elif cause == "carrier_breakdown":
                latest_eta = promised + timedelta(hours=rng.uniform(48, 72))
                ev("EXCEPTION", at, "MECH", latest_eta)
            elif cause == "weather_delay":
                latest_eta = promised + timedelta(hours=rng.uniform(24, 48))
                ev("DELAY", at, "WEATHER", latest_eta)
            elif cause == "address_issue":
                latest_eta = promised + timedelta(hours=rng.uniform(24, 36))
                ev("EXCEPTION", at, "ADDRESS", latest_eta)
            elif cause == "minor_delay":
                latest_eta = promised + timedelta(hours=rng.uniform(1.5, 4))
                ev("DELAY", at, "TRAFFIC", latest_eta)
            stale_tms = rng.random() < 0.6
            gt["usecases"]["late_shipment_triage"][tms_id] = cause
        elif promised <= AS_OF:
            ev("DELIVERED", promised - timedelta(hours=rng.uniform(0, 5)))
        ships.append({"tms_shipment_id": tms_id, "order_ref": f"SO-{ono:07d}", "carrier_scac": carrier[0],
                      "origin_dc": dc, "dest_city": rng.choice(CITIES), "ship_date": f"{ship_d:%Y-%m-%d %H:%M}",
                      "promised_eta": f"{promised:%Y-%m-%d %H:%M}",
                      "current_eta": f"{(promised if stale_tms else latest_eta):%Y-%m-%d %H:%M}",
                      "mode": rng.choice(["LTL", "FTL", "PARCEL"]), "weight_lb": rng.randint(40, 18000),
                      "wms_ref": wms_ref, "dc": dc, "ship_day": f"{ship_d:%Y%m%d}"})
        ship_clusters.append(sorted([f"tms:{tms_id}", f"wms:{wms_ref}"]))
    gt["entities"]["Shipment"] = ship_clusters
    gt["planted"] += [
        {"kind": "feeds", "what": "about 5% of carrier events arrive twice (same event_id)"},
        {"kind": "time", "what": "TMS current_eta was never updated after the delay for about 60% of delayed shipments"},
        {"kind": "business", "what": f"{n_in_transit_planted} in-transit shipments delayed by capacity, breakdown, weather, address or traffic"},
        {"kind": "report", "what": "WMS nightly report counts test orders (order numbers starting 9999) on 2026-09-15"},
    ]

    # ---------------- write legacy systems ----------------
    db = sqlite3.connect(out / "tms.db")
    db.executescript("""drop table if exists shipments; drop table if exists carriers;
      create table shipments (tms_shipment_id text, order_ref text, carrier_scac text, origin_dc text, dest_city text,
        ship_date text, promised_eta text, current_eta text, mode text, weight_lb int);
      create table carriers (scac text, carrier_name text, on_time_rate real, expedite_cost_per_lb real);""")
    for s in ships:
        db.execute("insert into shipments values (?,?,?,?,?,?,?,?,?,?)",
                   [s[k] for k in ("tms_shipment_id", "order_ref", "carrier_scac", "origin_dc", "dest_city",
                                   "ship_date", "promised_eta", "current_eta", "mode", "weight_lb")])
    for scac, name, otr in CARRIERS:
        db.execute("insert into carriers values (?,?,?,?)", [scac, name, otr, round(rng.uniform(0.02, 0.06), 3)])
    db.commit()
    db.close()

    report: dict[tuple[str, str], int] = {}
    with open(out / "SHIPCONF.dat", "w") as fh:
        for s in ships:
            ono = int(s["order_ref"][3:])
            fh.write(f"{s['wms_ref']:<10}{ono:09d}{s['dc']:<4}{s['ship_day']:<8}{s['ship_day']:<8}"
                     f"{rng.randint(1, 60):05d}SH\n")
            report[(s["ship_day"], s["dc"])] = report.get((s["ship_day"], s["dc"]), 0) + 1
        for t in range(3):  # WMS test orders the report still counts
            fh.write(f"{'WTST' + str(t).zfill(6):<10}{999900000 + t:09d}RNO4202609152026091500001SH\n")
    report[("20260915", "RNO4")] = report.get(("20260915", "RNO4"), 0) + 3
    with open(out / "reports" / "DCSHIPRP.dat", "w") as fh:
        for (d, dc), n in sorted(report.items()):
            fh.write(f"{d:<8}{dc:<4}{n:06d}\n")

    for name, rows in (("ORDERS_2026Q3.csv", orders), ("CUSTOMERS.csv", erp_c)):
        with open(out / "erp" / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    db = sqlite3.connect(out / "crm.db")
    db.executescript("drop table if exists accounts; create table accounts (account_id text, account_name text, duns text, owner text);")
    for c in crm_c:
        db.execute("insert into accounts values (?,?,?,?)", [c["account_id"], c["account_name"], c["duns"], c["owner"]])
    db.commit()
    db.close()

    rng.shuffle(events)
    dupes = [e for e in events if rng.random() < 0.05]
    by_carrier: dict[str, list] = {}
    for e in events + dupes:
        by_carrier.setdefault(e["scac"], []).append(e)
    for scac, evs in by_carrier.items():
        with open(out / "events" / f"carrier_{scac}.jsonl", "w") as fh:
            for e in evs:
                fh.write(json.dumps(e) + "\n")

    gt["meta"] = {"as_of": AS_OF.isoformat(), "orders": len(orders), "events": len(events) + len(dupes)}
    (out / "_ground_truth.json").write_text(json.dumps(gt, indent=2))
    return gt
