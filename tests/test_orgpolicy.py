"""Sketches 04 and 05: the org chart drives policy, and denials escalate.

Gate 1: policy generated from a created org gives, for every role, the
verdicts the org chart implies. Owners act on their object types, with the
chain's first approver required. Everyone else is denied.
Gate 2: an org edit shows up as a diff in preview, lands only on apply,
and the apply log records who did it and what changed.
Gate 3: a denied principal escalates, the owner grants with an expiry, the
same authorize() call flips to allowed until the expiry and back after.
A deny closes the escalation without a grant. Every moment is recorded.
Gate 4: packs keep their hand-written policies.yaml. The orgpolicy
endpoints 404 for packs, escalation records go to the data dir, and
nothing is ever written into ironwright/packs/.
"""
import json
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ironwright.policy import PolicyEngine


@pytest.fixture(scope="module")
def packs_before():
    from ironwright.config import PACKS_DIR

    return sorted(str(p) for p in PACKS_DIR.rglob("*"))


@pytest.fixture(scope="module")
def client(tmp_path_factory, packs_before):
    root = tmp_path_factory.mktemp("orgpolicy")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    from ironwright import escalation, orgpolicy
    from ironwright.platform_api import create_platform_app

    app = create_platform_app()
    app.include_router(orgpolicy.router)
    app.include_router(escalation.router)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


def _data() -> Path:
    return Path(os.environ["IRONWRIGHT_DATA"])


def _make_org(client, name: str, size: int = 6, seed: int = 4) -> dict:
    r = client.post("/workspaces", json={"name": name, "size": size, "seed": seed})
    assert r.status_code == 201, r.text
    return client.get(f"/workspaces/{name}/organization").json()


# ------------------------------------------------------------------ gate 1

def test_generated_rules_match_the_org_chart_for_every_role(client):
    from ironwright import orgpolicy

    org = _make_org(client, "harbor-pilot")

    # Deterministic: the same org gives byte-identical output.
    a = yaml.safe_dump(orgpolicy.generate(org), sort_keys=True).encode()
    b = yaml.safe_dump(orgpolicy.generate(json.loads(json.dumps(org))), sort_keys=True).encode()
    assert a == b

    spec = orgpolicy.generate(org)
    assert spec["default"] == "deny"
    engine = PolicyEngine(spec)
    owners = org["data_owners"]
    chains = {c["action"]: c["chain"] for c in org["approver_chains"]}
    role_ids = [r["id"] for r in org["roles"]]
    assert len(role_ids) > 8

    checked = 0
    for action in sorted(chains):
        obj = orgpolicy.object_for_action(org, action)
        assert obj, f"no object type derivable for {action}"
        owner = owners[obj]
        first = chains[action][0]
        for rid in role_ids:
            principal = f"role:{rid}"
            # Owner permitted with the chain's first approver, everyone else denied.
            with_approval = engine.authorize(
                principal, f"action:{action}", {"approved_by": f"role:{first}"})
            assert with_approval.allowed == (rid == owner), (rid, action)
            # Approval is required: without it nobody passes, owner included.
            bare = engine.authorize(principal, f"action:{action}", {})
            assert bare.allowed is False, (rid, action)
        # The wrong approver does not count.
        wrong = engine.authorize(f"role:{owner}", f"action:{action}",
                                 {"approved_by": "role:somebody_else"})
        assert wrong.allowed is False, action
        checked += 1
    assert checked >= 4


# ------------------------------------------------------------------ gate 2

def test_org_edit_previews_a_diff_and_applies_only_on_apply(client):
    name = "harbor-pilot"
    gen_file = _data() / name / "policies.generated.yaml"
    log_file = _data() / name / "policies.generated.log.jsonl"

    # First preview: everything is an addition, nothing applied yet.
    pre = client.get(f"/workspaces/{name}/orgpolicy/preview")
    assert pre.status_code == 200, pre.text
    d = pre.json()["diff"]
    assert d["added"] and not d["removed"] and not d["unchanged"]
    assert not gen_file.exists(), "preview must never write"

    r = client.post(f"/workspaces/{name}/orgpolicy/apply",
                    json={"approved_by": "user:console"})
    assert r.status_code == 200, r.text
    assert gen_file.exists()
    assert log_file.exists()

    # Preview after apply: all unchanged.
    d2 = client.get(f"/workspaces/{name}/orgpolicy/preview").json()["diff"]
    assert d2["unchanged"] and not d2["added"] and not d2["removed"]

    # Edit the org: item gets a new owner, and delist_item loses its chain.
    org_path = _data() / name / "org.json"
    org = json.loads(org_path.read_text())
    old_owner = org["data_owners"]["item"]
    assert old_owner != "cfo"
    org["data_owners"]["item"] = "cfo"
    org["approver_chains"] = [c for c in org["approver_chains"]
                              if c["action"] != "delist_item"]
    org_path.write_text(json.dumps(org, indent=1, sort_keys=True))

    before = gen_file.read_bytes()
    pv = client.get(f"/workspaces/{name}/orgpolicy/preview").json()
    assert gen_file.read_bytes() == before, "preview applied the diff; it must not"

    added = {r["id"]: r for r in pv["diff"]["added"]}
    removed = {r["id"]: r for r in pv["diff"]["removed"]}
    # The owner change rewrites the permit for that action.
    assert added["own-item-delist_item"]["principal"] == "role:cfo"
    assert removed["own-item-delist_item"]["principal"] == f"role:{old_owner}"
    # Irreversible and no chain means an outright forbid now.
    assert added["forbid-delist_item"]["effect"] == "forbid"
    assert "when" not in added["forbid-delist_item"]
    assert pv["diff"]["unchanged"], "untouched rules must survive as unchanged"

    r2 = client.post(f"/workspaces/{name}/orgpolicy/apply",
                     json={"approved_by": "user:governance.lead"})
    assert r2.status_code == 200
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[-1]["actor"] == "user:governance.lead"
    assert "own-item-delist_item" in lines[-1]["added"]
    assert "forbid-delist_item" in lines[-1]["added"]

    # Verdict after apply: irreversible without a chain is forbidden for all.
    from ironwright import orgpolicy
    engine = PolicyEngine({"rules": orgpolicy.applied_rules(name)})
    v = engine.authorize("role:cfo", "action:delist_item",
                        {"approved_by": "role:cmo"})
    assert v.allowed is False
    assert "forbid-delist_item" in v.reason


# ------------------------------------------------------------------ gate 3

def test_escalation_grant_expiry_and_deny(client):
    from ironwright import orgpolicy

    name = "mercury-ops"
    org = _make_org(client, name, seed=9)
    client.post(f"/workspaces/{name}/orgpolicy/apply",
                json={"approved_by": "user:console"})

    def verdict(now: str):
        engine = PolicyEngine({"rules": orgpolicy.applied_rules(name)})
        return engine.authorize(
            "user:analyst.7", "action:delist_item",
            {"params": {"object_id": "ITM-000123"}, "now": now})

    # Moment 1, the deny: no rule permits this principal.
    assert verdict("2026-10-20T09:00:00").allowed is False

    # Moment 2, the escalation, addressed to the owning role.
    esc = client.post(f"/workspaces/{name}/escalations", json={
        "principal": "user:analyst.7", "action": "delist_item",
        "object_id": "ITM-000123", "denying_rule": "default-deny"})
    assert esc.status_code == 201, esc.text
    esc = esc.json()
    assert esc["owner_role"] == "role:" + org["data_owners"]["item"]
    assert esc["status"] == "pending"
    # Org workspaces have no ontology store: the JSONL log is the record.
    assert esc["recorded"] == "jsonl"
    assert "the record" in esc["note"]

    pending = client.get(f"/workspaces/{name}/escalations").json()
    assert [p["id"] for p in pending] == [esc["id"]]

    # Moment 3, the grant, with an expiry the engine itself enforces.
    dec = client.post(f"/workspaces/{name}/escalations/{esc['id']}/decide",
                      json={"decision": "grant", "decided_by": esc["owner_role"],
                            "expires": "2026-11-01"})
    assert dec.status_code == 200, dec.text
    rule = dec.json()["rule"]
    assert rule["principal"] == "user:analyst.7"
    assert rule["action"] == "action:delist_item"
    assert "params.object_id == 'ITM-000123'" in rule["when"]
    assert "now < '2026-11-01'" in rule["when"]
    assert rule["expires"] == "2026-11-01"

    # The SAME authorize call now passes.
    assert verdict("2026-10-20T09:00:00").allowed is True
    # The grant is narrow: another object or another principal stays denied.
    engine = PolicyEngine({"rules": orgpolicy.applied_rules(name)})
    env = {"params": {"object_id": "ITM-OTHER"}, "now": "2026-10-20T09:00:00"}
    assert engine.authorize("user:analyst.7", "action:delist_item", env).allowed is False
    env = {"params": {"object_id": "ITM-000123"}, "now": "2026-10-20T09:00:00"}
    assert engine.authorize("user:analyst.9", "action:delist_item", env).allowed is False
    # Moment 4, the expiry: the when clause fails once now passes the date.
    assert verdict("2026-11-02T00:00:00").allowed is False
    # No clock in the env fails closed.
    engine = PolicyEngine({"rules": orgpolicy.applied_rules(name)})
    no_clock = engine.authorize("user:analyst.7", "action:delist_item",
                                {"params": {"object_id": "ITM-000123"}})
    assert no_clock.allowed is False
    # The loader also drops expired grants outright.
    active_now = orgpolicy.active_rules(name, now="2026-10-20T09:00:00")
    assert any(r["id"] == esc["id"] for r in active_now)
    active_later = orgpolicy.active_rules(name, now="2026-11-02T00:00:00")
    assert all(r["id"] != esc["id"] for r in active_later)

    # A regeneration apply keeps the grant alongside the generated rules.
    client.post(f"/workspaces/{name}/orgpolicy/apply",
                json={"approved_by": "user:console"})
    assert any(r["id"] == esc["id"] for r in orgpolicy.applied_rules(name))

    # The deny path closes without a grant.
    esc2 = client.post(f"/workspaces/{name}/escalations", json={
        "principal": "user:analyst.7", "action": "delist_item",
        "object_id": "ITM-000456", "denying_rule": "default-deny"}).json()
    den = client.post(f"/workspaces/{name}/escalations/{esc2['id']}/decide",
                      json={"decision": "deny", "decided_by": esc["owner_role"],
                            "reason": "not needed this quarter"})
    assert den.status_code == 200
    assert den.json()["status"] == "denied"
    assert client.get(f"/workspaces/{name}/escalations").json() == []
    assert all(r["id"] != esc2["id"] for r in orgpolicy.applied_rules(name))

    # Deciding twice is a conflict, not a second grant.
    again = client.post(f"/workspaces/{name}/escalations/{esc['id']}/decide",
                        json={"decision": "deny", "decided_by": esc["owner_role"]})
    assert again.status_code == 409

    # Every moment is on the append-only record.
    lines = [json.loads(line) for line in
             (_data() / name / "escalations.jsonl").read_text().splitlines()]
    assert [ln["moment"] for ln in lines] == ["escalated", "granted", "escalated", "denied"]
    assert lines[0]["denying_rule"] == "default-deny"
    assert lines[1]["expires"] == "2026-11-01"
    assert lines[3]["reason"] == "not needed this quarter"


def test_escalation_input_guardrails(client):
    name = "mercury-ops"
    bad = client.post(f"/workspaces/{name}/escalations", json={
        "principal": "user:analyst.7", "action": "delist_item",
        "object_id": "x' or '1'=='1", "denying_rule": "default-deny"})
    assert bad.status_code == 422
    missing = client.post(f"/workspaces/{name}/escalations", json={
        "principal": "user:analyst.7", "action": "no_such_thing_here",
        "object_id": "ITM-1", "denying_rule": "default-deny"})
    assert missing.status_code in (201, 422)  # underivable owner is a 422
    nowhere = client.post("/workspaces/never-existed/escalations", json={
        "principal": "user:x", "action": "delist_item",
        "object_id": "ITM-1", "denying_rule": "r"})
    assert nowhere.status_code == 404
    ghost = client.post(f"/workspaces/{name}/escalations/esc-9999/decide",
                        json={"decision": "deny", "decided_by": "role:cmo"})
    assert ghost.status_code == 404
    esc = client.post(f"/workspaces/{name}/escalations", json={
        "principal": "user:analyst.8", "action": "onboard_vendor",
        "object_id": "VEN-1", "denying_rule": "default-deny"}).json()
    no_expiry = client.post(f"/workspaces/{name}/escalations/{esc['id']}/decide",
                            json={"decision": "grant", "decided_by": "role:cmo"})
    assert no_expiry.status_code == 422
    odd = client.post(f"/workspaces/{name}/escalations/{esc['id']}/decide",
                      json={"decision": "maybe", "decided_by": "role:cmo"})
    assert odd.status_code == 422


# ------------------------------------------------------------------ gate 4

def test_packs_keep_hand_written_policy_and_stay_untouched(client, packs_before):
    from ironwright import orgpolicy
    from ironwright.config import PACKS_DIR, PackPaths
    from ironwright.ontology.store import OntologyStore

    # The org-to-policy surface is for organizations only.
    assert client.get("/workspaces/retail/orgpolicy/preview").status_code == 404
    assert client.post("/workspaces/retail/orgpolicy/apply",
                       json={"approved_by": "user:console"}).status_code == 404

    # A pack escalation records to the data dir and to the audit chain.
    db = PackPaths("retail").ontology_db
    db.parent.mkdir(parents=True, exist_ok=True)
    OntologyStore(db).con.close()
    esc = client.post("/workspaces/retail/escalations", json={
        "principal": "agent:retail-triage", "action": "delist_item",
        "object_id": "ITM-11111111", "denying_rule": "no-agent-delisting"})
    assert esc.status_code == 201, esc.text
    esc = esc.json()
    assert esc["owner_role"] == "role:merchandising"  # Item owner in the pack ontology
    assert esc["recorded"] == "jsonl+audit"
    assert (PackPaths("retail").reports / "escalations.jsonl").exists()

    dec = client.post(f"/workspaces/retail/escalations/{esc['id']}/decide",
                      json={"decision": "grant", "decided_by": "role:merchandising",
                            "expires": "2026-11-01"})
    assert dec.status_code == 200, dec.text

    # The grant landed in the data dir, next to the run artifacts.
    assert (_data() / "retail" / "policies.generated.yaml").exists()

    # A hand-written forbid still beats a generated permit, loudly.
    engine = PolicyEngine({"rules": orgpolicy.combined_rules(
        "retail", now="2026-10-20T00:00:00")})
    v = engine.authorize("agent:retail-triage", "action:delist_item",
                        {"params": {"object_id": "ITM-11111111"},
                         "now": "2026-10-20T00:00:00"})
    assert v.allowed is False
    assert "no-agent-delisting" in v.reason

    # Both moments are audit rows and the chain still verifies.
    store = OntologyStore(db)
    try:
        rows = store.con.execute(
            "select action, status from audit_log order by seq").fetchall()
        assert ("escalate:delist_item", "escalated") in rows
        assert ("escalate:delist_item", "escalation_granted") in rows
        assert store.verify_audit_chain() is True
    finally:
        store.con.close()

    # Nothing in this whole module ever landed in ironwright/packs/.
    assert sorted(str(p) for p in PACKS_DIR.rglob("*")) == packs_before
