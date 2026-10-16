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
    assert all(t["rows"] >= 0 for t in tables)

    gold = sorted(i for i in idents if i.startswith("gold."))[0].replace(".", "_")
    q = client.post("/workspaces/retail/lake/query",
                    json={"sql": f"select count(*) as n from {gold}", "limit": 10}).json()
    assert q["columns"] == ["n"]
    assert int(q["rows"][0][0]) >= 0

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
