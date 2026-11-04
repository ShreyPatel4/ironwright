"""The journey over HTTP, for a tenant: profile, proposal, create, then Ask.

Every test goes through the API the console calls. Where a test builds something
behind the API, it says why. meta_db runs each test on SQLite and on Postgres.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from ironwright import config, orgpolicy
from ironwright import workspaces_tenant as wt
from ironwright.config import data_root
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.ontology import propose as prop
from ironwright.ontology.runtime import Ontology
from ironwright.platform_api import NOT_FOUND, create_platform_app

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "tenant_ws"
JOIN = HERE / "fixtures" / "ingest" / "join"
PASSWORD = "correct-horse-9"
SLUG = "acme-claims"
GONE = {"detail": NOT_FOUND}
ASK_READS = ["ask/log", "ask/metrics", "ask/examples"]
A_GHOST = "0" * 31 + "1"


def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


def signup(c: TestClient, label: str) -> dict:
    r = c.post("/auth/signup", json={"email": f"{label}@example.com", "password": PASSWORD,
                                     "org_name": f"Org {label}"})
    assert r.status_code == 201, r.text
    return r.json()


def join_with_role(c: TestClient, owner: dict, label: str, role: str) -> dict:
    email = f"{label}@example.com"
    made = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner), json={"email": email, "role": role})
    assert made.status_code == 201, made.text
    got = c.post("/auth/accept-invite", json={"token": made.json()["token"], "password": PASSWORD})
    assert got.status_code in (200, 201), got.text
    return got.json()


@pytest.fixture()
def client(meta_db):
    with TestClient(create_platform_app()) as c:
        yield c


@pytest.fixture()
def orgs(client):
    return client, signup(client, "alpha"), signup(client, "beta")


def post_upload(c: TestClient, who: dict) -> str:
    files = [("files", (f.name, f.read_bytes(), "text/csv")) for f in sorted(JOIN.glob("*.csv"))]
    made = c.post("/uploads", headers=bearer(who), files=files)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def work(org_id: str, job_id: str) -> dict:
    Worker(JobStore()).run_once(job_id)
    got = JobStore().get(org_id, job_id)
    assert got["state"] == "succeeded", got["error"]
    return got


def profiled(c: TestClient, who: dict) -> tuple[str, dict]:
    upload_id = post_upload(c, who)
    r = c.post(f"/uploads/{upload_id}/profile", headers=bearer(who))
    assert r.status_code == 202, r.text
    work(who["org_id"], r.json()["id"])
    got = c.get(f"/uploads/{upload_id}/proposal", headers=bearer(who))
    assert got.status_code == 200, got.text
    return upload_id, got.json()


def fixture_workspace(org_id: str, slug: str = SLUG, policies: dict | None = None, run: bool = True) -> None:
    """T2's fixture, hand written ontology and all. Its upload id is not a real one,
    so no route can make it. That is why it goes in behind the API."""
    dest = config.uploads_root(org_id) / "fixture"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE / "uploads" / "fixture", dest, dirs_exist_ok=True)
    models = {layer: {f.name: f.read_text() for f in sorted((FIXTURE / "models" / layer).glob("*.sql"))}
              for layer in ("silver", "gold")}
    wt.create_workspace(org_id, slug, yaml.safe_load((FIXTURE / "workspace.yaml").read_text()), models=models,
                        ontology=yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
                        policies=policies if policies is not None else yaml.safe_load((FIXTURE / "policies.yaml").read_text()))
    if run:
        assert wt.run_workspace(org_id, slug)["passed"] is True


def a_question(c: TestClient, who: dict, slug: str = SLUG) -> str:
    got = c.get(f"/workspaces/{slug}/ask/examples", headers=bearer(who))
    assert got.status_code == 200, got.text
    assert got.json(), "the workspace offers no question"
    return got.json()[0]["question"]


def asks(c: TestClient, who: dict, question: str, slug: str = SLUG) -> dict:
    r = c.post(f"/workspaces/{slug}/ask", headers=bearer(who), json={"question": question})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- profile

def test_a_new_org_profiles_an_upload_before_any_workspace_exists(orgs):
    c, a, _ = orgs
    _, proposal = profiled(c, a)
    assert {s["name"] for s in proposal["workspace"]["sources"]} == {"customers", "orders"}
    assert wt.list_workspaces(a["org_id"]) == []


def test_the_proposal_gives_a_reason_for_each_decision(orgs):
    c, a, _ = orgs
    _, proposal = profiled(c, a)
    assert proposal["decisions"]
    assert all(d["reason"] for d in proposal["decisions"])


def test_the_profile_job_is_a_job_its_org_can_read(orgs):
    c, a, _ = orgs
    upload_id = post_upload(c, a)
    job_id = c.post(f"/uploads/{upload_id}/profile", headers=bearer(a)).json()["id"]
    got = c.get(f"/jobs/{job_id}", headers=bearer(a)).json()
    assert (got["kind"], got["state"]) == ("profile", "queued")


def test_another_orgs_upload_reads_like_no_upload_and_queues_nothing(orgs):
    c, a, b = orgs
    upload_id = post_upload(c, a)
    theirs = c.post(f"/uploads/{upload_id}/profile", headers=bearer(b))
    nobody = c.post(f"/uploads/{A_GHOST}/profile", headers=bearer(b))
    assert (theirs.status_code, theirs.json()) == (nobody.status_code, nobody.json()) == (404, GONE)
    assert c.get("/jobs", headers=bearer(a)).json() == []
    assert c.get("/jobs", headers=bearer(b)).json() == []


@pytest.mark.parametrize("bad", ["not-an-id", "a.b", "A" * 32])
def test_a_bad_upload_id_reads_like_no_upload(orgs, bad):
    c, a, _ = orgs
    r = c.post(f"/uploads/{bad}/profile", headers=bearer(a))
    assert (r.status_code, r.json()) == (404, GONE)


def test_org_b_cannot_read_a_proposal_of_org_a(orgs):
    c, a, b = orgs
    upload_id, _ = profiled(c, a)
    r = c.get(f"/uploads/{upload_id}/proposal", headers=bearer(b))
    assert (r.status_code, r.json()) == (404, GONE)


def test_a_member_may_profile_what_a_member_may_upload(orgs):
    c, a, _ = orgs
    member = join_with_role(c, a, "member", "member")
    upload_id = post_upload(c, member)
    assert c.post(f"/uploads/{upload_id}/profile", headers=bearer(member)).status_code == 202


def test_a_viewer_may_not_profile(orgs):
    c, a, _ = orgs
    upload_id = post_upload(c, a)
    viewer = join_with_role(c, a, "viewer", "viewer")
    assert c.post(f"/uploads/{upload_id}/profile", headers=bearer(viewer)).status_code == 403


# ---------------------------------------------------------------- create

def create(c: TestClient, who: dict, upload_id: str, spec: dict, slug: str = SLUG, **more):
    return c.post("/workspaces", headers=bearer(who),
                  json={"from_upload": upload_id, "spec": spec, "slug": slug, **more})


def test_an_approved_proposal_becomes_a_workspace_the_org_lists(orgs):
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    r = create(c, a, upload_id, proposal["workspace"])
    assert r.status_code == 201, r.text
    assert (r.json()["name"], r.json()["kind"]) == (SLUG, "workspace")
    assert r.json()["verification"]["level"] == "L1"
    listed = {w["name"]: w for w in c.get("/workspaces", headers=bearer(a)).json()}
    assert listed[SLUG]["kind"] == "workspace"
    assert listed[SLUG]["title"] == proposal["workspace"]["title"]
    assert listed[SLUG]["verification"]["level"] == "L1"


def test_org_b_does_not_list_the_workspace_of_org_a(orgs):
    c, a, b = orgs
    upload_id, proposal = profiled(c, a)
    assert create(c, a, upload_id, proposal["workspace"]).status_code == 201
    assert SLUG not in {w["name"] for w in c.get("/workspaces", headers=bearer(b)).json()}


def test_org_b_cannot_build_on_the_upload_of_org_a(orgs):
    c, a, b = orgs
    upload_id, proposal = profiled(c, a)
    r = create(c, b, upload_id, proposal["workspace"])
    assert (r.status_code, r.json()) == (404, GONE)
    assert wt.list_workspaces(b["org_id"]) == []


def test_a_spec_cannot_reach_past_the_upload_it_names(orgs):
    c, a, _ = orgs
    first, proposal = profiled(c, a)
    second = post_upload(c, a)
    r = create(c, a, second, proposal["workspace"])
    assert r.status_code == 422, r.text
    assert "from_upload" in r.json()["detail"]
    assert first not in r.json()["detail"]
    assert wt.list_workspaces(a["org_id"]) == []


def test_a_bad_spec_is_refused_and_leaves_nothing(orgs):
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    bad = {**proposal["workspace"], "kind": "pack"}
    assert create(c, a, upload_id, bad).status_code == 422
    assert not config.workspace_dir(a["org_id"], SLUG).exists()


def test_a_spec_that_is_not_shaped_like_one_is_a_422_and_not_a_500(orgs):
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    assert create(c, a, upload_id, {**proposal["workspace"], "sources": ["nope"]}).status_code == 422
    assert create(c, a, upload_id, {**proposal["workspace"], "sources": "nope"}).status_code == 422


def test_a_taken_slug_is_refused(orgs):
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    assert create(c, a, upload_id, proposal["workspace"]).status_code == 201
    assert create(c, a, upload_id, proposal["workspace"]).status_code == 422


def test_two_orgs_can_use_one_slug(orgs):
    c, a, b = orgs
    for who in (a, b):
        upload_id, proposal = profiled(c, who)
        assert create(c, who, upload_id, proposal["workspace"]).status_code == 201


def test_a_demo_pack_name_is_refused(orgs):
    """The gate answers for the pack first, so this workspace could never be reached."""
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    r = create(c, a, upload_id, proposal["workspace"], slug="retail")
    assert r.status_code == 422, r.text
    assert not config.workspace_dir(a["org_id"], "retail").exists()


def test_a_slug_cannot_take_the_name_of_a_scenario_folder_of_another_org(orgs):
    """The gate lets an org through on a name it holds a folder for. A slug spelled like
    org A's scenario folder would open /organization, orgpolicy and delete on A's data."""
    c, a, b = orgs
    made = c.post("/workspaces", headers=bearer(a), json={"name": "north", "size": 4, "seed": 3})
    assert made.status_code == 201, made.text
    target = made.json()["name"]
    upload_id, proposal = profiled(c, b)
    r = create(c, b, upload_id, proposal["workspace"], slug=target)
    assert r.status_code == 422, r.text
    assert not config.workspace_dir(b["org_id"], target).exists()
    assert c.get(f"/workspaces/{target}/organization", headers=bearer(b)).status_code == 404


def test_the_refusal_does_not_say_whether_the_scenario_folder_exists(orgs):
    c, a, b = orgs
    target = c.post("/workspaces", headers=bearer(a), json={"name": "north", "size": 4, "seed": 3}).json()["name"]
    upload_id, proposal = profiled(c, b)
    taken = create(c, b, upload_id, proposal["workspace"], slug=target)
    free = create(c, b, upload_id, proposal["workspace"], slug=f"{target[:9]}-free")
    assert (taken.status_code, taken.json()) == (free.status_code, free.json())


def test_a_member_cannot_create_a_workspace(orgs):
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    member = join_with_role(c, a, "member", "member")
    assert create(c, member, upload_id, proposal["workspace"]).status_code == 403


def test_silver_models_come_along(orgs):
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    models = {"silver": {"01_customers.sql": "select * from bronze_customers"}}
    assert create(c, a, upload_id, proposal["workspace"], models=models).status_code == 201
    assert [name for name, _ in wt.load_workspace(a["org_id"], SLUG).models("silver")]


def test_a_body_with_a_spec_and_no_from_upload_does_not_make_a_scenario_org(orgs):
    """POST /workspaces also makes scenario orgs, whose body ignored unknown keys. A client
    sending a spec under the wrong names would get a fake store chain and a 201."""
    c, a, _ = orgs
    _, proposal = profiled(c, a)
    r = c.post("/workspaces", headers=bearer(a), json={"name": SLUG, "spec": proposal["workspace"]})
    assert r.status_code == 422, r.text
    assert SLUG not in {w["name"] for w in c.get("/workspaces", headers=bearer(a)).json()}


def test_a_scenario_org_can_still_be_made(orgs):
    c, a, _ = orgs
    r = c.post("/workspaces", headers=bearer(a), json={"name": "north", "size": 4, "seed": 3})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "organization"


# ---------------------------------------------------------------- Ask

def test_ask_on_the_fixture_workspace_answers_with_evidence_ids(orgs):
    c, a, _ = orgs
    fixture_workspace(a["org_id"])
    entry = asks(c, a, a_question(c, a))
    assert entry["status"] == "answered", entry
    gathered = {call["evidence_id"] for call in entry["calls"]}
    assert entry["citations"]
    assert set(entry["citations"]) <= gathered
    assert f"[ev:{entry['citations'][0]}]" in entry["answer"]
    assert entry["verification"]["level"] == "L1"


@pytest.mark.parametrize("path", ASK_READS)
def test_org_b_reading_ask_on_a_workspace_of_org_a_gets_404(orgs, path):
    c, a, b = orgs
    fixture_workspace(a["org_id"], run=False)
    r = c.get(f"/workspaces/{SLUG}/{path}", headers=bearer(b))
    assert (r.status_code, r.json()) == (404, GONE)


def test_org_b_asking_against_a_workspace_of_org_a_gets_404(orgs):
    c, a, b = orgs
    fixture_workspace(a["org_id"])
    question = a_question(c, a)
    r = c.post(f"/workspaces/{SLUG}/ask", headers=bearer(b), json={"question": question})
    assert (r.status_code, r.json()) == (404, GONE)
    assert c.get(f"/workspaces/{SLUG}/ask/log", headers=bearer(a)).json() == []


def test_org_b_cannot_replay_an_answer_of_org_a(orgs):
    c, a, b = orgs
    fixture_workspace(a["org_id"])
    asks(c, a, a_question(c, a))
    r = c.post(f"/workspaces/{SLUG}/ask/replay", headers=bearer(b), json={"index": 0})
    assert (r.status_code, r.json()) == (404, GONE)


def test_log_metrics_and_replay_work_on_a_tenant_workspace(orgs):
    c, a, _ = orgs
    fixture_workspace(a["org_id"])
    question = a_question(c, a)
    asks(c, a, question)
    asks(c, a, "what is the meaning of everything")

    log = c.get(f"/workspaces/{SLUG}/ask/log", headers=bearer(a)).json()
    assert [e["status"] for e in log] == ["answered", "refused"]
    assert log[0]["principal"] == a["principal"]

    metrics = c.get(f"/workspaces/{SLUG}/ask/metrics", headers=bearer(a)).json()
    assert (metrics["asked"], metrics["answered"], metrics["refused"]) == (2, 1, 1)

    replay = c.post(f"/workspaces/{SLUG}/ask/replay", headers=bearer(a), json={"index": 0}).json()
    assert replay["replayable"] is True
    assert replay["match"] is True
    assert c.post(f"/workspaces/{SLUG}/ask/replay", headers=bearer(a), json={"index": 9}).status_code == 404


def test_one_slug_in_two_orgs_keeps_two_logs(orgs):
    c, a, b = orgs
    fixture_workspace(a["org_id"])
    fixture_workspace(b["org_id"])
    question = a_question(c, a)
    asks(c, a, question)
    assert len(c.get(f"/workspaces/{SLUG}/ask/log", headers=bearer(a)).json()) == 1
    assert c.get(f"/workspaces/{SLUG}/ask/log", headers=bearer(b)).json() == []
    assert c.get(f"/workspaces/{SLUG}/ask/metrics", headers=bearer(b)).json()["asked"] == 0


def test_a_workspace_that_has_not_run_refuses_and_does_not_break(orgs):
    c, a, _ = orgs
    fixture_workspace(a["org_id"], run=False)
    assert c.get(f"/workspaces/{SLUG}/ask/examples", headers=bearer(a)).json() == []
    assert asks(c, a, "get one claim by its id CLM-1")["status"] == "refused"


def test_a_scenario_org_has_no_ask(orgs):
    c, a, _ = orgs
    name = c.post("/workspaces", headers=bearer(a), json={"name": "north", "size": 4, "seed": 3}).json()["name"]
    assert c.post(f"/workspaces/{name}/ask", headers=bearer(a), json={"question": "x"}).status_code == 404


def test_a_run_holding_the_store_is_a_409_and_not_a_500(orgs, monkeypatch):
    """The worker is another process in the hosted stack and DuckDB takes a file lock."""
    c, a, _ = orgs
    fixture_workspace(a["org_id"])

    def locked(*_args, **_kw):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr("ironwright.platform_api.Ontology", locked)
    r = c.post(f"/workspaces/{SLUG}/ask", headers=bearer(a), json={"question": "x"})
    assert r.status_code == 409, r.text
    assert "Could not set lock" not in r.text


# ---------------------------------------------------------------- the accepted ontology

def propose(c: TestClient, who: dict, slug: str = SLUG) -> None:
    r = c.post("/jobs", headers=bearer(who), json={"kind": "ontology_propose", "workspace": slug, "params": {}})
    assert r.status_code == 200, r.text
    work(who["org_id"], r.json()["id"])


def accept(c: TestClient, who: dict, edits: list | None = None, slug: str = SLUG) -> None:
    org = who["org_id"]
    proposal = c.get(f"/workspaces/{slug}/ontology/proposal", headers=bearer(who)).json()
    done = c.post(f"/workspaces/{slug}/ontology/accept", headers=bearer(who),
                  json={"proposal_id": proposal["id"], "edits": edits or []})
    assert done.status_code == 200, done.text
    work(org, done.json()["job_id"])


def test_ask_follows_the_version_a_human_accepted(orgs):
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    assert create(c, a, upload_id, proposal["workspace"]).status_code == 201
    r = c.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": SLUG, "params": {}})
    work(a["org_id"], r.json()["id"])
    assert c.get(f"/workspaces/{SLUG}/ask/examples", headers=bearer(a)).json() == []

    propose(c, a)
    accept(c, a)
    names = {q["function"] for q in c.get(f"/workspaces/{SLUG}/ask/examples", headers=bearer(a)).json()}
    assert "get_customer" in names
    assert asks(c, a, a_question(c, a))["status"] == "answered"

    accept(c, a, edits=[{"op": "drop_object", "object": "Customer"}])
    names = {q["function"] for q in c.get(f"/workspaces/{SLUG}/ask/examples", headers=bearer(a)).json()}
    assert "get_customer" not in names
    assert "get_order" in names


@pytest.mark.xfail(strict=True, reason="lake.py: a run id has one second of resolution, so two runs inside a second "
                                       "read as one and every key counts twice. Cross-ticket ask on this PR.")
def test_two_runs_inside_one_second_do_not_lose_the_objects(orgs, monkeypatch):
    """The clock is frozen so this fails every time. Small files really do run this fast."""
    c, a, _ = orgs
    upload_id, proposal = profiled(c, a)
    assert create(c, a, upload_id, proposal["workspace"]).status_code == 201
    monkeypatch.setattr("ironwright.lake.time.strftime", lambda *_: "20261104120000")
    for _ in range(2):
        job_id = c.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": SLUG, "params": {}}).json()["id"]
        work(a["org_id"], job_id)
    propose(c, a)
    assert c.get(f"/workspaces/{SLUG}/ontology/proposal", headers=bearer(a)).json()["objects"]


# ---------------------------------------------------------------- policy

def tenant_engine(who: dict, slug: str = SLUG):
    return orgpolicy.live_engine(slug, pack=wt.load_workspace(who["org_id"], slug))


def test_members_read_by_default_with_no_policy_file(orgs):
    c, a, _ = orgs
    fixture_workspace(a["org_id"], policies={})
    assert tenant_engine(a).authorize(a["principal"], "function:get_claim", {}).allowed
    assert asks(c, a, a_question(c, a))["status"] == "answered"


def test_the_default_permit_never_reaches_an_action(orgs):
    _, a, _ = orgs
    fixture_workspace(a["org_id"], policies={}, run=False)
    engine = tenant_engine(a)
    for action in ("action:close_claim", "action:anything", "ontology.accept", "function"):
        assert not engine.authorize(a["principal"], action, {}).allowed, action


def test_the_default_permit_is_for_people_and_not_agents(orgs):
    _, a, _ = orgs
    fixture_workspace(a["org_id"], policies={}, run=False)
    assert not tenant_engine(a).authorize("agent:triage", "function:get_claim", {}).allowed


def test_a_forbid_the_tenant_wrote_beats_the_default(orgs):
    c, a, _ = orgs
    rule = {"id": "no-claims", "effect": "forbid", "principal": "user:*", "action": "function:get_claim"}
    fixture_workspace(a["org_id"], policies={"default": "deny", "rules": [rule]})
    assert not tenant_engine(a).authorize(a["principal"], "function:get_claim", {}).allowed
    offered = {q["function"] for q in c.get(f"/workspaces/{SLUG}/ask/examples", headers=bearer(a)).json()}
    assert "get_claim" not in offered
    assert "get_policy" in offered
    ont = Ontology(prop.askable(wt.load_workspace(a["org_id"], SLUG)), attach_lake=False)
    claim = ont.store.list("Claim", 1, 0)[0]["_id"]
    ont.store.con.close()
    r = asks(c, a, f"get one claim by its id {claim}")
    assert (r["status"], r.get("reason_code")) == ("refused", "policy_denied"), r


def plant_rules_at(slug: str, rules: list[dict]) -> None:
    """Rules the way orgpolicy writes them for a scenario org with this name."""
    d = data_root() / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "org.json").write_text(json.dumps({"company": "someone else"}))
    (d / orgpolicy.GENERATED_FILE).write_text(yaml.safe_dump({"default": "deny", "rules": rules}))


def test_a_tenant_engine_never_reads_rules_filed_under_the_same_name(orgs):
    """The hazard W2 saw. live_engine(name) reads data_root/<name>, one namespace across
    every org, so the slug alone decided whose rules and grants a tenant's Ask ran on."""
    c, a, _ = orgs
    fixture_workspace(a["org_id"])
    question = a_question(c, a)
    plant_rules_at(SLUG, [{"id": "esc-theirs", "effect": "forbid", "principal": "*", "action": "function:*"},
                          {"id": "esc-grant", "effect": "permit", "principal": "*", "action": "action:*"}])
    engine = tenant_engine(a)
    assert engine.authorize(a["principal"], "function:get_claim", {}).allowed
    assert not engine.authorize(a["principal"], "action:close_claim", {}).allowed
    assert asks(c, a, question)["status"] == "answered"


def test_live_engine_by_name_still_reads_what_it_read(meta_db):
    """Scenario orgs and packs keep the path they had."""
    rule = {"id": "esc-x", "effect": "permit", "principal": "user:x", "action": "action:y"}
    plant_rules_at("north", [rule])
    assert orgpolicy.live_engine("north").authorize("user:x", "action:y", {}).allowed
