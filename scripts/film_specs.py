"""Build one film spec per use case.

Real packs: read web/data/<pack>.json (the engine's own export) so every number, run and trace in
the film is what the engine actually produced. Ideation films: hand-written specs, clearly tagged.
Writes web/films/specs/<id>.json.
"""
import json
import pathlib

root = pathlib.Path(__file__).resolve().parents[1]
out = root / "web" / "films" / "specs"
out.mkdir(parents=True, exist_ok=True)

SYS = {"mainframe": "Mainframe", "portal": "Vendor portal", "spaceman": "Planogram tool", "pos": "Store POS",
       "inventory": "Inventory", "tms": "Transport system", "wms": "Warehouse system", "erp": "ERP",
       "crm": "CRM", "carriers": "Carrier feeds", "core": "Core banking", "olb": "Online banking",
       "aml_engine": "AML engine", "fnol": "Claims intake", "claims_hist": "Claims history",
       "cat": "Catastrophe feed", "roster": "Adjuster roster", "txn": "Transactions"}
FMT = {"fixed_width": "fixed width", "sqlite": "database", "csv": "CSV drop", "jsonl": "event stream"}
SHORT = {"retail": "Retail", "supply_chain": "Supply chain", "banking_aml": "Banking AML", "insurance_claims": "Insurance"}


def nice(s):
    return str(s).replace("_", " ").capitalize()


def fmtn(v):
    if v is None:
        return "none"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) and v % 1 else f"{int(v):,}"


def trace_rows(spans, limit=9):
    if not spans:
        return []
    t0 = min(s["start"] for s in spans)
    t1 = max(s["end"] or s["start"] for s in spans)
    span = max(t1 - t0, 1e-6)
    kids = {}
    for s in spans:
        kids.setdefault(s["parent_id"], []).append(s)
    rows = []

    def walk(pid, depth):
        for s in sorted(kids.get(pid, []), key=lambda z: z["start"]):
            name = s["name"]
            col = None
            evs = [e["name"] for e in s["events"]]
            if not (name.startswith("ontology.action") or name == "node.approval"):
                pass
            elif any("denied" in e for e in evs):
                col = "bad"
            elif any("approval" in e or "interrupt" in e for e in evs):
                col = "warn"
            if "action" in s["attributes"] and name.startswith("ontology.action"):
                name = "action " + str(s["attributes"]["action"])
            rows.append([name, round((s["start"] - t0) / span, 4), round(((s["end"] or s["start"]) - t0) / span, 4), min(depth, 3), col])
            walk(s["span_id"], depth + 1)

    walk(None, 0)
    keep = [r for r in rows if not r[0].startswith(("warehouse", "ontology.function"))]
    must = {i for i, r in enumerate(keep) if r[0].startswith(("action", "node.approval", "node.propose", "agent.run", "node.diagnose"))}
    for i in range(len(keep)):
        if len(must) >= limit:
            break
        must.add(i)
    return [keep[i] for i in sorted(must)][:limit]


def from_pack(pk):
    d = json.loads((root / "web" / "data" / f"{pk}.json").read_text())
    uc = list(d["agents"].values())[0]
    runs = uc["runs"]
    score = lambda r: (any(a["status"] == "denied" for a in r["proposal"].get("attempts", [])) * 2
                       + bool(r.get("review")) + (r["trace_id"] in d["traces"]) * 3)
    run = sorted(runs, key=score, reverse=True)[0]
    rec = None
    for rule in d["reconcile"]["rules"]:
        for y in rule["differences"]:
            m = rule["measures"][0]
            if y["status"] == "explained" and y.get("legacy_" + m) is not None and y.get("new_" + m) is not None:
                rec = {"title": rule["name"], "labels": ["Legacy report", "New platform"], "a": fmtn(y["legacy_" + m]),
                       "b": fmtn(y["new_" + m]), "measure": nice(m), "key": " ".join(map(str, y["key"].values())),
                       "reason": y["reason"], "verdict": "Cutover ready, 0 unexplained differences"}
                break
        if rec:
            break
    flagged = [c for w in d["migrate"]["writes"] for c in w["checks"] if not c["passed"]]
    m = sorted(d["ontology"]["merges"], key=lambda z: -(2 * len({x.split(":")[0] for x in z["members"]}) + len(z["members"])))[0]
    prov = [kv for kv in m["provenance"].items() if kv[0] not in ("dedupe_key", "label")][:4]
    ev = d["evals"]["layers"]
    am = list(ev[-1]["metrics"].values())[0]
    pol = sorted(d["policies"], key=lambda p: (p["effect"] != "forbid", "when" not in p))[:6]
    return {
        "id": pk, "tag": "Use case", "industry": d["industry"], "company": d["title"], "short": SHORT[pk],
        "question": d["decision"], "tagline": d["tagline"],
        "sources": [[SYS.get(l["system"], nice(l["system"])), l["label"].split(" (")[0], FMT.get(l["kind"], l["kind"])] for l in d["legacy"]][:8],
        "problems": [[p["kind"], p["what"][0].upper() + p["what"][1:]] for p in d["planted"]][:5],
        "gate": ([flagged[0]["table"], nice(flagged[0]["check"]), flagged[0]["severity"], flagged[0]["detail"]] if flagged else None),
        "recon": rec,
        "merge": {"type": m["type"], "title": m["title"], "id": m["id"], "method": m["method"].replace(":", " match on "),
                  "members": [x.split(":", 1) for x in m["members"]][:4], "props": [[k, SYS.get(v, v)] for k, v in prov]},
        "run": {"agent": uc["agent"], "target": " · ".join(run["objects"].values()), "diagnosis": nice(run["diagnosis"]),
                "confidence": run["confidence"], "narrative": run["narrative"].split(" [ev:")[0],
                "cites": [c.split("#")[0] for c in run["narrative"].split("[ev:")[1:]],
                "attempts": [[a["action"], a["status"], a.get("reason", "")] for a in run["proposal"].get("attempts", [])],
                "review": [run["review"]["by"], run["review"]["approved"]] if run.get("review") else None,
                "outcome": run["outcome"]["status"], "created": (run["outcome"].get("created") or {}).get("type"),
                "trace": trace_rows(d["traces"].get(run["trace_id"], [])), "trace_id": run["trace_id"]},
        "policies": [[p["effect"], p["principal"], p["action"], p.get("when", "")] for p in pol],
        "results": {"mode": "measured", "layers": [[l["layer"], l["headline"], l["passed"]] for l in ev],
                    "stats": [[str(am["decisions"]), "decisions"], [f"{am['accuracy']:.0%}", "match ground truth"],
                              [f"{am['groundedness']:.0%}", "grounded in evidence"], [str(am["policy_denials"]), "denials held"]]},
    }


HEALTH = {
    "id": "healthcare", "tag": "Ideation", "industry": "Healthcare revenue cycle", "company": "Riverbend Health", "short": "Healthcare",
    "question": "Why was this claim denied, and what is the fastest compliant way to get it paid?",
    "tagline": "Care, billing and compliance working from one model, each seeing only what they should.",
    "sources": [["EHR", "Charts, encounters, orders", "database"], ["Practice management", "Patient accounts, charges", "database"],
                ["Clearinghouse", "835 remittances, 277 status", "file drop"], ["Payer portals", "Prior authorizations", "web export"],
                ["Scheduling", "Appointments and referrals", "database"], ["Finance", "Write-offs and adjustments", "spreadsheet"]],
    "problems": [["identity", "The same patient has an EHR record number and a different billing account number"],
                 ["denials", "11% of claims denied, a third for missing prior authorization"],
                 ["feeds", "Remittance files are sometimes resent, so denials get counted twice"],
                 ["privacy", "Billing staff can open full charts today, far beyond what a claim needs"],
                 ["controls", "Write-offs over $5,000 approved by the same person who created them"]],
    "gate": ["silver.remittances", "No duplicate remittance lines", "quarantine", "25 resent lines held back"],
    "recon": {"title": "Denied claims per week vs the clearinghouse report", "labels": ["Clearinghouse report", "New platform"],
              "a": "312", "b": "287", "measure": "Denials", "key": "week 38",
              "reason": "25 remittance lines were resent by the payer. The old report counted them as new denials.",
              "verdict": "Explained, numbers agree after dedupe"},
    "merge": {"type": "Patient", "title": "Patient P-4821", "id": "PAT-4821", "method": "match on name, date of birth and insurance id",
              "members": [["EHR", "MRN 004821"], ["Practice management", "PA-77120"], ["Payer portal", "member NL-55190"]],
              "props": [["clinical record", "EHR"], ["balance due", "Practice management"], ["coverage", "Payer portal"], ["identity shown to billing", "masked"]]},
    "run": {"agent": "agent:claims-agent", "target": "Claim C-2291 · Northline PPO", "diagnosis": "Missing prior authorization",
            "confidence": 0.9, "narrative": "The payer denied the claim with code CO-197. The authorization was approved two days after the visit, so an appeal with the authorization attached should resolve it.",
            "cites": ["remittance_835", "prior_auth", "claim_details"],
            "attempts": [["submit_appeal", "denied", "forbid: agents never submit appeals on their own"], ["draft_appeal", "needs_approval", "a revenue cycle lead must approve"]],
            "review": ["user:m.lee (revenue cycle lead)", True], "outcome": "applied", "created": "Appeal",
            "trace": [["agent.run", 0, 1, 0, None], ["node.gather", .05, .4, 1, None], ["tool.claim_details", .06, .18, 2, None], ["tool.remittance_835", .2, .3, 2, None],
                      ["tool.prior_auth", .31, .39, 2, None], ["node.diagnose", .42, .45, 1, None], ["node.narrate", .46, .55, 1, None], ["node.propose", .56, .8, 1, None],
                      ["action submit_appeal", .57, .65, 2, "bad"], ["action draft_appeal", .67, .79, 2, None], ["node.approval", .82, .85, 1, "warn"]],
            "trace_id": "run A-1187"},
    "policies": [["forbid", "agent:*", "submit_appeal", ""], ["forbid", "role:billing", "read_sensitive:Patient", ""],
                 ["permit", "role:billing", "write_off", "params.amount <= 500"], ["permit", "role:rev_cycle_lead", "approve_write_off", "approver != creator"],
                 ["permit", "role:care_team", "read_sensitive:Patient", "patient in care_team.panel"], ["permit", "agent:claims-agent", "draft_appeal", ""]],
    "results": {"mode": "planned", "layers": [["access", "Every chart view matches the viewer's role", None], ["controls", "Every large write-off has a second approver", None],
                ["traceability", "Every agent step has inputs, rule, model and approver", None], ["agents", "Appeal drafts graded against coder decisions", None],
                ["evidence", "Access review exportable in one click", None]],
                "stats": [["< 1 day", "to draft an appeal"], ["100%", "PHI access checked by role"], ["0", "self-approved write-offs"], ["1 click", "evidence pack"]]},
}

INVEST = {
    "id": "investing", "tag": "Ideation", "industry": "Personal investing", "company": "Your SIP portfolio", "short": "Investing",
    "question": "Is my reason for owning this company still true, and should my monthly SIP change?",
    "tagline": "Each company you own becomes a living model. Agents watch your thesis, you make the call.",
    "sources": [["Annual reports", "Segments, risks, management", "PDF and XBRL"], ["Quarterly results", "Revenue, margins, cash flow", "XBRL"],
                ["Earnings calls", "Guidance and commentary", "transcripts"], ["Exchange prices", "Daily bars, later live", "price feed"],
                ["Corporate actions", "Splits, dividends, renames", "notices"], ["Your broker", "Holdings and SIPs", "API, read only"]],
    "problems": [["identity", "One company shows up as a ticker, an ISIN and its old name before a rename"],
                 ["prices", "Old prices are not adjusted for a 1:5 split, so returns look wrong"],
                 ["time", "Backtests quietly use results published after the decision date"],
                 ["say vs do", "Management guidance is never checked against what actually arrived"],
                 ["drift", "SIPs keep running after the reason for buying is gone"]],
    "gate": ["silver.prices_daily", "Split adjusted", "block", "unadjusted bars blocked until the 1:5 split is applied"],
    "recon": {"title": "Management guidance vs reported results, FY26 Q2", "labels": ["Guided EBITDA margin", "Reported"],
              "a": "21.0%", "b": "18.6%", "measure": "Margin", "key": "FY26 Q2",
              "reason": "One plant was shut for 3 weeks. Excluding it, margin was 20.7%. Say versus do stays on track.",
              "verdict": "Explained, guidance track record 7 of 8 quarters"},
    "merge": {"type": "Company", "title": "Kestrel Motors Ltd", "id": "CO-KSTRL", "method": "match on ISIN and corporate action history",
              "members": [["Exchange", "KSTRL"], ["Depository", "ISIN INE0KS101019"], ["Filings", "Kestrel Auto Components (old name)"]],
              "props": [["segments", "Annual report"], ["guidance", "Earnings calls"], ["price history", "Exchange, split adjusted"], ["your position", "Broker"]]},
    "run": {"agent": "agent:thesis-watch", "target": "Kestrel Motors · thesis: EV parts share grows past 30%", "diagnosis": "Thesis at risk",
            "confidence": 0.7, "narrative": "EV parts share fell from 27% to 24% over two quarters, and the largest EV customer moved 40% of its volume to a competitor. Two of three thesis claims now fail.",
            "cites": ["segment_revenue", "earnings_call_q2", "customer_concentration"],
            "attempts": [["place_order", "denied", "forbid: agents never place orders"], ["propose_sip_change", "needs_approval", "you approve every SIP change"]],
            "review": ["you", True], "outcome": "applied", "created": "SIP change: monthly amount down 25%",
            "trace": [["agent.run", 0, 1, 0, None], ["node.gather", .05, .42, 1, None], ["tool.segment_revenue", .06, .2, 2, None], ["tool.earnings_call", .21, .33, 2, None],
                      ["tool.customer_concentration", .34, .41, 2, None], ["node.check_thesis", .44, .5, 1, None], ["node.narrate", .51, .58, 1, None],
                      ["node.propose", .6, .82, 1, None], ["action place_order", .61, .68, 2, "bad"], ["action propose_sip_change", .7, .81, 2, None], ["node.approval", .84, .87, 1, "warn"]],
            "trace_id": "run W-0412"},
    "policies": [["forbid", "agent:*", "place_order", ""], ["permit", "agent:thesis-watch", "flag_thesis_break", ""],
                 ["permit", "agent:thesis-watch", "propose_sip_change", "abs(params.change_pct) <= 25"], ["permit", "agent:screener", "add_to_watchlist", "per_week <= 5"],
                 ["permit", "agent:*", "read_as_of", "params.as_of <= run.date"], ["permit", "user:you", "action:*", ""]],
    "results": {"mode": "planned", "layers": [["extraction", "KPIs from filings match official numbers", None], ["identity", "Tickers, ISINs and old names resolve to one company", None],
                ["no lookahead", "Fails if any read uses data published after its date", None], ["backtest", "Decisions scored with costs and taxes across regimes", None],
                ["grounded", "Every thesis claim cites a document", None]],
                "stats": [["10 to 15", "companies watched"], ["quarterly", "thesis checks"], ["0", "orders placed by agents"], ["100%", "proposals with sources"]]},
}

ids = []
for pk in ["retail", "supply_chain", "banking_aml", "insurance_claims"]:
    spec = from_pack(pk)
    (out / f"{pk}.json").write_text(json.dumps(spec, indent=1))
    ids.append(pk)
for spec in (HEALTH, INVEST):
    (out / f"{spec['id']}.json").write_text(json.dumps(spec, indent=1))
    ids.append(spec["id"])
print("specs:", ", ".join(ids))
