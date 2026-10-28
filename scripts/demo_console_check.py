"""The console half of docs/DEMO-SCRIPT.md, headless.

make demo-check runs the pipeline half (run all packs, evals green,
showcase present). This script walks every console phase the script
added from 1:30 on, against the same data root, through the real
platform app. It prints one line per phase and exits non-zero the
moment a phase breaks, so CI fails before a human reads the script
aloud over a broken console.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from fastapi.testclient import TestClient

from ironwright.platform_api import create_platform_app


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'BROKEN'}  {name}  {detail}")
    if not ok:
        sys.exit(1)


def main() -> None:
    with TestClient(create_platform_app()) as c:
        ws = {w["name"]: w for w in c.get("/workspaces").json()}
        step("1:30 four workspaces with derived badges",
             all(p in ws for p in ("retail", "supply_chain", "banking_aml", "insurance_claims"))
             and ws["retail"]["verification"]["level"] == "L3",
             f"retail={ws.get('retail', {}).get('verification')}")

        door = c.get("/auth/status").json()
        bounced = c.post("/auth/login", json={"email": "x@y", "password": "wrong"})
        step("1:40 the door answers and a wrong password bounces",
             "note" in door and bounced.status_code == 401)

        tables = c.get("/workspaces/retail/lake/tables").json()
        gold = next(t for t in tables if t["layer"] == "gold")
        q = c.post("/workspaces/retail/lake/query",
                   json={"sql": f"select count(*) as n from gold_{gold['name']}"})
        blocked = c.post("/workspaces/retail/lake/query",
                         json={"sql": "select * from read_text('README.md')"})
        step("1:30 explorer answers SELECTs and the engine blocks file reads",
             q.status_code == 200 and int(q.json()["rows"][0][0]) > 0
             and blocked.status_code == 400)

        item = c.get("/workspaces/retail/objects/Item", params={"limit": 1}).json()[0]["_id"]
        dash = c.get(f"/workspaces/retail/objects/Item/{item}/dashboard").json()
        step("1:42 the governed object page renders its parts",
             bool(dash["_crosswalk"]) and "_allowed_actions" in dash and "_trail" in dash)

        asof = c.get(f"/workspaces/retail/objects/Item/{item}/asof",
                     params={"ts": "2200-01-01T00:00:00Z"})
        step("1:41b objects read as-of a moment", asof.status_code == 200)

        disc = c.get("/workspaces/retail/discovery").json()
        step("1:43 discovery derives findings blind", len(disc["findings"]) > 0,
             f"{len(disc['findings'])} findings")

        saved = c.put("/workspaces/retail/boards/demo-check",
                      json={"title": "demo-check",
                            "tiles": [{"title": "n", "viz": "number",
                                       "sql": f"select count(*) as n from gold_{gold['name']}"}]})
        ran = c.post("/workspaces/retail/boards/demo-check/run").json()
        c.delete("/workspaces/retail/boards/demo-check")
        step("1:42 boards save, run and report",
             saved.status_code == 200 and all(t["ok"] for t in ran["tiles"]))

        examples = c.get("/workspaces/retail/ask/examples").json()
        asked = c.post("/workspaces/retail/ask",
                       json={"question": examples[0]["question"]}).json()
        step("1:30 ask answers through governed calls",
             asked["status"] == "answered", asked.get("reason", ""))

        # 1:41b escalation: deny, escalate, grant, and the serving engine flips
        # until the short expiry; a hand-written forbid still wins elsewhere.
        deny = c.post("/workspaces/retail/governance/check",
                      json={"principal": "user:demo.check", "action": "action:delist_item",
                            "env": {"params": {"item_id": item}}}).json()
        esc = c.post("/workspaces/retail/escalations",
                     json={"principal": "user:demo.check", "action": "delist_item",
                           "object_id": item, "denying_rule": "default-deny"}).json()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        c.post(f"/workspaces/retail/escalations/{esc['id']}/decide",
               json={"decision": "grant", "decided_by": "role:category_manager",
                     "expires": tomorrow})
        allow = c.post("/workspaces/retail/governance/check",
                       json={"principal": "user:demo.check", "action": "action:delist_item",
                             "env": {"params": {"item_id": item}}}).json()
        step("1:41b a grant is policy the serving engine consults",
             not deny["decision"]["allowed"] and allow["decision"]["allowed"],
             f"{esc['id']} until {tomorrow}")

        verify = c.get("/workspaces/retail/audit/verify").json()
        step("1:15 the audit chain verifies with the new rows in it", verify["intact"])

        made = c.post("/workspaces", json={"name": "demo-check-org", "size": 6, "seed": 3})
        prev = c.get("/workspaces/demo-check-org/orgpolicy/preview").json()
        applied = c.post("/workspaces/demo-check-org/orgpolicy/apply",
                         json={"approved_by": "user:demo.check"}).json()
        empty = c.get("/workspaces/demo-check-org/discovery").json()
        c.delete("/workspaces/demo-check-org")
        step("1:50 an org generates, previews and applies its governance",
             made.status_code == 201 and prev["rules"] and applied["applied"]
             and empty["findings"] == [])

        pmap = c.get("/platform/map").json()
        live = [n for n in pmap["nodes"] if n["status"] == "live"]
        planned = [n for n in pmap["nodes"] if n["status"] == "planned"]
        step("1:45 the map never poses", len(live) > 0 and len(planned) > 0,
             f"{len(live)} live, {len(planned)} planned")

        otel = c.post("/workspaces/retail/otel/export",
                      json={"endpoint": "http://127.0.0.1:1/v1/traces"})
        step("1:41b trace export fails loudly without a collector, not silently",
             otel.status_code == 502)

        labels = c.get("/workspaces/retail/training/labels")
        ev = c.post("/workspaces/retail/training/evaluate", json={"rules": []})
        step("1:41c training mode: labels from audit, evaluation never adopts",
             labels.status_code == 200 and ev.status_code == 200
             and ev.json()["adopted"] is False,
             f"{len(labels.json())} labels")

        bi_bad = c.put("/workspaces/retail/bi", json={"url": "javascript:alert(1)"})
        bi_ok = c.put("/workspaces/retail/bi", json={"url": "https://example.com/dash"})
        step("1:41c BI embed: config round-trips and a bad scheme bounces",
             bi_bad.status_code == 422 and bi_ok.status_code == 200
             and c.get("/workspaces/retail/bi").json().get("url") == "https://example.com/dash")

        import os
        if os.environ.get("POSTGRES_URI"):
            eng = c.post("/workspaces/retail/engines/check", json={}).json()
            step("1:41c engine swap: every model identical on DuckDB and postgres",
                 eng["match"] is True, f"{len(eng['models'])} models")
            mir = c.post("/workspaces/retail/bi/mirror", json={}).json()
            step("1:41c BI mirror: every table round-trips to postgres",
                 all(t["match"] for t in mir["tables"]), f"{len(mir['tables'])} tables")
        else:
            print("skip  1:41c engine swap + BI mirror  POSTGRES_URI unset; "
                  "claimed only where a real server proves them")

    print("console demo path: every phase answered")


if __name__ == "__main__":
    main()
