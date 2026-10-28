"""Sketch 07 slice: labels replayed from audit, gated offline evaluation.

The fixture plants the human verdicts itself through the real platform app:
an approval, a denial, a straight policy denial, a re-request decided on its
own, and one request left unsettled. Every assertion below is against those
planted outcomes; nothing is hand-set.
"""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ironwright import training
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app


def _item_and_open_stores(n: int) -> tuple[str, list[str]]:
    """A real Item id and n distinct OPEN Store ids from the built workspace."""
    from ironwright.ontology.store import OntologyStore

    pack = load_pack("retail")
    store = OntologyStore(pack.paths.ontology_db)
    try:
        item = store.con.execute(
            "select id from objects where type = 'Item' order by id limit 1").fetchone()[0]
        rows = store.con.execute(
            "select id, props from objects where type = 'Store' order by id").fetchall()
        open_ids = [sid for sid, props in rows if json.loads(props).get("status") == "OPEN"]
        assert len(open_ids) >= n
        return item, open_ids[:n]
    finally:
        store.con.close()


def _propose(c: TestClient, item: str, store: str, facings: int, key: str) -> int:
    r = c.post("/workspaces/retail/actions/propose_facing_change",
               json={"params": {"item_id": item, "store_id": store, "new_facings": facings},
                     "actor": "user:category.manager.1", "idempotency_key": key}).json()
    assert r["status"] == "needs_approval", r
    return r["audit_seq"]


def _decide(c: TestClient, seq: int, decision: str) -> dict:
    r = c.post(f"/workspaces/retail/approvals/{seq}/decide",
               json={"approved_by": "role:space_planning_lead", "decision": decision})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    root = tmp_path_factory.mktemp("training")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        with TestClient(create_platform_app()) as c:
            run = c.post("/workspaces/retail/runs", json={"seed": 7})
            assert run.status_code == 200 and run.json()["evals"]["passed"] is True

            item, (s0, s1) = _item_and_open_stores(2)
            planted = {"item": item, "stores": [s0, s1]}

            # approved: request then a human approves
            planted["approved_seq"] = _propose(c, item, s0, 4, "train-approve")
            approved = _decide(c, planted["approved_seq"], "approve")
            assert approved["status"] in ("applied", "applied_pending_sync")

            # denied: request then a human denies
            planted["denied_seq"] = _propose(c, item, s1, 3, "train-deny")
            assert _decide(c, planted["denied_seq"], "deny")["status"] == "denied"

            # straight policy denial: default deny, no needs_approval precursor
            pol = c.post("/workspaces/retail/actions/delist_item",
                         json={"params": {"item_id": item},
                               "actor": "user:trainee.1"}).json()
            assert pol["status"] == "denied", pol
            planted["policy_denied_seq"] = pol["audit_seq"]

            # re-request of the approved params: must pair with its OWN settlement
            planted["re_seq"] = _propose(c, item, s0, 4, "train-re-request")
            re_decided = _decide(c, planted["re_seq"], "approve")
            assert re_decided["result"].get("replayed") is not True

            # unsettled: parked and never decided, so never a label
            planted["unsettled_seq"] = _propose(c, item, s0, 5, "train-unsettled")

            yield {"client": c, "planted": planted}
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


def _by_seq(labels: list[dict]) -> dict[int, dict]:
    out = {lb["seq"]: lb for lb in labels}
    assert len(out) == len(labels), "two labels share a request seq"
    return out


def test_labels_match_the_planted_outcomes(ws):
    p = ws["planted"]
    labels = training.derive_labels(load_pack("retail"))
    by_seq = _by_seq(labels)

    approved = by_seq[p["approved_seq"]]
    assert approved["verdict"] == "approved"
    assert approved["requested_by"] == "user:category.manager.1"
    assert approved["decided_by"] == "role:space_planning_lead"
    assert approved["action"] == "propose_facing_change"
    assert approved["params"] == {"item_id": p["item"], "store_id": p["stores"][0],
                                  "new_facings": 4}
    assert approved["settled_seq"] > approved["seq"]

    denied = by_seq[p["denied_seq"]]
    assert denied["verdict"] == "denied"
    assert denied["decided_by"] == "role:space_planning_lead"
    assert denied["params"]["store_id"] == p["stores"][1]

    pol = by_seq[p["policy_denied_seq"]]
    assert pol["verdict"] == "denied_by_policy"
    assert pol["decided_by"] is None
    assert "default deny" in pol["rule"]
    assert pol["seq"] == pol["settled_seq"]

    # the re-request pairs with its own, later settlement, not the first one
    re = by_seq[p["re_seq"]]
    assert re["verdict"] == "approved"
    assert re["params"] == approved["params"]
    assert re["seq"] > approved["settled_seq"]
    assert re["settled_seq"] > re["seq"]

    # the unsettled request is not a label
    assert p["unsettled_seq"] not in by_seq

    for lb in labels:
        assert lb["verdict"] in ("approved", "denied", "denied_by_policy")
        assert lb["settled_seq"] >= lb["seq"]


def test_evaluate_with_an_empty_proposal_scores_the_current_rules(ws):
    p = ws["planted"]
    pack = load_pack("retail")
    rep = training.evaluate(pack, [])
    assert rep["adopted"] is False
    assert "orgpolicy and escalation" in rep["note"]
    assert rep["labels"] == len(training.derive_labels(pack))
    assert rep["agreements"] + len(rep["disagreements"]) == rep["labels"]
    assert rep["agreement_rate"] == round(rep["agreements"] / rep["labels"], 4)

    dseqs = {d["seq"] for d in rep["disagreements"]}
    # current rules deny delist_item to user:trainee.1, matching the verdict
    assert p["policy_denied_seq"] not in dseqs
    # current rules permit the approved proposal, matching the verdict
    assert p["approved_seq"] not in dseqs
    # the human denied what the rules permit: an honest score reports it
    human_denial = next(d for d in rep["disagreements"] if d["seq"] == p["denied_seq"])
    assert human_denial == {"seq": p["denied_seq"], "action": "propose_facing_change",
                            "expected": "deny", "got": "permit"}


def test_a_rule_that_flips_a_denied_label_reports_exactly_that_disagreement(ws):
    p = ws["planted"]
    pack = load_pack("retail")
    base = training.evaluate(pack, [])
    flip = training.evaluate(pack, [{"id": "trial-permit-trainee", "effect": "permit",
                                     "principal": "user:trainee.1",
                                     "action": "action:delist_item"}])
    base_seqs = {d["seq"] for d in base["disagreements"]}
    flip_seqs = {d["seq"] for d in flip["disagreements"]}
    assert flip_seqs - base_seqs == {p["policy_denied_seq"]}
    assert base_seqs <= flip_seqs
    new_row = next(d for d in flip["disagreements"] if d["seq"] == p["policy_denied_seq"])
    assert new_row == {"seq": p["policy_denied_seq"], "action": "delist_item",
                       "expected": "deny", "got": "permit"}
    assert flip["agreements"] == base["agreements"] - 1
    assert base["adopted"] is False and flip["adopted"] is False


def test_write_labels_is_deterministic_byte_for_byte(ws):
    pack = load_pack("retail")
    first = training.write_labels(pack)
    bytes_one = (pack.paths.reports / "training_labels.jsonl").read_bytes()
    second = training.write_labels(pack)
    bytes_two = (pack.paths.reports / "training_labels.jsonl").read_bytes()
    assert first == second
    assert bytes_one == bytes_two and len(bytes_one) > 0
    lines = [json.loads(line) for line in bytes_one.decode().splitlines()]
    assert lines == training.derive_labels(pack)


def test_router_serves_labels_and_evaluation(ws):
    app = FastAPI()
    app.include_router(training.router)
    with TestClient(app) as c:
        p = ws["planted"]
        got = c.get("/workspaces/retail/training/labels")
        assert got.status_code == 200
        body = got.json()
        assert body["count"] == len(body["labels"]) > 0
        assert any(lb["seq"] == p["approved_seq"] for lb in body["labels"])

        rep = c.post("/workspaces/retail/training/evaluate", json={"rules": []})
        assert rep.status_code == 200
        assert rep.json()["adopted"] is False

        bad = c.post("/workspaces/retail/training/evaluate",
                     json={"rules": [{"id": "shapeless"}]})
        assert bad.status_code == 422

        assert c.get("/workspaces/nonsense/training/labels").status_code == 404

        # a created organization has no store: an honest 404, not an invented []
        ws["client"].post("/workspaces", json={"name": "train-org", "size": 6, "seed": 2})
        try:
            org = c.get("/workspaces/train-org/training/labels")
            assert org.status_code == 404
            assert "no audit rows" in org.json()["detail"]
        finally:
            ws["client"].delete("/workspaces/train-org")


def test_adopted_is_false_in_every_report(ws):
    pack = load_pack("retail")
    for proposal in ([], [{"id": "noop", "effect": "forbid",
                           "principal": "user:nobody", "action": "action:none"}]):
        assert training.evaluate(pack, proposal)["adopted"] is False
