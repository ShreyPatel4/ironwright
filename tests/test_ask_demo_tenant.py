"""Ask on a demo pack, for a signed up org. A demo pack is the one thing two orgs share,
so the point of every test here is what the shared folder does not learn.

One real retail run, then real routes. The only shortcut is the run itself: tenants cannot
run a demo pack, so a dev open server makes the data they read.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ironwright import config
from ironwright.ontology.store import OntologyStore
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import quotas, service, usage

DEMO = "retail"
ANALYSTS_ONLY = "banking_aml"  # its policies.yaml lets only user:aml.analyst* read
PASSWORD = "correct-horse-9"
A_EMAIL = "canary-alpha@a.example"
CANARY = "zebrafish-canary"


def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


@pytest.fixture(scope="module")
def demo_root(tmp_path_factory) -> Path:
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("demo-ask")
    mp.setenv("IRONWRIGHT_DATA", str(root))
    mp.setenv("IRONWRIGHT_DEV_OPEN", "1")
    mp.delenv("IRONWRIGHT_DB_URL", raising=False)
    with TestClient(create_platform_app()) as c:
        for pack in (DEMO, ANALYSTS_ONLY):
            assert c.post(f"/workspaces/{pack}/runs", json={"seed": 7}).status_code == 200
    mp.undo()
    return root


@pytest.fixture()
def world(demo_root, monkeypatch):
    """Every test gets fresh orgs on a shared run, with a new metadata database."""
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(demo_root))
    monkeypatch.setenv("IRONWRIGHT_DB_URL", f"sqlite:///{demo_root}/meta-{uuid.uuid4().hex}.sqlite")
    with TestClient(create_platform_app()) as c:
        a = c.post("/auth/signup", json={"email": A_EMAIL, "password": PASSWORD, "org_name": "Canary Alpha"}).json()
        b = c.post("/auth/signup", json={"email": "b@b.example", "password": PASSWORD, "org_name": "Beta org"}).json()
        yield c, a, b


def invited(c: TestClient, owner: dict, role: str) -> dict:
    made = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                  json={"email": f"{role}@a.example", "role": role})
    assert made.status_code == 201, made.text
    return c.post("/auth/accept-invite", json={"token": made.json()["token"], "password": PASSWORD}).json()


def a_question(c: TestClient, who: dict) -> str:
    got = c.get(f"/workspaces/{DEMO}/ask/examples", headers=bearer(who))
    assert got.status_code == 200, got.text
    assert got.json(), "retail offers no question"
    return got.json()[0]["question"]


def asks(c: TestClient, who: dict, question: str) -> dict:
    r = c.post(f"/workspaces/{DEMO}/ask", headers=bearer(who), json={"question": question})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_signed_up_owner_asks_a_demo_and_gets_a_grounded_answer(world):
    c, a, _ = world
    entry = asks(c, a, a_question(c, a))
    assert entry["status"] == "answered", entry
    assert entry["citations"]
    assert set(entry["citations"]) <= {call["evidence_id"] for call in entry["calls"]}
    assert f"[ev:{entry['citations'][0]}]" in entry["answer"]
    assert entry["verification"]["level"] == "L3"


def test_a_member_asks_a_demo(world):
    c, a, _ = world
    member = invited(c, a, "member")
    assert asks(c, member, a_question(c, a))["status"] == "answered"


def test_a_viewer_may_not_ask_a_demo_as_on_any_workspace(world):
    c, a, _ = world
    viewer = invited(c, a, "viewer")
    question = a_question(c, a)
    r = c.post(f"/workspaces/{DEMO}/ask", headers=bearer(viewer), json={"question": question})
    assert r.status_code == 403
    assert "role" in r.json()["detail"]
    assert usage.used(a["org_id"], "ask", time.time()) == 0


def test_actions_on_a_demo_stay_refused(world):
    c, a, _ = world
    for path in (f"/workspaces/{DEMO}/runs", f"/workspaces/{DEMO}/actions/delist_item",
                 f"/workspaces/{DEMO}/approvals/1/decide", f"/workspaces/{DEMO}/boards/any/run"):
        r = c.post(path, headers=bearer(a), json={})
        assert r.status_code == 403, path
        assert "read only" in r.json()["detail"]


def test_a_question_cannot_become_an_action(world):
    c, a, _ = world
    entry = asks(c, a, "delist item and create replenishment order and close everything")
    assert entry["status"] == "refused"
    assert not c.get(f"/workspaces/{DEMO}/approvals", headers=bearer(a)).json()


def test_a_question_lands_in_the_log_of_the_org_that_asked(world):
    c, a, b = world
    question = a_question(c, a)
    asks(c, a, question)
    mine = c.get(f"/workspaces/{DEMO}/ask/log", headers=bearer(a)).json()
    assert [e["question"] for e in mine] == [question]
    assert c.get(f"/workspaces/{DEMO}/ask/log", headers=bearer(b)).json() == []
    assert c.get(f"/workspaces/{DEMO}/ask/metrics", headers=bearer(a)).json()["asked"] == 1
    assert c.get(f"/workspaces/{DEMO}/ask/metrics", headers=bearer(b)).json()["asked"] == 0


def test_the_log_is_kept_under_the_org_and_owner_only(world):
    c, a, _ = world
    asks(c, a, f"what about {CANARY}")
    path = config.demo_ask_log(a["org_id"], DEMO)
    assert path.is_relative_to(config.tenant_dir(a["org_id"]))
    assert CANARY in path.read_text()
    assert path.stat().st_mode & 0o077 == 0


def test_the_shared_pack_folder_learns_nothing_from_a_question(world):
    c, a, _ = world
    root = load_pack(DEMO).paths.root
    entry = asks(c, a, a_question(c, a))
    asks(c, a, f"what about {CANARY}")
    c.post(f"/workspaces/{DEMO}/ask/replay", headers=bearer(a), json={"index": 0})
    assert entry["status"] == "answered"
    needles = (CANARY, a["org_id"], a["user_id"], A_EMAIL, a["org_slug"])
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".jsonl", ".json", ".log", ".txt"):
            text = f.read_text(errors="replace")
            for needle in needles:
                assert needle not in text, f"{f} holds {needle}"


def test_org_b_sees_no_trace_of_what_org_a_asked(world):
    c, a, b = world
    question = a_question(c, a)
    asks(c, a, question)
    asks(c, a, f"what about {CANARY}")
    canaries = (CANARY, a["org_id"], a["user_id"], a["org_slug"], A_EMAIL)
    for path in ("ask/log", "ask/metrics", "ask/examples", "traces"):
        text = c.get(f"/workspaces/{DEMO}/{path}", headers=bearer(b)).text
        for canary in canaries:
            assert canary not in text, f"{path} shows B {canary}"
    for trace in c.get(f"/workspaces/{DEMO}/traces", headers=bearer(b)).json():
        text = c.get(f"/workspaces/{DEMO}/traces/{trace['trace_id']}", headers=bearer(b)).text
        for canary in canaries:
            assert canary not in text, f"trace {trace['trace_id']} shows B {canary}"


def test_replay_reads_the_asking_org_only(world):
    c, a, b = world
    asks(c, a, a_question(c, a))
    mine = c.post(f"/workspaces/{DEMO}/ask/replay", headers=bearer(a), json={"index": 0})
    assert (mine.json()["replayable"], mine.json()["match"]) == (True, True)
    assert c.post(f"/workspaces/{DEMO}/ask/replay", headers=bearer(b), json={"index": 0}).status_code == 404


def test_a_demo_question_spends_the_quota_of_the_org_that_asked(world):
    c, a, b = world
    asks(c, a, a_question(c, a))
    now = time.time()
    assert usage.used(a["org_id"], "ask", now) == 1
    assert usage.used(b["org_id"], "ask", now) == 0


def test_a_demo_question_past_the_limit_is_a_429_and_logs_nothing(world, monkeypatch):
    c, a, _ = world
    monkeypatch.setitem(quotas.PLANS, "free", {**quotas.PLANS["free"], "ask": 1})
    question = a_question(c, a)
    asks(c, a, question)
    over = c.post(f"/workspaces/{DEMO}/ask", headers=bearer(a), json={"question": question})
    assert over.status_code == 429
    assert len(c.get(f"/workspaces/{DEMO}/ask/log", headers=bearer(a)).json()) == 1


def test_removing_an_org_removes_its_demo_questions(world):
    c, a, _ = world
    asks(c, a, a_question(c, a))
    path = config.demo_ask_log(a["org_id"], DEMO)
    assert path.exists()
    service._remove_files(a["org_id"])
    assert not path.exists()


def test_a_pack_policy_still_decides_who_may_read_it(world):
    """The gate opens, and the pack's own default deny still rules. A tenant is no analyst."""
    c, a, _ = world
    store = OntologyStore(load_pack(ANALYSTS_ONLY).paths.ontology_db)
    alert = store.con.execute("select id from objects where type = 'Alert' order by id limit 1").fetchone()[0]
    store.con.close()
    assert c.get(f"/workspaces/{ANALYSTS_ONLY}/ask/examples", headers=bearer(a)).json() == []
    r = c.post(f"/workspaces/{ANALYSTS_ONLY}/ask", headers=bearer(a),
               json={"question": f"the alert, the account and customer profile behind alert {alert}"})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["reason_code"]) == ("refused", "policy_denied")


def test_the_listing_says_a_demo_is_read_only_for_a_tenant(world):
    c, a, _ = world
    listed = {w["name"]: w for w in c.get("/workspaces", headers=bearer(a)).json()}
    assert listed[DEMO]["read_only"] is True


def test_the_listing_leaves_a_dev_server_free_to_run(demo_root, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    monkeypatch.setenv("IRONWRIGHT_DATA", str(demo_root))
    monkeypatch.setenv("IRONWRIGHT_DB_URL", f"sqlite:///{demo_root}/meta-dev.sqlite")
    with TestClient(create_platform_app()) as c:
        listed = {w["name"]: w for w in c.get("/workspaces").json()}
    assert listed[DEMO]["read_only"] is False


def test_a_dev_server_keeps_the_pack_log(demo_root, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    monkeypatch.setenv("IRONWRIGHT_DATA", str(demo_root))
    monkeypatch.setenv("IRONWRIGHT_DB_URL", f"sqlite:///{demo_root}/meta-dev2.sqlite")
    log = load_pack(DEMO).paths.reports / "ask_log.jsonl"
    before = log.read_text().count("\n") if log.exists() else 0
    with TestClient(create_platform_app()) as c:
        assert c.post(f"/workspaces/{DEMO}/ask", json={"question": "what about nothing"}).status_code == 200
    assert log.read_text().count("\n") == before + 1
    assert json.loads(log.read_text().splitlines()[-1])["status"] == "refused"
