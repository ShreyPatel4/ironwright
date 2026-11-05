"""The real journey over HTTP, on a customer's messy exports, with no hand written SQL.

Upload the tenant_ws CSVs, profile them, read the silver draft, create the workspace,
run it, propose an ontology, accept it, then Ask. Routes and the worker only. This is
the walk that dead ended at step 6, because the fixtures shipped their own silver.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ironwright import workspaces_tenant as wt
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.lake import Lakehouse
from ironwright.platform_api import create_platform_app

HERE = Path(__file__).parent
UPLOADS = HERE / "fixtures" / "tenant_ws" / "uploads" / "fixture"
PASSWORD = "correct-horse-9"
SLUG = "acme-claims"


def bearer(who: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {who['token']}"}


def work(org_id: str, job_id: str) -> dict:
    Worker(JobStore()).run_once(job_id)
    got = JobStore().get(org_id, job_id)
    assert got["state"] == "succeeded", got["error"]
    return got


def start(c: TestClient, who: dict, kind: str, slug: str = SLUG) -> None:
    r = c.post("/jobs", headers=bearer(who), json={"kind": kind, "workspace": slug, "params": {}})
    assert r.status_code == 200, r.text
    work(who["org_id"], r.json()["id"])


@pytest.fixture()
def client(meta_db):
    with TestClient(create_platform_app()) as c:
        yield c


@pytest.fixture()
def owner(client):
    r = client.post("/auth/signup", json={"email": "ann@example.com", "password": PASSWORD, "org_name": "Org Ann"})
    assert r.status_code == 201, r.text
    return r.json()


def profiled(c: TestClient, who: dict) -> tuple[str, dict]:
    files = [("files", (f.name, f.read_bytes(), "text/csv")) for f in sorted(UPLOADS.glob("*.csv"))]
    made = c.post("/uploads", headers=bearer(who), files=files)
    assert made.status_code == 201, made.text
    upload_id = made.json()["id"]
    job = c.post(f"/uploads/{upload_id}/profile", headers=bearer(who))
    assert job.status_code == 202, job.text
    work(who["org_id"], job.json()["id"])
    got = c.get(f"/uploads/{upload_id}/proposal", headers=bearer(who))
    assert got.status_code == 200, got.text
    return upload_id, got.json()


def created(c: TestClient, who: dict, upload_id: str, proposal: dict, **silver) -> dict:
    r = c.post("/workspaces", headers=bearer(who), json={
        "from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"],
        "silver": {"declined": [], "renames": {}, **silver}})
    assert r.status_code == 201, r.text
    return r.json()


def silver_rows(who: dict, sql: str) -> list[tuple]:
    pack = wt.load_workspace(who["org_id"], SLUG)
    return Lakehouse(pack).connect().execute(sql).fetchall()


def test_messy_uploads_reach_a_grounded_answer_with_no_hand_written_sql(client, owner):
    c, a = client, owner
    upload_id, proposal = profiled(c, a)
    assert {s["name"] for s in proposal["silver"]["sources"]} == {"claims", "policies"}

    created(c, a, upload_id, proposal)
    start(c, a, "run")

    assert silver_rows(a, "select count(*) from silver_claims") == [(5,)]
    assert silver_rows(a, "select count(*) from silver_policies") == [(3,)]

    start(c, a, "ontology_propose")
    onto = c.get(f"/workspaces/{SLUG}/ontology/proposal", headers=bearer(a)).json()
    assert onto["source_layer"] == "silver"
    assert {o["name"]: o["key"] for o in onto["objects"]} == {"Claim": "claim_id", "Policy": "policy_no"}
    link = next(k for k in onto["links"] if k["via"] == "policy_ref")
    assert (link["from"], link["to"], link["orphans"]) == ("Claim", "Policy", 1)

    done = c.post(f"/workspaces/{SLUG}/ontology/accept", headers=bearer(a),
                  json={"proposal_id": onto["id"], "edits": []})
    assert done.status_code == 200, done.text
    work(a["org_id"], done.json()["job_id"])

    examples = c.get(f"/workspaces/{SLUG}/ask/examples", headers=bearer(a))
    assert examples.status_code == 200, examples.text
    assert examples.json(), "the workspace offers no question"
    answer = c.post(f"/workspaces/{SLUG}/ask", headers=bearer(a),
                    json={"question": examples.json()[0]["question"]}).json()
    assert answer["status"] == "answered", answer
    assert "[ev:" in answer["answer"]
    assert all(f"[ev:{eid}]" in answer["answer"] for eid in answer["citations"])


# ---------------------------------------------------------------- the draft through the API

def join_as(c: TestClient, owner: dict, label: str, role: str) -> dict:
    made = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                  json={"email": f"{label}@example.com", "role": role})
    assert made.status_code == 201, made.text
    got = c.post("/auth/accept-invite", json={"token": made.json()["token"], "password": PASSWORD})
    assert got.status_code in (200, 201), got.text
    return got.json()


def draft_of(proposal: dict, name: str) -> dict:
    return next(s for s in proposal["silver"]["sources"] if s["name"] == name)


def test_the_proposal_shows_decisions_with_counts_before_anything_is_created(client, owner):
    _, proposal = profiled(client, owner)
    claims = draft_of(proposal, "claims")
    assert {d["kind"] for d in claims["decisions"]} >= {"normalize_key", "cast", "drop_exact_duplicates", "collapse_on_key"}
    assert all("rows" in d and "confidence" in d and d["reason"] for d in claims["decisions"])
    assert wt.list_workspaces(owner["org_id"]) == []


def test_creating_with_the_draft_writes_the_silver_models(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal)
    got = wt.load_workspace(owner["org_id"], SLUG)
    assert [n for n, _ in got.models("silver")] == ["claims", "policies"]


def test_a_workspace_created_with_no_silver_choice_gets_no_models(client, owner):
    upload_id, proposal = profiled(client, owner)
    r = client.post("/workspaces", headers=bearer(owner),
                    json={"from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"]})
    assert r.status_code == 201, r.text
    assert wt.load_workspace(owner["org_id"], SLUG).models("silver") == []


def test_an_untick_reaches_the_lake(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal, declined=["claims.collapse_on_key"])
    start(client, owner, "run")
    assert silver_rows(owner, "select count(*) from silver_claims where claim_id = 'C-1002'") == [(2,)]


def test_a_rename_reaches_the_lake(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal, renames={"policies": {"holder": "policy_holder"}})
    start(client, owner, "run")
    assert len(silver_rows(owner, "select policy_holder from silver_policies")) == 3


@pytest.mark.parametrize("silver", [{"declined": ["claims.no_such_decision"]}, {"renames": {"claims": {"claim_id": "bad name"}}},
                                    {"renames": {"ghost": {"a": "b"}}}])
def test_a_bad_choice_is_refused_and_nothing_is_written(client, owner, silver):
    upload_id, proposal = profiled(client, owner)
    r = client.post("/workspaces", headers=bearer(owner),
                    json={"from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"], "silver": silver})
    assert r.status_code == 422, r.text
    assert wt.list_workspaces(owner["org_id"]) == []


def test_silver_sql_and_a_silver_choice_together_are_refused(client, owner):
    upload_id, proposal = profiled(client, owner)
    r = client.post("/workspaces", headers=bearer(owner), json={
        "from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"], "silver": {},
        "models": {"silver": {"01_claims.sql": "select 1"}}})
    assert r.status_code == 422, r.text


def test_a_malformed_spec_with_a_silver_choice_is_a_422_and_not_a_crash(client, owner):
    upload_id, _ = profiled(client, owner)
    r = client.post("/workspaces", headers=bearer(owner),
                    json={"from_upload": upload_id, "slug": SLUG, "spec": {"sources": "nope"}, "silver": {}})
    assert r.status_code == 422, r.text


def test_the_ontology_proposal_uses_silver_and_falls_back_to_bronze_per_source(client, owner):
    """Only policies has a model here. Claims is still looked at, from bronze."""
    upload_id, proposal = profiled(client, owner)
    made = client.post("/workspaces", headers=bearer(owner), json={
        "from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"],
        "models": {"silver": {"01_policies.sql": draft_of(proposal, "policies")["sql"]}}})
    assert made.status_code == 201, made.text
    start(client, owner, "run")
    start(client, owner, "ontology_propose")
    onto = client.get(f"/workspaces/{SLUG}/ontology/proposal", headers=bearer(owner)).json()
    assert onto["source_layer"] == "silver"
    assert {o["table"] for o in onto["objects"]} == {"silver.policies"}
    assert "bronze.claims" in {s["table"] for s in onto["skipped"]}


# ---------------------------------------------------------------- regenerate

def path(name: str = SLUG) -> str:
    return f"/workspaces/{name}/silver/draft"


def test_a_workspace_keeps_its_draft_and_the_choices_a_human_made(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal, declined=["claims.collapse_on_key"])
    got = client.get(path(), headers=bearer(owner))
    assert got.status_code == 200, got.text
    assert got.json()["declined"] == ["claims.collapse_on_key"]
    assert {s["name"] for s in got.json()["draft"]["sources"]} == {"claims", "policies"}


def test_regenerate_rewrites_the_model_and_the_next_run_builds_it(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal, declined=["claims.collapse_on_key"])
    start(client, owner, "run")
    assert silver_rows(owner, "select count(*) from silver_claims") == [(6,)]

    r = client.post(path(), headers=bearer(owner), json={"declined": [], "renames": {}})
    assert r.status_code == 200, r.text
    assert "01_claims.sql" in r.json()["models"]
    assert [n for n, _ in wt.load_workspace(owner["org_id"], SLUG).models("silver")] == ["claims", "policies"]
    start(client, owner, "run")
    assert silver_rows(owner, "select count(*) from silver_claims") == [(5,)]


def test_regenerate_replaces_a_hand_written_model_of_the_same_table_and_leaves_the_others(client, owner):
    upload_id, proposal = profiled(client, owner)
    made = client.post("/workspaces", headers=bearer(owner), json={
        "from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"], "silver": {},
        "models": {"gold": {"01_all.sql": "select count(*) n from silver_claims"}}})
    assert made.status_code == 201, made.text
    pack = wt.load_workspace(owner["org_id"], SLUG)
    (pack.dir / "models" / "silver" / "claims.sql").write_text("select 1 as x")
    (pack.dir / "models" / "silver" / "extra.sql").write_text("select 1 as x")
    r = client.post(path(), headers=bearer(owner), json={"declined": [], "renames": {}})
    assert r.status_code == 200, r.text
    folder = sorted(p.name for p in (pack.dir / "models" / "silver").iterdir())
    assert folder == ["01_claims.sql", "02_policies.sql", "extra.sql"]
    assert (pack.dir / "models" / "gold" / "01_all.sql").exists()


def test_regenerate_refuses_an_unknown_decision_and_changes_nothing(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal, declined=["claims.collapse_on_key"])
    before = client.get(path(), headers=bearer(owner)).json()
    r = client.post(path(), headers=bearer(owner), json={"declined": ["nope"], "renames": {}})
    assert r.status_code == 422, r.text
    assert client.get(path(), headers=bearer(owner)).json() == before


def test_a_workspace_with_no_draft_says_so_and_the_job_makes_one(client, owner):
    upload_id, proposal = profiled(client, owner)
    made = client.post("/workspaces", headers=bearer(owner),
                       json={"from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"]})
    assert made.status_code == 201, made.text
    got = client.get(path(), headers=bearer(owner))
    assert got.status_code == 404
    assert "silver_draft" in got.json()["detail"]
    assert client.post(path(), headers=bearer(owner), json={"declined": [], "renames": {}}).status_code == 422

    start(client, owner, "silver_draft")
    assert wt.load_workspace(owner["org_id"], SLUG).models("silver") == []  # the job alone changes no model
    assert client.post(path(), headers=bearer(owner), json={"declined": [], "renames": {}}).status_code == 200
    start(client, owner, "run")
    assert silver_rows(owner, "select count(*) from silver_claims") == [(5,)]


def test_the_job_drafts_the_latest_upload_and_keeps_what_a_human_unticked(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal, declined=["claims.collapse_on_key", "claims.cast.amount"])
    start(client, owner, "run")
    body = (UPLOADS / "claims.csv").read_bytes() + b"C-1006,P-100,10,2026-04-01,open,\nC-1006,P-100,11,2026-04-02,open,\n"
    newer = client.post("/uploads", headers=bearer(owner), files=[("files", ("claims.csv", body, "text/csv"))])
    added = client.post(f"/workspaces/{SLUG}/sources/claims/versions", headers=bearer(owner),
                        json={"upload": f"{newer.json()['id']}/claims.csv"})
    assert added.status_code == 201, added.text

    start(client, owner, "silver_draft")
    saved = client.get(path(), headers=bearer(owner)).json()
    assert saved["declined"] == ["claims.cast.amount", "claims.collapse_on_key"]
    claims = next(s for s in saved["draft"]["sources"] if s["name"] == "claims")
    assert claims["rows_in"] == 8


def test_a_member_may_read_the_draft_but_not_rewrite_the_models(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal)
    member = join_as(client, owner, "member", "member")
    assert client.get(path(), headers=bearer(member)).status_code == 200
    assert client.post(path(), headers=bearer(member), json={"declined": [], "renames": {}}).status_code == 403


def test_a_member_cannot_create_with_a_draft(client, owner):
    upload_id, proposal = profiled(client, owner)
    member = join_as(client, owner, "member", "member")
    r = client.post("/workspaces", headers=bearer(member),
                    json={"from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"], "silver": {}})
    assert r.status_code == 403


def test_another_org_reads_no_draft_and_writes_none(client, owner):
    upload_id, proposal = profiled(client, owner)
    created(client, owner, upload_id, proposal)
    other = client.post("/auth/signup", json={"email": "bo@example.com", "password": PASSWORD, "org_name": "Org Bo"}).json()
    ghost = client.get(path("no-such-workspace"), headers=bearer(other))
    theirs = client.get(path(), headers=bearer(other))
    assert (theirs.status_code, theirs.json()) == (ghost.status_code, ghost.json()) == (404, {"detail": "no such resource"})
    write = client.post(path(), headers=bearer(other), json={"declined": [], "renames": {}})
    assert write.status_code == 404
    assert "no such resource" in write.text


def test_a_demo_pack_has_no_silver_draft(client, owner):
    r = client.get(path("retail"), headers=bearer(owner))
    assert r.status_code == 404
    assert "not one of your workspaces" in r.json()["detail"]
    start_job = client.post("/jobs", headers=bearer(owner),
                            json={"kind": "silver_draft", "workspace": "retail", "params": {}})
    if start_job.status_code == 200:
        Worker(JobStore()).run_once(start_job.json()["id"])
        assert JobStore().get(owner["org_id"], start_job.json()["id"])["state"] == "failed"
    else:
        assert start_job.status_code in (403, 404)
