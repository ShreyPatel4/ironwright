"""Governed actions on a customer's own workspace, over real routes and the real worker.

The walk: upload, profile, create, run, propose and accept an ontology, then author an
action, request it as a member, and decide it as an owner. No stubs.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_silver_journey import (
    PASSWORD,
    SLUG,
    bearer,
    created,
    join_as,
    profiled,
    start,
    work,
)

from ironwright.platform_api import NOT_FOUND, create_platform_app

ACTIONS = f"/workspaces/{SLUG}/actions"
SET_STATUS = {"template": "set_status", "object_type": "Claim", "request_role": "member",
              "values": ["open", "closed"]}


@pytest.fixture()
def client(meta_db):
    with TestClient(create_platform_app()) as c:
        yield c


@pytest.fixture()
def owner(client):
    r = client.post("/auth/signup", json={"email": "ann@example.com", "password": PASSWORD, "org_name": "Org Ann"})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def ws(client, owner):
    """A workspace with an accepted ontology and built objects."""
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal)
    start(client, owner, "run")
    start(client, owner, "ontology_propose")
    onto = client.get(f"/workspaces/{SLUG}/ontology/proposal", headers=bearer(owner)).json()
    done = client.post(f"/workspaces/{SLUG}/ontology/accept", headers=bearer(owner),
                       json={"proposal_id": onto["id"], "edits": []})
    assert done.status_code == 200, done.text
    work(owner["org_id"], done.json()["job_id"])
    return owner


def authored(c: TestClient, who: dict, **over) -> dict:
    body = {**SET_STATUS, **over}
    draft = c.post(f"{ACTIONS}/propose", headers=bearer(who), json=body)
    assert draft.status_code == 200, draft.text
    done = c.post(f"{ACTIONS}/accept", headers=bearer(who), json={**body, "draft_id": draft.json()["id"]})
    assert done.status_code == 200, done.text
    return draft.json()


def a_claim(c: TestClient, who: dict) -> dict:
    r = c.get(f"/workspaces/{SLUG}/objects/Claim", headers=bearer(who))
    assert r.status_code == 200, r.text
    return r.json()[0]


def request(c: TestClient, who: dict, action: str, claim: dict, status: str = "closed", **extra) -> dict:
    r = c.post(f"{ACTIONS}/{action}", headers=bearer(who),
               json={"params": {"claim_id": claim["_id"], "status": status}, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def pending(c: TestClient, who: dict) -> list[dict]:
    r = c.get(f"/workspaces/{SLUG}/approvals", headers=bearer(who))
    assert r.status_code == 200, r.text
    return r.json()


def decide(c: TestClient, who: dict, seq: int, decision: str = "approve"):
    return c.post(f"/workspaces/{SLUG}/approvals/{seq}/decide", headers=bearer(who),
                  json={"decision": decision})


def history(c: TestClient, who: dict, claim: dict) -> list:
    r = c.get(f"/workspaces/{SLUG}/objects/Claim/{claim['_id']}/dashboard", headers=bearer(who))
    assert r.status_code == 200, r.text
    return r.json()["_history"]


# ---------------------------------------------------------------- authoring

def test_the_templates_come_from_data_and_nothing_is_callable_before_accept(client, ws):
    got = client.get(ACTIONS, headers=bearer(ws))
    assert got.status_code == 200, got.text
    assert {t["name"] for t in got.json()["templates"]} == {"set_status", "flag_for_review", "assign_owner", "add_note"}
    assert got.json()["accepted"] == []
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    r = client.post(f"{ACTIONS}/set_status_claim", headers=bearer(member),
                    json={"params": {"claim_id": claim["_id"], "status": "closed"}})
    assert r.status_code == 404 or r.json()["status"] in ("denied", "rejected"), r.text


def test_a_draft_permits_the_picked_role_and_requires_approval_by_default(client, ws):
    draft = client.post(f"{ACTIONS}/propose", headers=bearer(ws), json=SET_STATUS)
    assert draft.status_code == 200, draft.text
    d = draft.json()
    assert d["name"] == "set_status_claim"
    assert d["requires_approval"] is True
    assert d["approver_roles"] == ["owner", "admin"]
    assert d["policy_rule"]["effect"] == "permit"
    assert d["policy_rule"]["action"] == "action:set_status_claim"
    assert "member" in d["policy_rule"]["when"] and "viewer" not in d["policy_rule"]["when"]
    assert d["params"]["claim_id"] == "Claim"
    assert d["plain"] and "—" not in d["plain"]
    # a draft writes nothing
    assert client.get(ACTIONS, headers=bearer(ws)).json()["accepted"] == []


def test_accept_writes_policies_and_an_audit_row(client, ws):
    authored(client, ws)
    got = client.get(ACTIONS, headers=bearer(ws)).json()
    assert [a["name"] for a in got["accepted"]] == ["set_status_claim"]
    audit = client.get(f"/workspaces/{SLUG}/audit", headers=bearer(ws)).json()
    assert audit[0]["action"] == "action.accept" and audit[0]["actor"] == f"user:{ws['user_id']}"
    gov = client.get(f"/workspaces/{SLUG}/ontology", headers=bearer(ws)).json()
    assert "set_status_claim" in gov["describe"]["actions"]


def test_accept_refuses_a_draft_id_that_does_not_match_the_body(client, ws):
    draft = client.post(f"{ACTIONS}/propose", headers=bearer(ws), json=SET_STATUS).json()
    r = client.post(f"{ACTIONS}/accept", headers=bearer(ws),
                    json={**SET_STATUS, "request_role": "admin", "draft_id": draft["id"]})
    assert r.status_code == 409, r.text
    assert client.get(ACTIONS, headers=bearer(ws)).json()["accepted"] == []


@pytest.mark.parametrize("bad", [
    {"template": "delete_everything"},
    {"object_type": "Nope"},
    {"request_role": "viewer"},
    {"values": ["ok'] or True or ['"]},
    {"name": "propose"},
])
def test_a_bad_draft_is_a_422(client, ws, bad):
    r = client.post(f"{ACTIONS}/propose", headers=bearer(ws), json={**SET_STATUS, **bad})
    assert r.status_code == 422, r.text


def test_a_member_cannot_author_and_a_viewer_cannot_request(client, ws):
    member = join_as(client, ws, "mo", "member")
    assert client.post(f"{ACTIONS}/propose", headers=bearer(member), json=SET_STATUS).status_code == 403
    authored(client, ws)
    viewer = join_as(client, ws, "vi", "viewer")
    claim = a_claim(client, ws)
    r = client.post(f"{ACTIONS}/set_status_claim", headers=bearer(viewer),
                    json={"params": {"claim_id": claim["_id"], "status": "closed"}})
    assert r.status_code == 403, r.text


def test_an_admin_only_permit_denies_a_member(client, ws):
    authored(client, ws, request_role="admin")
    member = join_as(client, ws, "mo", "member")
    out = request(client, member, "set_status_claim", a_claim(client, ws))
    assert out["status"] == "denied", out


# ---------------------------------------------------------------- request and decide

def test_a_member_request_waits_and_the_owner_approves_with_before_and_after(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    before = history(client, ws, claim)
    out = request(client, member, "set_status_claim", claim)
    assert out["status"] == "needs_approval", out
    assert history(client, ws, claim) == before, "an object changed before a human decided"

    [row] = pending(client, ws)
    assert row["actor"] == f"user:{member['user_id']}"
    [change] = row["preview"]
    assert change["after"]["status"] == "closed"
    assert change["before"]["status"] != "closed"

    r = decide(client, ws, row["seq"])
    assert r.status_code == 200, r.text
    assert r.json()["result"]["status"] == "applied", r.json()
    got = client.get(f"/workspaces/{SLUG}/objects/Claim/{claim['_id']}", headers=bearer(ws)).json()
    assert got["status"] == "closed"
    assert pending(client, ws) == []
    assert decide(client, ws, row["seq"]).status_code == 404  # decided once
    audit = client.get(f"/workspaces/{SLUG}/audit", headers=bearer(ws)).json()
    assert audit[0]["approved_by"] == f"user:{ws['user_id']}"
    assert client.get(f"/workspaces/{SLUG}/audit/verify", headers=bearer(ws)).json()["intact"] is True


def test_a_requester_cannot_decide_their_own_request(client, ws):
    authored(client, ws, request_role="admin")
    admin = join_as(client, ws, "ad", "admin")
    claim = a_claim(client, ws)
    out = request(client, admin, "set_status_claim", claim)
    assert out["status"] == "needs_approval", out
    [row] = pending(client, ws)
    for decision in ("approve", "deny"):
        r = client.post(f"/workspaces/{SLUG}/approvals/{row['seq']}/decide", headers=bearer(admin),
                        json={"decision": decision, "approved_by": f"user:{ws['user_id']}"})
        assert r.status_code == 403, r.text
    assert len(pending(client, ws)) == 1
    assert decide(client, ws, row["seq"]).json()["result"]["status"] == "applied"


def test_a_member_cannot_decide(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    request(client, member, "set_status_claim", a_claim(client, ws))
    [row] = pending(client, ws)
    other = join_as(client, ws, "mi", "member")
    assert decide(client, other, row["seq"]).status_code == 403


def test_deny_leaves_the_object_alone(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    before = history(client, ws, claim)
    request(client, member, "set_status_claim", claim)
    [row] = pending(client, ws)
    r = decide(client, ws, row["seq"], "deny")
    assert r.status_code == 200 and r.json()["status"] == "denied", r.text
    assert history(client, ws, claim) == before
    assert pending(client, ws) == []
    assert client.get(f"/workspaces/{SLUG}/audit/verify", headers=bearer(ws)).json()["intact"] is True


def test_a_value_outside_the_list_is_rejected(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    out = request(client, member, "set_status_claim", a_claim(client, ws), status="paid")
    assert out["status"] == "rejected", out


def test_the_status_precondition_holds(client, ws):
    authored(client, ws, requires_approval=False, allowed_statuses=["closed"])
    member = join_as(client, ws, "mo", "member")
    out = request(client, member, "set_status_claim", a_claim(client, ws))
    assert out["status"] == "rejected" and "precondition" in out["reason"], out


def test_without_approval_it_applies_once_and_replays(client, ws):
    authored(client, ws, requires_approval=False)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    before = len(history(client, ws, claim))
    first = request(client, member, "set_status_claim", claim, idempotency_key="k-1")
    assert first["status"] == "applied", first
    again = request(client, member, "set_status_claim", claim, idempotency_key="k-1")
    assert again["replayed"] is True and again["action_run"] == first["action_run"]
    assert len(history(client, ws, claim)) == before + 1, "a replay wrote a second version"
    assert client.get(f"/workspaces/{SLUG}/audit/verify", headers=bearer(ws)).json()["intact"] is True


def test_the_object_page_shows_permitted_actions(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    r = client.get(f"/workspaces/{SLUG}/objects/Claim/{claim['_id']}/dashboard", headers=bearer(member))
    assert r.status_code == 200, r.text
    [a] = r.json()["_allowed_actions"]
    assert a["action"] == "set_status_claim" and a["allowed"] is True and a["requires_approval"] is True
    viewer = join_as(client, ws, "vi", "viewer")
    r = client.get(f"/workspaces/{SLUG}/objects/Claim/{claim['_id']}/dashboard", headers=bearer(viewer))
    assert r.json()["_allowed_actions"][0]["allowed"] is False


@pytest.mark.parametrize("template,params", [
    ("flag_for_review", {"reason": "amount looks high"}),
    ("assign_owner", {"owner": "ann@example.com"}),
    ("add_note", {"note": "called the holder"}),
])
def test_every_template_applies(client, ws, template, params):
    draft = authored(client, ws, template=template, values=None, requires_approval=False)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    r = client.post(f"{ACTIONS}/{draft['name']}", headers=bearer(member),
                    json={"params": {"claim_id": claim["_id"], **params}})
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text


# ---------------------------------------------------------------- Ask offers, never fires

def test_ask_offers_a_permitted_action_with_evidence_and_does_not_fire_it(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    before = history(client, ws, claim)
    r = client.post(f"/workspaces/{SLUG}/ask", headers=bearer(member),
                    json={"question": f"Get claim {claim['_id']}: what is its status?"})
    assert r.status_code == 200, r.text
    ans = r.json()
    assert ans["status"] == "answered", ans
    [offer] = ans["offers"]
    assert offer["action"] == "set_status_claim"
    assert offer["params"] == {"claim_id": claim["_id"]}
    assert offer["evidence"] and set(offer["evidence"]) <= set(ans["citations"])
    assert offer["rule"] == "tenant-action-set_status_claim"
    assert history(client, ws, claim) == before
    assert pending(client, ws) == []


def test_ask_offers_nothing_the_caller_is_not_permitted_to_request(client, ws):
    authored(client, ws, request_role="admin")
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    ans = client.post(f"/workspaces/{SLUG}/ask", headers=bearer(member),
                      json={"question": f"Get claim {claim['_id']}: what is its status?"}).json()
    assert ans["status"] == "answered", ans
    assert ans["offers"] == []


def test_ask_offers_nothing_when_the_question_matches_no_action(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    ans = client.post(f"/workspaces/{SLUG}/ask", headers=bearer(member),
                      json={"question": f"Get claim {claim['_id']}: how much is it?"}).json()
    assert ans["status"] == "answered", ans
    assert ans["offers"] == []


# ---------------------------------------------------------------- another org

def test_org_b_cannot_see_propose_request_or_decide_on_org_a(client, ws):
    authored(client, ws)
    member = join_as(client, ws, "mo", "member")
    claim = a_claim(client, ws)
    request(client, member, "set_status_claim", claim)
    [row] = pending(client, ws)
    b = client.post("/auth/signup", json={"email": "bob@example.com", "password": PASSWORD,
                                          "org_name": "Org Bob"}).json()
    calls = [
        ("GET", ACTIONS, None),
        ("POST", f"{ACTIONS}/propose", SET_STATUS),
        ("POST", f"{ACTIONS}/accept", {**SET_STATUS, "draft_id": "x"}),
        ("POST", f"{ACTIONS}/set_status_claim", {"params": {"claim_id": claim["_id"], "status": "closed"}}),
        ("GET", f"/workspaces/{SLUG}/approvals", None),
        ("POST", f"/workspaces/{SLUG}/approvals/{row['seq']}/decide", {"decision": "approve"}),
        ("POST", f"/workspaces/{SLUG}/approvals/{row['seq']}/decide", {"decision": "deny"}),
    ]
    for method, url, body in calls:
        r = client.request(method, url, headers=bearer(b), json=body)
        assert r.status_code == 404 and r.json()["detail"] == NOT_FOUND, (method, url, r.text)
    assert len(pending(client, ws)) == 1
