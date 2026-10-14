"""Brightline Retail: a fictional regional retailer selling personal care and pet
products, with vendors who want sell-through and compliance data back.

Legacy systems it has today (and their real-world look):
  ITEMMAST.dat   mainframe item master, COBOL fixed width, unit cost in CENTS
  STORMAST.dat   mainframe store master, fixed width, status O/C
  VENDMAST.dat   mainframe vendor master, fixed width, uppercase truncated names
  vendor_portal.db   vendor portal (SQLite): vendors + catalog with GTIN-14, prices in DOLLARS
  spaceman.db    planogram system (SQLite): planogram versions with effective dates
  pos/POS_<region>_<date>.csv   store POS drops, one file per region per day
  inv/INV_<date>.csv   nightly on-hand snapshot keyed by mainframe item number
  reports/DLYSLSRP.dat   the mainframe's own nightly sales report (reconciliation target)

Planted problems, all recorded in the ground truth:
  identity  vendor names differ across systems, a vendor registered twice in the portal,
            two portal vendors missing tax ids, a look-alike vendor that must NOT merge,
            UPC-12 in POS vs GTIN-14 in the portal vs item numbers in inventory
  units     cost in cents on the mainframe, dollars in the portal
  feeds     one POS file re-sent (duplicate rows), the SE region feed quiet for two days
  time      planogram v2 for cluster A removes items and changes facings mid-period
  business  out-of-stocks, a low-facings item, compliance gaps, a chain-wide demand drop
"""
from __future__ import annotations

import csv
import json
import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

START, DAYS = date(2026, 7, 1), 90
WINDOW_START = date(2026, 9, 1)          # analysis window: last 28 days
POG_V2_DATE = date(2026, 8, 15)
QUIET = {"SE": [date(2026, 9, 20), date(2026, 9, 21)]}
RESENT = ("MW", date(2026, 9, 10))       # this file arrives twice

VENDORS = [  # name in portal, mainframe name, tax id
    ("Lumen Skin Company, Inc.", "LUMEN SKIN CO", "41-2290871"),
    ("Harbor Pet Foods", "HARBOR PET FOODS", "36-7781204"),
    ("Northfield Naturals LLC", "NORTHFIELD NATURALS", "27-1093345"),
    ("PureLeaf Botanicals", "PURELEAF BOTANICALS", "58-4410921"),
    ("Kestrel Labs", "KESTREL LABS", "83-3007719"),
    ("Maple & Ash Grooming Co.", "MAPLE AND ASH GROOMNG", "64-9012283"),
    ("BrightPaws Inc", "BRIGHTPAWS INC", "19-5527730"),
    ("Clearwater Health Corp", "CLEARWATER HEALTH", "72-6638104"),
]
DEPTS = {"SKN": "Skin care", "HAR": "Hair care", "PET": "Pet food", "PTC": "Pet care", "WEL": "Wellness"}
REGIONS = ["NE", "SE", "MW", "W"]


def upc_check(d11: str) -> str:
    odd = sum(int(c) for c in d11[0::2])
    even = sum(int(c) for c in d11[1::2])
    return str((10 - (odd * 3 + even) % 10) % 10)


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def generate(out: Path, seed: int = 7) -> dict:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    for sub in ("pos", "inv", "reports"):
        (out / sub).mkdir(exist_ok=True)
    gt: dict = {"entities": {}, "usecases": {"sku_triage": {}}, "planted": []}

    # ---------------- vendors ----------------
    vend_mf, vend_portal = [], []
    for i, (pname, mname, tax) in enumerate(VENDORS):
        vnbr = f"{100200 + i * 7:06d}"
        vend_mf.append({"vendor_nbr": vnbr, "name": mname[:22], "tax_id": tax, "terms": rng.choice(["N30", "N45", "N60"])})
    clusters = []
    for i, (pname, mname, tax) in enumerate(VENDORS):
        code = f"V-{pname.split()[0][:3].upper()}{i + 1:02d}"
        tax_digits = tax.replace("-", "")
        if i in (3, 6):  # two portal vendors never entered a tax id
            tax_digits = None
        vend_portal.append({"vendor_code": code, "legal_name": pname, "tax_id": tax_digits,
                            "contact_email": f"partners@{pname.split()[0].lower()}.example"})
        members = [f"mainframe:{vend_mf[i]['vendor_nbr']}", f"portal:{code}"]
        if i == 1:  # registered twice in the portal
            vend_portal.append({"vendor_code": "V-HAR99", "legal_name": "Harbor Pet Foods LLC", "tax_id": tax_digits,
                                "contact_email": "ap@harborpet.example"})
            members.append("portal:V-HAR99")
        clusters.append(sorted(members))
    # look-alike that must stay separate
    vend_portal.append({"vendor_code": "V-KES20", "legal_name": "Kestrel Lab Supply", "tax_id": "831140226",
                        "contact_email": "sales@kestrelsupply.example"})
    clusters.append(["portal:V-KES20"])
    gt["entities"]["Vendor"] = clusters
    gt["planted"] += [
        {"kind": "identity", "what": "Harbor Pet Foods registered twice in the vendor portal (V-HAR02, V-HAR99)"},
        {"kind": "identity", "what": "PureLeaf and BrightPaws have no tax id in the portal, must match by name"},
        {"kind": "identity", "what": "Kestrel Lab Supply looks like Kestrel Labs but has a different tax id"},
    ]

    # ---------------- items ----------------
    items = []
    n = 0
    for vi, v in enumerate(vend_mf):
        for _ in range(7 if vi != 7 else 4):
            n += 1
            d11 = f"0{rng.randint(10000, 99999)}{rng.randint(10000, 99999)}"
            upc = d11 + upc_check(d11)
            dept = rng.choice(list(DEPTS))
            items.append({"item_nbr": f"{400000000 + n * 13:09d}", "upc": upc, "vendor_idx": vi,
                          "vendor_nbr": v["vendor_nbr"], "dept": dept,
                          "desc": f"{VENDORS[vi][0].split()[0]} {DEPTS[dept]} {n:02d}",
                          "cost_cents": rng.randint(150, 2400), "rate": rng.uniform(3.0, 7.5)})
    # two portal-only items (not set up on the mainframe yet)
    portal_only = []
    for k in range(2):
        d11 = f"0{rng.randint(10000, 99999)}{rng.randint(10000, 99999)}"
        portal_only.append({"upc": d11 + upc_check(d11), "vendor_idx": 0, "desc": f"Lumen New Launch {k + 1}"})

    # ---------------- stores ----------------
    stores = []
    for s in range(16):
        stores.append({"store_nbr": f"{101 + s:04d}", "name": f"Brightline #{101 + s}", "region": REGIONS[s % 4],
                       "cluster": "A" if s < 10 else "B", "status": "C" if s == 15 else "O",
                       "open_date": f"20{10 + s % 9:02d}0{1 + s % 9}15", "traffic": rng.uniform(0.8, 1.3)})
    open_stores = [s for s in stores if s["status"] == "O"]

    # ---------------- planograms ----------------
    idx = list(range(len(items)))
    rng.shuffle(idx)
    pogA_v1 = idx[:44]
    pogB_v1 = idx[10:52]
    removed_v2 = pogA_v1[:2]                     # dropped from cluster A on Aug 15
    pogA_v2 = [i for i in pogA_v1 if i not in removed_v2] + idx[52:54]
    facings = {i: rng.choice([2, 3, 3, 4]) for i in range(len(items))}
    low_face_item = [i for i in pogB_v1 if i in pogA_v1 and i not in removed_v2][0]
    planograms = {("A", 1): (pogA_v1, START), ("A", 2): (pogA_v2, POG_V2_DATE), ("B", 1): (pogB_v1, START)}
    gt["planted"].append({"kind": "time", "what": f"planogram A v2 on {POG_V2_DATE} removes items "
                          f"{[items[i]['item_nbr'] for i in removed_v2]}; joins must use the version in effect"})

    def on_pog(item_i: int, store: dict, d: date) -> int:
        """facings in effect for this item, store and day (0 if not on the planogram)"""
        if store["cluster"] == "A":
            members = pogA_v2 if d >= POG_V2_DATE else pogA_v1
        else:
            members = pogB_v1
        if item_i not in members:
            return 0
        if store["cluster"] == "B" and item_i == low_face_item:
            return 1
        return facings[item_i]

    # ---------------- planted business problems ----------------
    window_days = [WINDOW_START + timedelta(d) for d in range(28)]
    weak_item = [i for i in pogA_v2 if i in pogA_v1 and i != low_face_item and i not in pogB_v1][0]
    candidates = [(i, s["store_nbr"]) for s in open_stores for i in range(len(items))
                  if on_pog(i, s, window_days[-1]) and i not in (weak_item, low_face_item)]
    rng.shuffle(candidates)
    oos_pairs = candidates[:8]
    comp_pairs = [p for p in candidates[8:] if p[1] != "0108"][:3] + [next(p for p in candidates[8:] if p[1] == "0108")]
    oos_days = {p: set(window_days[rng.randint(3, 10):][:rng.randint(14, 17)]) for p in oos_pairs}
    comp_start = {p: window_days[-rng.randint(15, 18)] for p in comp_pairs}

    # ---------------- POS + inventory ----------------
    pos_rows: dict[tuple[str, date], list[dict]] = {}
    inv_rows: dict[date, list[dict]] = {}
    for d_i in range(DAYS):
        d = START + timedelta(d_i)
        dow = [1.0, 0.9, 0.9, 1.0, 1.15, 1.35, 1.2][d.weekday()]
        for s in stores:
            if s["status"] == "C" and d >= date(2026, 8, 1):
                continue
            for i, it in enumerate(items):
                f = on_pog(i, s, d)
                key = (i, s["store_nbr"])
                on_hand = rng.randint(8, 40) if f else 0
                if key in oos_days and d in oos_days[key]:
                    on_hand = 0
                if f:
                    inv_rows.setdefault(d, []).append({"STORE_NBR": s["store_nbr"], "ITEM_NBR": it["item_nbr"],
                                                       "ON_HAND_QTY": on_hand})
                if not f or on_hand == 0:
                    continue
                lam = it["rate"] * s["traffic"] * dow
                if i == weak_item and d >= WINDOW_START:
                    lam *= 0.22
                if key in comp_start and d >= comp_start[key]:
                    lam = 0
                units = poisson(rng, lam)
                if f == 1:
                    units = min(units, 2)     # one facing empties fast: sales capped by shelf space
                if units == 0:
                    continue
                price = round(it["cost_cents"] / 100 * rng.uniform(1.6, 1.9), 2)
                ret = 1 if rng.random() < 0.02 else 0
                pos_rows.setdefault((s["region"], d), []).append({
                    "store_nbr": s["store_nbr"], "upc": it["upc"], "sale_date": d.isoformat(),
                    "units": units, "net_sales": f"{units * price:.2f}", "returns_units": ret})
    for p in oos_pairs:
        gt["usecases"]["sku_triage"][f"{items[p[0]]['item_nbr']}|{p[1]}"] = "out_of_stock"
    for p in comp_pairs:
        gt["usecases"]["sku_triage"][f"{items[p[0]]['item_nbr']}|{p[1]}"] = "compliance_gap"
    for s in open_stores:
        if s["cluster"] == "B":
            gt["usecases"]["sku_triage"][f"{items[low_face_item]['item_nbr']}|{s['store_nbr']}"] = "low_facings"
        if on_pog(weak_item, s, window_days[-1]):
            gt["usecases"]["sku_triage"][f"{items[weak_item]['item_nbr']}|{s['store_nbr']}"] = "weak_demand"
    gt["planted"] += [
        {"kind": "business", "what": f"{len(oos_pairs)} item-store pairs out of stock for 14 to 17 days"},
        {"kind": "business", "what": f"{len(comp_pairs)} item-store pairs in stock but not on the shelf (zero sales)"},
        {"kind": "business", "what": f"item {items[low_face_item]['item_nbr']} has one facing in cluster B, sales capped"},
        {"kind": "business", "what": f"item {items[weak_item]['item_nbr']} demand drops ~78% chain-wide in September"},
        {"kind": "feeds", "what": f"POS file {RESENT[0]} {RESENT[1]} sent twice; SE POS quiet on {[str(x) for x in QUIET['SE']]}"},
        {"kind": "units", "what": "mainframe cost in cents, portal list price in dollars"},
    ]

    # write POS files (with the quiet feed and the re-sent file)
    legacy_report: dict[date, list[int]] = {}
    for (region, d), rows in sorted(pos_rows.items()):
        if d in QUIET.get(region, []):
            continue
        copies = 2 if (region, d) == RESENT else 1
        with open(out / "pos" / f"POS_{region}_{d:%Y%m%d}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            for _ in range(copies):
                w.writerows(rows)
        agg = legacy_report.setdefault(d, [0, 0])
        for _ in range(copies):  # the legacy nightly job never deduplicated re-sends
            for r in rows:
                agg[0] += int(r["units"])
                agg[1] += round(float(r["net_sales"]) * 100)
    for d, rows in sorted(inv_rows.items()):
        with open(out / "inv" / f"INV_{d:%Y%m%d}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    # ---------------- mainframe fixed-width files ----------------
    with open(out / "ITEMMAST.dat", "w") as fh:
        for it in items:
            fh.write(f"{it['item_nbr']:<9}{it['upc']:<12}{it['desc'][:30].upper():<30}{it['vendor_nbr']:<6}"
                     f"{it['dept']:<3}{it['cost_cents']:09d}{'A':<1}\n")
    with open(out / "STORMAST.dat", "w") as fh:
        for s in stores:
            fh.write(f"{s['store_nbr']:<4}{s['name'][:20]:<20}{s['region']:<2}{s['cluster']:<1}{s['status']:<1}"
                     f"{s['open_date']:<8}\n")
    with open(out / "VENDMAST.dat", "w") as fh:
        for v in vend_mf:
            fh.write(f"{v['vendor_nbr']:<6}{v['name']:<22}{v['tax_id']:<10}{v['terms']:<3}\n")
    with open(out / "reports" / "DLYSLSRP.dat", "w") as fh:
        for d, (u, c) in sorted(legacy_report.items()):
            fh.write(f"{d:%Y-%m-%d}{u:09d}{c:012d}\n")

    # ---------------- vendor portal ----------------
    db = sqlite3.connect(out / "vendor_portal.db")
    db.executescript("""drop table if exists vendors; drop table if exists catalog;
      create table vendors (vendor_code text, legal_name text, tax_id text, contact_email text, created_at text);
      create table catalog (gtin14 text, vendor_code text, item_name text, list_price_usd real, case_pack int);""")
    for v in vend_portal:
        db.execute("insert into vendors values (?, ?, ?, ?, ?)",
                   [v["vendor_code"], v["legal_name"], v["tax_id"], v["contact_email"], "2024-03-01"])
    code_by_idx = {i: vend_portal[[p["legal_name"] for p in vend_portal].index(VENDORS[i][0])]["vendor_code"]
                   for i in range(len(VENDORS))}
    item_clusters = []
    for k, it in enumerate(items):
        if k in (5, 23, 41):  # three items never listed in the portal
            item_clusters.append([f"mainframe:{it['item_nbr']}"])
            continue
        gtin = "00" + it["upc"]
        db.execute("insert into catalog values (?, ?, ?, ?, ?)",
                   [gtin, code_by_idx[it["vendor_idx"]], it["desc"].title() + " (12 ct)",
                    round(it["cost_cents"] / 100 * 1.75, 2), rng.choice([6, 12, 24])])
        item_clusters.append(sorted([f"mainframe:{it['item_nbr']}", f"portal:{gtin}"]))
    for po in portal_only:
        gtin = "00" + po["upc"]
        db.execute("insert into catalog values (?, ?, ?, ?, ?)", [gtin, code_by_idx[0], po["desc"], 14.99, 12])
        item_clusters.append([f"portal:{gtin}"])
    db.commit()
    db.close()
    gt["entities"]["Item"] = item_clusters

    # ---------------- planogram system ----------------
    db = sqlite3.connect(out / "spaceman.db")
    db.executescript("""drop table if exists planograms; drop table if exists positions;
      create table planograms (pog_id text, cluster text, version int, effective_date text, status text);
      create table positions (pog_id text, version int, upc text, fixture text, shelf int, facings int);""")
    for (cl, ver), (members, eff) in planograms.items():
        pid = f"POG-PC-{cl}"
        db.execute("insert into planograms values (?, ?, ?, ?, ?)", [pid, cl, ver, eff.isoformat(), "PUBLISHED"])
        for j, i in enumerate(members):
            f = 1 if (cl == "B" and i == low_face_item) else facings[i]
            db.execute("insert into positions values (?, ?, ?, ?, ?, ?)",
                       [pid, ver, items[i]["upc"], f"BAY-{j // 8 + 1:02d}", j % 5 + 1, f])
    db.commit()
    db.close()

    gt["entities"]["Store"] = [[f"mainframe:{s['store_nbr']}"] for s in stores]
    gt["meta"] = {"window": [WINDOW_START.isoformat(), window_days[-1].isoformat()], "seed": seed,
                  "items": len(items), "stores": len(stores), "vendors_portal": len(vend_portal)}
    (out / "_ground_truth.json").write_text(json.dumps(gt, indent=2))
    return gt
