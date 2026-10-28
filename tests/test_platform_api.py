"""C0 spine: one app over every workspace.

The platform app wraps what the engine already does: list workspaces with a
mechanically derived verification level, run stages, surface pending
approvals over REST, and verify the audit chain. Nothing here invents
state; every answer is derived from artifacts on disk or the stores.
"""
import pytest
from fastapi.testclient import TestClient

from ironwright.platform_api import create_platform_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os

    root = tmp_path_factory.mktemp("platform")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        with TestClient(create_platform_app()) as c:
            yield c
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


def _real_item_and_open_store(offset: int) -> tuple[str, str]:
    """A real Item id and a distinct OPEN Store id from the built workspace."""
    import json as _json

    from ironwright.ontology.store import OntologyStore
    from ironwright.packs import load_pack

    pack = load_pack("retail")
    store = OntologyStore(pack.paths.ontology_db)
    try:
        item = store.con.execute(
            "select id from objects where type = 'Item' order by id limit 1"
        ).fetchone()[0]
        stores = store.con.execute(
            "select id, props from objects where type = 'Store' order by id"
        ).fetchall()
        open_ids = [sid for sid, props in stores
                    if _json.loads(props).get("status") == "OPEN"]
        return item, open_ids[offset]
    finally:
        store.con.close()


def test_workspaces_start_unproven_on_this_machine(client):
    ws = {w["name"]: w for w in client.get("/workspaces").json()}
    assert set(ws) >= {"retail", "supply_chain", "banking_aml", "insurance_claims"}
    # Nothing has run yet in this data root: no ground truth, no evals report,
    # so nothing may claim L3. The badge is derived, not declared.
    assert ws["retail"]["verification"]["level"] == "L1"


def test_run_executes_stages_and_promotes_to_l3(client):
    r = client.post("/workspaces/retail/runs", json={"seed": 7})
    assert r.status_code == 200
    body = r.json()
    stages = {s["stage"]: s for s in body["stages"]}
    assert set(stages) == {"generate", "migrate", "reconcile", "ontology", "agents", "evals", "export"}
    assert all(s["status"] == "ok" for s in stages.values())
    assert body["evals"]["passed"] is True
    ws = {w["name"]: w for w in client.get("/workspaces").json()}
    v = ws["retail"]["verification"]
    assert v["level"] == "L3"
    assert v["ground_truth"] is True and v["evals_green"] is True


def test_audit_chain_verifies_over_rest(client):
    r = client.get("/workspaces/retail/audit/verify").json()
    assert r["intact"] is True
    assert r["rows"] > 0


def test_approvals_surface_and_decide(client):
    # propose_facing_change requires approval in the retail pack; calling it
    # without approved_by must park it, visibly, in the approvals inbox.
    # Params are derived from the workspace, not invented: a real item and a
    # real OPEN store, per the action's own preconditions.
    item_id, store_id = _real_item_and_open_store(0)
    first = client.post(
        "/workspaces/retail/actions/propose_facing_change",
        json={"params": {"item_id": item_id, "store_id": store_id, "new_facings": 4},
              "actor": "user:category.manager.1"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "needs_approval"

    pending = client.get("/workspaces/retail/approvals").json()
    mine = [p for p in pending if p["action"] == "propose_facing_change"
            and p["params"].get("item_id") == item_id
            and p["params"].get("store_id") == store_id]
    assert mine, f"parked action not in the inbox: {pending}"

    decided = client.post(
        f"/workspaces/retail/approvals/{mine[0]['seq']}/decide",
        json={"approved_by": "role:space_planning_lead", "decision": "approve"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] in ("applied", "applied_pending_sync")

    left = client.get("/workspaces/retail/approvals").json()
    assert not [p for p in left if p["seq"] == mine[0]["seq"]]


def test_denied_approval_stays_out_of_the_applied_path(client):
    item_id, store_id = _real_item_and_open_store(1)
    first = client.post(
        "/workspaces/retail/actions/propose_facing_change",
        json={"params": {"item_id": item_id, "store_id": store_id, "new_facings": 3},
              "actor": "user:category.manager.1"},
    ).json()
    assert first["status"] == "needs_approval"
    pending = client.get("/workspaces/retail/approvals").json()
    row = [p for p in pending if p["params"].get("store_id") == store_id
           and p["params"].get("item_id") == item_id][0]
    denied = client.post(
        f"/workspaces/retail/approvals/{row['seq']}/decide",
        json={"approved_by": "role:space_planning_lead", "decision": "deny"},
    ).json()
    assert denied["status"] == "denied"
    left = client.get("/workspaces/retail/approvals").json()
    assert not [p for p in left if p["seq"] == row["seq"]]


def test_unknown_workspace_is_a_404_not_a_500(client):
    assert client.get("/workspaces/nonsense/approvals").status_code == 404
    assert client.post("/workspaces/nonsense/runs", json={}).status_code == 404


def test_lake_tables_and_readonly_query(client):
    tables = client.get("/workspaces/retail/lake/tables").json()
    idents = {t["ident"] for t in tables}
    assert any(i.startswith("gold.") for i in idents)
    # the pipeline ran: a seeded workspace with zero rows everywhere is a bug
    assert any(t["rows"] > 0 for t in tables)

    gold = sorted(i for i in idents if i.startswith("gold."))[0].replace(".", "_")
    q = client.post("/workspaces/retail/lake/query",
                    json={"sql": f"select count(*) as n from {gold}", "limit": 10}).json()
    assert q["columns"] == ["n"]
    assert int(q["rows"][0][0]) > 0

    for bad in ("drop table gold_x", "select 1; select 2", "update t set a=1"):
        r = client.post("/workspaces/retail/lake/query", json={"sql": bad})
        assert r.status_code == 422, bad


def test_ontology_objects_and_audit_feed(client):
    desc = client.get("/workspaces/retail/ontology").json()
    types = list(desc["describe"]["object_types"])
    assert types
    objs = client.get(f"/workspaces/retail/objects/{types[0]}", params={"limit": 3}).json()
    assert isinstance(objs, list)
    feed = client.get("/workspaces/retail/audit", params={"limit": 5}).json()
    assert feed and {"seq", "actor", "action", "status", "hash"} <= set(feed[0])


def test_console_is_served_when_built(client):
    import ironwright.platform_api as papi

    if not papi.CONSOLE_DIST.exists():
        import pytest as _pytest

        _pytest.skip("console/dist not built in this checkout")
    home = client.get("/", follow_redirects=False)
    assert home.status_code in (302, 307)
    page = client.get("/console/")
    assert page.status_code == 200
    assert "Ironwright Console" in page.text


def test_sources_governance_and_policy_dry_run(client):
    srcs = client.get("/workspaces/retail/sources").json()
    assert srcs and {"name", "system", "kind"} <= set(srcs[0])

    gov = client.get("/workspaces/retail/governance").json()
    assert gov["rules"] and any(r.get("effect") == "permit" for r in gov["rules"])
    assert gov["actions"]["propose_facing_change"]["requires_approval"] is True

    allowed = client.post("/workspaces/retail/governance/check",
                          json={"principal": "agent:retail-triage",
                                "action": "action:propose_facing_change"}).json()
    assert allowed["decision"]["allowed"] is True
    denied = client.post("/workspaces/retail/governance/check",
                         json={"principal": "vendor:V-00001",
                               "action": "action:delist_item"}).json()
    assert denied["decision"]["allowed"] is False


def test_loads_show_wap_branches_and_checks(client):
    loads = client.get("/workspaces/retail/loads").json()
    w = loads["writes"][0]
    assert {"table", "branch", "published", "checks"} <= set(w)
    assert any(c["kind"] == "structural" for c in w["checks"])


def test_agents_report_and_trace_detail(client):
    ag = client.get("/workspaces/retail/agents").json()
    uc = next(iter(ag))
    run = ag[uc]["runs"][0]
    assert {"diagnosis", "narrative", "trace_id"} <= set(run)
    spans = client.get(f"/workspaces/retail/traces/{run['trace_id']}").json()
    assert spans and {"name", "start"} <= set(spans[0])


def test_principals_are_listed_with_the_honesty_note(client):
    p = client.get("/workspaces/retail/principals").json()
    assert "agent:retail-triage" in p["policy_principals"] or p["seen_in_audit"]
    assert "not authentication" in p["note"] or "no authentication" in p["note"]


def test_create_organization_lists_as_l2_and_serves_its_org(client):
    r = client.post("/workspaces", json={"name": "acme-goods", "size": 8, "seed": 4})
    assert r.status_code == 201
    assert r.json()["verification"]["level"] == "L2"

    ws = {w["name"]: w for w in client.get("/workspaces").json()}
    assert ws["acme-goods"]["kind"] == "organization"
    assert ws["acme-goods"]["verification"]["level"] == "L2"
    assert ws["retail"]["kind"] == "pack"

    org = client.get("/workspaces/acme-goods/organization").json()
    assert org["company"] and len(org["stores"]) == 8
    assert org["approver_chains"] and org["data_owners"]

    # Same spec recreated gives the identical org: determinism survives the API.
    import hashlib
    import json as _json
    d1 = hashlib.sha256(_json.dumps(org, sort_keys=True).encode()).hexdigest()
    client.delete("/workspaces/acme-goods")
    client.post("/workspaces", json={"name": "acme-goods", "size": 8, "seed": 4})
    org2 = client.get("/workspaces/acme-goods/organization").json()
    d2 = hashlib.sha256(_json.dumps(org2, sort_keys=True).encode()).hexdigest()
    assert d1 == d2
    client.delete("/workspaces/acme-goods")


def test_workspace_creation_guardrails(client):
    assert client.post("/workspaces", json={"name": "retail"}).status_code == 422
    assert client.post("/workspaces", json={"name": "Bad Name!"}).status_code == 422
    assert client.post("/workspaces", json={"name": "ok-org", "archetype": "no_such"}).status_code == 422
    assert client.delete("/workspaces/retail").status_code == 403
    assert client.delete("/workspaces/never-existed").status_code == 404


def test_platform_map_never_poses(client):
    """The house rule as a test: a node may not claim LIVE without a real
    console surface behind it, and a PLANNED node must name what it is and,
    where applicable, depend on a real node."""
    from ironwright.platform_map import LIVE_VIEWS

    m = client.get("/platform/map").json()
    ids = {n["id"] for n in m["nodes"]}
    live = [n for n in m["nodes"] if n["status"] == "live"]
    planned = [n for n in m["nodes"] if n["status"] == "planned"]
    assert live and planned
    for n in live:
        assert n["view"] in LIVE_VIEWS, n["id"]
    for n in planned:
        assert len(n["detail"]) > 20, n["id"]
        assert n["depends_on"] is None or n["depends_on"] in ids, n["id"]
    assert {n["lane"] for n in m["nodes"]} == set(m["lanes"])


def test_object_dashboard_is_governed_per_principal(client):
    """Sketch 02 gate: the dashboard shows the three-source identity and the
    object's trail, and the action row differs between principals purely
    from policy dry-runs."""
    import json as _json

    from ironwright.ontology.store import OntologyStore
    from ironwright.packs import load_pack

    store = OntologyStore(load_pack("retail").paths.ontology_db)
    try:
        row = store.con.execute(
            "select type, id, members from merge_log order by length(members) desc limit 1"
        ).fetchone()
    finally:
        store.con.close()
    type_, id_, members = row[0], row[1], _json.loads(row[2])
    assert len(members) >= 3

    mgr = client.get(f"/workspaces/retail/objects/{type_}/{id_}/dashboard",
                     headers={"x-principal": "user:category.manager.1"}).json()
    assert len(mgr["_crosswalk"]) == len(members)
    assert isinstance(mgr["_trail"], list) and isinstance(mgr["_pending"], list)
    vend = client.get(f"/workspaces/retail/objects/{type_}/{id_}/dashboard",
                      headers={"x-principal": "vendor:V-00001"}).json()
    mgr_allowed = {a["action"] for a in mgr["_allowed_actions"] if a["allowed"]}
    vend_allowed = {a["action"] for a in vend["_allowed_actions"] if a["allowed"]}
    assert mgr_allowed and vend_allowed != mgr_allowed


def test_boards_guard_persist_and_run(client):
    tiles = [
        {"title": "Gold rows", "sql": "select count(*) as n from gold_item_store_perf", "viz": "number"},
        {"title": "By store", "sql": "select store_nbr, count(*) as n from gold_item_store_perf group by 1 order by 1 limit 10", "viz": "bar"},
        {"title": "Sample", "sql": "select * from gold_item_store_perf limit 5", "viz": "table"},
    ]
    saved = client.put("/workspaces/retail/boards/weekly-ops",
                       json={"title": "Weekly ops", "tiles": tiles})
    assert saved.status_code == 200, saved.text

    bad = client.put("/workspaces/retail/boards/evil",
                     json={"title": "x", "tiles": [{"sql": "update objects set title='x'"}]})
    assert bad.status_code == 422  # rejected at save, per the sketch gate

    run = client.post("/workspaces/retail/boards/weekly-ops/run").json()
    assert all(t["ok"] for t in run["tiles"]), run
    assert run["tiles"][0]["rows"][0][0].isdigit()

    listed = client.get("/workspaces/retail/boards").json()
    assert any(b["slug"] == "weekly-ops" and b["tiles"] == 3 for b in listed)
    assert client.delete("/workspaces/retail/boards/weekly-ops").status_code == 200


# ----- review gates: each test below pins a fix from the five-review pass,
# so the defect it closed cannot ship again with a green suite.

def test_sprint_routers_answer_on_the_bare_platform_app(client):
    # The four sprint route families must be mounted by create_platform_app
    # itself; earlier tests mounted the routers on their own fixture apps, so
    # deleting the include_router lines stayed green.
    assert client.get("/workspaces/retail/escalations").status_code == 200
    preview = client.get("/workspaces/retail/orgpolicy/preview")
    assert preview.status_code == 404 and "hand-written" in preview.json()["detail"]
    item_id, _ = _real_item_and_open_store(0)
    asof = client.get(f"/workspaces/retail/objects/Item/{item_id}/asof",
                      params={"ts": "2200-01-01T00:00:00Z"})
    assert asof.status_code == 200
    otel = client.post("/workspaces/retail/otel/export",
                       json={"endpoint": "http://127.0.0.1:1/v1/traces"})
    assert otel.status_code == 502


def test_dashboard_trail_names_the_denied_delist(client):
    # Sketch 02's gate verbatim: the trail must contain the denied delist,
    # not merely be a list.
    item_id, _ = _real_item_and_open_store(0)
    denied = client.post("/workspaces/retail/actions/delist_item",
                         json={"params": {"item_id": item_id},
                               "actor": "agent:retail-triage"}).json()
    assert denied["status"] == "denied"
    dash = client.get(f"/workspaces/retail/objects/Item/{item_id}/dashboard").json()
    assert any(t["action"] == "delist_item" and t["status"] == "denied"
               for t in dash["_trail"]), dash["_trail"]


def test_escalation_grant_bites_on_serving_until_it_expires(client):
    item_id, _ = _real_item_and_open_store(0)

    def check():
        return client.post("/workspaces/retail/governance/check",
                           json={"principal": "user:analyst.1",
                                 "action": "action:delist_item",
                                 "env": {"params": {"item_id": item_id}}}).json()["decision"]

    assert not check()["allowed"]
    esc = client.post("/workspaces/retail/escalations", json={
        "principal": "user:analyst.1", "action": "delist_item",
        "object_id": item_id, "denying_rule": "default-deny"}).json()
    granted = client.post(f"/workspaces/retail/escalations/{esc['id']}/decide",
                          json={"decision": "grant", "decided_by": "role:category_manager",
                                "expires": "2100-01-01"})
    assert granted.status_code == 200
    after = check()
    assert after["allowed"] and esc["id"] in after["matched"]

    # the governed dashboard chip flips for this principal on this object
    dash = client.get(f"/workspaces/retail/objects/Item/{item_id}/dashboard",
                      headers={"x-principal": "user:analyst.1"}).json()
    chip = next(a for a in dash["_allowed_actions"] if a["action"] == "delist_item")
    assert chip["allowed"], chip

    # and the real act() path consults the same engine: no longer a policy denial
    acted = client.post("/workspaces/retail/actions/delist_item",
                        json={"params": {"item_id": item_id},
                              "actor": "user:analyst.1"}).json()
    assert acted["status"] != "denied", acted

    # a different object stays denied: the grant is as narrow as it claims
    items = client.get("/workspaces/retail/objects/Item", params={"limit": 2}).json()
    other_item = next(o["_id"] for o in items if o["_id"] != item_id)
    narrow = client.post("/workspaces/retail/governance/check",
                         json={"principal": "user:analyst.1",
                               "action": "action:delist_item",
                               "env": {"params": {"item_id": other_item}}}).json()["decision"]
    assert not narrow["allowed"]


def test_hand_written_forbid_still_beats_a_grant(client):
    item_id, _ = _real_item_and_open_store(1)
    esc = client.post("/workspaces/retail/escalations", json={
        "principal": "agent:retail-triage", "action": "delist_item",
        "object_id": item_id, "denying_rule": "no-agent-delisting"}).json()
    client.post(f"/workspaces/retail/escalations/{esc['id']}/decide",
                json={"decision": "grant", "decided_by": "role:category_manager",
                      "expires": "2100-01-01"})
    d = client.post("/workspaces/retail/governance/check",
                    json={"principal": "agent:retail-triage",
                          "action": "action:delist_item",
                          "env": {"params": {"item_id": item_id}}}).json()["decision"]
    assert not d["allowed"] and "no-agent-delisting" in d["matched"]


def test_generated_conflict_with_hand_written_is_dropped_and_reported(client):
    from ironwright import orgpolicy

    orgpolicy.upsert_rules("retail", [{"id": "gen-forbid-propose", "effect": "forbid",
                                       "principal": "*",
                                       "action": "action:propose_facing_change"}],
                           actor="test:conflict")
    rules, conflicts = orgpolicy.combined("retail")
    assert any(c["generated"] == "gen-forbid-propose" for c in conflicts)
    assert all(r["id"] != "gen-forbid-propose" for r in rules)
    d = client.post("/workspaces/retail/governance/check",
                    json={"principal": "user:category.manager.1",
                          "action": "action:propose_facing_change",
                          "env": {}}).json()["decision"]
    assert d["allowed"]  # the hand-written permit survives the generated forbid


def test_rerequested_approval_reappears_and_executes(client):
    # An earlier settlement of the same action+params must not swallow a NEW
    # needs_approval row, and approving the new row must execute, not replay.
    item_id, store_id = _real_item_and_open_store(0)
    body = {"params": {"item_id": item_id, "store_id": store_id, "new_facings": 4},
            "actor": "user:category.manager.1", "idempotency_key": "re-request-1"}
    again = client.post("/workspaces/retail/actions/propose_facing_change", json=body).json()
    assert again["status"] == "needs_approval", again
    pending = client.get("/workspaces/retail/approvals").json()
    mine = [p for p in pending if p["seq"] == again["audit_seq"]]
    assert mine, f"re-request swallowed by an earlier settlement: {pending}"
    decided = client.post(f"/workspaces/retail/approvals/{mine[0]['seq']}/decide",
                          json={"approved_by": "role:space_planning_lead",
                                "decision": "approve"}).json()
    assert decided["status"] in ("applied", "applied_pending_sync")
    assert decided["result"].get("replayed") is not True


def test_approval_decision_must_be_approve_or_deny(client):
    r = client.post("/workspaces/retail/approvals/999999/decide",
                    json={"approved_by": "role:x", "decision": "dny"})
    assert r.status_code == 422


def test_explorer_cannot_touch_files_or_network(client):
    # The connection disables external access before user SQL runs; the shape
    # guard alone cannot stop SELECTs over read_text/glob/httpfs.
    for sql in ("select content from read_text('README.md')",
                "select * from glob('/etc/*')",
                "select * from read_csv_auto('/etc/hosts')",
                "select * from read_text('http://127.0.0.1:1/x')"):
        r = client.post("/workspaces/retail/lake/query", json={"sql": sql})
        assert r.status_code == 400, (sql, r.text)
        assert "disabled" in r.json()["detail"].lower(), r.json()["detail"]


def test_grant_expiry_must_be_a_real_ascii_date(client):
    item_id, _ = _real_item_and_open_store(0)
    esc = client.post("/workspaces/retail/escalations", json={
        "principal": "user:analyst.2", "action": "delist_item",
        "object_id": item_id, "denying_rule": "default-deny"}).json()
    for bad in ("٢٠٢٦-١١-٠١",  # arabic-indic digits
                "9999-99-99", "2026-13-45", "20261101"):
        r = client.post(f"/workspaces/retail/escalations/{esc['id']}/decide",
                        json={"decision": "grant", "decided_by": "role:cm",
                              "expires": bad})
        assert r.status_code == 422, bad


def test_discovery_on_a_created_org_is_empty_not_a_404(client):
    client.post("/workspaces", json={"name": "disco-org", "size": 6, "seed": 2})
    r = client.get("/workspaces/disco-org/discovery")
    assert r.status_code == 200
    body = r.json()
    assert body["findings"] == [] and "invented" in body["note"]
    client.delete("/workspaces/disco-org")


def test_stream_window_duration_is_iso_time_not_calendar():
    from ironwright.connectors import _window_seconds

    assert _window_seconds("PT5M") == 300
    assert _window_seconds("90") == 90.0
    with pytest.raises(ValueError, match="months"):
        _window_seconds("P5M")  # five MONTHS in ISO 8601, never five minutes
    with pytest.raises(ValueError):
        _window_seconds("garbage")


def test_empty_approver_chain_is_a_loud_error():
    from ironwright.orgpolicy import generate

    org = {"data_owners": {"item": "category_manager"},
           "approver_chains": [{"action": "update_item", "chain": []}]}
    with pytest.raises(ValueError, match="empty"):
        generate(org)


def test_live_views_exist_in_the_console_source():
    # LIVE_VIEWS is hand-maintained next to the node table; tie it to the
    # console's real view keys so a posing node cannot borrow a name that
    # does not exist as a surface.
    import pathlib
    import re as _re

    from ironwright.platform_map import LIVE_VIEWS, platform_map

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "console" / "src" / "App.jsx").read_text()
    keys = set(_re.findall(r'\["([a-z]+)",\s*"', src))
    assert LIVE_VIEWS <= keys, LIVE_VIEWS - keys
    for node in platform_map()["nodes"]:
        if node["status"] == "live":
            assert node["view"] in keys, node["id"]
