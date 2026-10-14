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
