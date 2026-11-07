"""Every read a console view makes, on a workspace an org made through the API.

The state R1 found (F-03): the selector names the workspace, and the view beside it says
no workspace named it. These tests use the wizard's own routes and a real worker. A name
alone never picks anything here, so two orgs that both use one slug are in every proof.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import journey
import pytest

from ironwright import discovery as discovery_mod
from ironwright.config import data_root

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
JOIN_FILES = sorted((FIXTURES / "ingest" / "join").glob("*.csv"))
SLUG = "acme-sales"
READS = ["sources", "loads", "lake/tables", "ontology", "discovery", "boards", "bi", "audit", "audit/verify",
         "governance", "principals", "approvals", "escalations", "agents", "traces", "reports/migrate"]


def _as_read(stack, state: journey.State, path: str, status: int = 200):
    return journey.expect(stack, "GET", f"/workspaces/{state.workspace}/{path}", status, path, token=state.a_token)


def _query(stack, state: journey.State, sql: str) -> journey.Answer:
    return stack.call("POST", f"/workspaces/{state.workspace}/lake/query", body={"sql": sql}, token=state.a_token)


def _state(slug: str = SLUG) -> journey.State:
    state = journey.fresh_state()
    state.workspace = slug
    return state


@pytest.fixture()
def built(stack) -> journey.State:
    """Sign up to an accepted ontology, with the run that accept queues finished."""
    state = _state()
    for step in (journey.signup_a, journey.upload, journey.profile_job, journey.accept_proposal,
                 journey.run_job, journey.ontology_accept):
        step(stack, state)
    return state


@pytest.fixture()
def unrun(stack) -> journey.State:
    """The wizard stopped at Map: the workspace exists, and nothing has run."""
    state = _state()
    for step in (journey.signup_a, journey.upload, journey.profile_job, journey.accept_proposal):
        step(stack, state)
    return state


def _second_org(stack, first: journey.State) -> journey.State:
    """A different org, the same slug, and files with other names, so its sources differ."""
    state = _state(first.workspace)
    journey.signup_a(stack, state)
    files = [(f"other_{p.name}", p.read_bytes()) for p in JOIN_FILES]
    state.upload_id = journey.expect(stack, "POST", "/uploads", 201, "upload", token=state.a_token, files=files)["id"]
    job_id = journey.expect(stack, "POST", f"/uploads/{state.upload_id}/profile", 202, "profile",
                            token=state.a_token)["id"]
    journey._wait_job(stack, state, job_id, "profile")
    state.proposal = journey.expect(stack, "GET", f"/uploads/{state.upload_id}/proposal", 200, "profile",
                                    token=state.a_token)["workspace"]
    journey.accept_proposal(stack, state)
    return state


def test_every_view_read_answers_on_a_tenant_workspace(stack, built):
    for path in READS:
        _as_read(stack, built, path)


def test_every_view_read_answers_before_the_first_run(stack, unrun):
    """Red text beside a workspace that is merely new would be F-03 again."""
    reads = [p for p in READS if p != "reports/migrate"]
    for path in reads:
        _as_read(stack, unrun, path)
    assert _as_read(stack, unrun, "lake/tables") == []
    loads = _as_read(stack, unrun, "loads")
    assert loads["writes"] == [] and loads["run_id"] is None and loads["quarantine_tables"] == []


def test_sources_are_the_ones_this_workspace_declares(stack, built):
    got = _as_read(stack, built, "sources")
    assert {s["name"] for s in got} == {s["name"] for s in built.proposal["sources"]}
    assert all(s["kind"] == "upload" and s["format"] == "csv" and s["file"].endswith(".csv") for s in got)


def test_sources_never_name_a_server_path(stack, built):
    raw = stack.call("GET", f"/workspaces/{built.workspace}/sources", token=built.a_token).raw.decode()
    assert "upload_path" not in raw
    assert str(data_root()) not in raw


def test_a_fixed_width_source_answers_the_layout_the_view_draws(stack):
    state = _state()
    journey.signup_a(stack, state)
    raw = (FIXTURES / "ingest" / "mainframe" / "ITEMMAST.DAT").read_bytes()
    state.upload_id = journey.expect(stack, "POST", "/uploads", 201, "upload", token=state.a_token,
                                     files=[("ITEMMAST.DAT", raw)])["id"]
    job_id = journey.expect(stack, "POST", f"/uploads/{state.upload_id}/profile", 202, "profile",
                            token=state.a_token)["id"]
    journey._wait_job(stack, state, job_id, "profile")
    proposal = journey.expect(stack, "GET", f"/uploads/{state.upload_id}/proposal", 200, "profile",
                              token=state.a_token)["workspace"]
    state.proposal = proposal
    journey.accept_proposal(stack, state)
    source = _as_read(stack, state, "sources")[0]
    assert source["format"] == "fixed_width"
    declared = proposal["sources"][0]["layout"]["fields"]
    assert source["layout"] == [[f["name"], f["width"]] for f in declared]


def test_the_lake_tables_and_a_query_read_this_workspace(stack, built):
    tables = _as_read(stack, built, "lake/tables")
    assert {t["ident"] for t in tables} >= {f"bronze.{s['name']}" for s in built.proposal["sources"]}
    first = built.proposal["sources"][0]["name"]
    got = _query(stack, built, f"select count(*) as n from bronze_{first}")
    assert got.status == 200 and int(got.json()["rows"][0][0]) > 0


def test_a_query_cannot_read_a_file_off_the_server(stack, built):
    for sql in ("select * from read_csv('/etc/passwd')", "select * from read_text('/etc/passwd')"):
        got = _query(stack, built, sql)
        assert got.status in (400, 422), got.raw
        assert b"root:" not in got.raw


def test_loads_show_what_the_run_wrote(stack, built):
    loads = _as_read(stack, built, "loads")
    assert loads["run_id"]
    assert all(w["published"] for w in loads["writes"]) and loads["writes"]


def test_the_audit_chain_is_this_workspace_s_own(stack, built):
    assert _as_read(stack, built, "audit/verify")["intact"] is True


def test_traces_are_the_pipeline_s_own(stack, built):
    traces = _as_read(stack, built, "traces")
    assert traces
    spans = _as_read(stack, built, f"traces/{traces[0]['trace_id']}")
    assert spans


def test_governance_shows_the_rules_that_decide(stack, built):
    gov = _as_read(stack, built, "governance")
    assert "org-default-members-read" in {r["id"] for r in gov["rules"]}
    assert gov["generated_rules"] == []
    assert "local build" not in gov["note"]
    got = journey.expect(stack, "POST", f"/workspaces/{built.workspace}/governance/check", 200, "check",
                         token=built.a_token, body={"principal": "user:someone", "action": "function:anything"})
    assert got["decision"]["allowed"] is True
    got = journey.expect(stack, "POST", f"/workspaces/{built.workspace}/governance/check", 200, "check",
                         token=built.a_token, body={"principal": "agent:someone", "action": "action:anything"})
    assert got["decision"]["allowed"] is False


def test_boards_save_and_run_over_this_workspace(stack, built):
    first = built.proposal["sources"][0]["name"]
    tiles = [{"title": "n", "sql": f"select count(*) as n from bronze_{first}", "viz": "number"},
             {"title": "escape", "sql": "select * from read_text('/etc/passwd')", "viz": "table"}]
    path = f"/workspaces/{built.workspace}/boards/weekly"
    journey.expect(stack, "PUT", path, 200, "board", token=built.a_token, body={"title": "w", "tiles": tiles})
    assert [b["slug"] for b in _as_read(stack, built, "boards")] == ["weekly"]
    ran = journey.expect(stack, "POST", f"{path}/run", 200, "board", token=built.a_token, body={})
    assert ran["tiles"][0]["ok"] and int(ran["tiles"][0]["rows"][0][0]) > 0
    # The tile saves, because it is one SELECT. The locked connection is what stops it.
    assert not ran["tiles"][1]["ok"] and "root:" not in json.dumps(ran["tiles"][1])


def test_the_bi_embed_is_kept_per_workspace(stack, built):
    assert _as_read(stack, built, "bi")["url"] is None
    journey.expect(stack, "PUT", f"/workspaces/{built.workspace}/bi", 200, "bi", token=built.a_token,
                   body={"url": "https://bi.acme.example/d/1"})
    assert _as_read(stack, built, "bi")["url"] == "https://bi.acme.example/d/1"


def test_agents_and_approvals_answer_empty_instead_of_refusing(stack, built):
    assert _as_read(stack, built, "agents") == {}
    assert _as_read(stack, built, "approvals") == []
    assert _as_read(stack, built, "escalations") == []


def test_an_escalation_says_why_it_does_not_apply(stack, built):
    got = stack.call("POST", f"/workspaces/{built.workspace}/escalations", token=built.a_token,
                     body={"action": "x", "object_id": "y", "denying_rule": "z"})
    assert got.status == 404
    assert "no workspace named" not in got.json()["detail"]
    got = stack.call("POST", f"/workspaces/{built.workspace}/escalations/esc-0001/decide", token=built.a_token,
                     body={"decision": "deny"})
    assert got.status == 404
    assert "no workspace named" not in got.json()["detail"]


def _an_object(stack, state: journey.State) -> tuple[str, str]:
    types = _as_read(stack, state, "ontology")["describe"]["object_types"]
    type_ = min(types)
    return type_, _as_read(stack, state, f"objects/{type_}")[0]["_id"]


def test_an_object_opens_with_its_identity_and_history(stack, built):
    type_, id_ = _an_object(stack, built)
    assert _as_read(stack, built, f"objects/{type_}/{id_}")["_id"] == id_
    page = _as_read(stack, built, f"objects/{type_}/{id_}/dashboard")
    assert page["_id"] == id_ and page["_crosswalk"] and page["_history"]
    assert page["_allowed_actions"] == []


def test_an_object_reads_as_of_a_time(stack, built):
    type_, id_ = _an_object(stack, built)
    got = _as_read(stack, built, f"objects/{type_}/{id_}/asof?ts=2100-01-01T00:00:00Z")
    assert got["object"]["_id"] == id_


def test_an_object_is_this_org_s_own_when_the_slug_is_shared(stack, built):
    other = _second_org(stack, built)
    journey.run_job(stack, other)
    type_, id_ = _an_object(stack, built)
    assert stack.call("GET", f"/workspaces/{other.workspace}/objects/{type_}/{id_}", token=other.a_token).status == 404


def test_an_action_says_why_it_does_not_apply(stack, built):
    got = stack.call("POST", f"/workspaces/{built.workspace}/actions/anything", body={"params": {}}, token=built.a_token)
    assert got.status == 404, got.raw
    assert "no workspace named" not in got.json()["detail"]


def test_a_decision_on_nothing_pending_is_a_plain_404(stack, built):
    got = stack.call("POST", f"/workspaces/{built.workspace}/approvals/1/decide", token=built.a_token,
                     body={"approved_by": "role:x", "decision": "deny"})
    assert got.status == 404, got.raw
    assert "no workspace named" not in got.json()["detail"]


def test_training_says_why_it_does_not_apply(stack, built):
    got = stack.call("GET", f"/workspaces/{built.workspace}/training/labels", token=built.a_token)
    assert got.status == 404
    assert "no workspace named" not in got.json()["detail"]
    assert "agents" in got.json()["detail"]


def test_a_read_during_a_run_says_so_instead_of_a_500(stack, built, monkeypatch):
    def locked(*_a, **_k):
        raise duckdb.IOException("Conflicting lock is held")

    monkeypatch.setattr(discovery_mod, "OntologyStore", locked)
    got = stack.call("GET", f"/workspaces/{built.workspace}/discovery", token=built.a_token)
    assert got.status == 409
    assert "run" in got.json()["detail"]


@pytest.mark.parametrize(("method", "path", "body"), [
    ("POST", "bi/mirror", {}),
    ("POST", "engines/check", {}),
    ("POST", "otel/export", {"endpoint": "http://collector.example/v1/traces"}),
])
def test_tenant_data_is_not_copied_off_its_prefix(stack, built, method, path, body):
    """A shared Postgres and a URL the caller names are both outside the org's storage."""
    got = stack.call(method, f"/workspaces/{built.workspace}/{path}", body=body, token=built.a_token)
    assert got.status == 403, got.raw
    detail = got.json()["detail"]
    assert "no workspace named" not in detail


def test_two_orgs_with_one_slug_each_read_only_their_own(stack, built):
    other = _second_org(stack, built)
    journey.run_job(stack, other)
    assert other.workspace == built.workspace

    mine = {s["name"] for s in _as_read(stack, built, "sources")}
    theirs = {s["name"] for s in _as_read(stack, other, "sources")}
    assert mine and theirs and mine.isdisjoint(theirs)

    my_tables = {t["ident"] for t in _as_read(stack, built, "lake/tables")}
    their_tables = {t["ident"] for t in _as_read(stack, other, "lake/tables")}
    assert my_tables.isdisjoint(their_tables)
    assert _as_read(stack, built, "loads")["run_id"]
    assert {w["table"] for w in _as_read(stack, built, "loads")["writes"]}.isdisjoint(
        {w["table"] for w in _as_read(stack, other, "loads")["writes"]})

    table = min(t for t in their_tables if t.startswith("bronze.")).replace(".", "_")
    assert _query(stack, other, f"select 1 from {table}").status == 200
    assert _query(stack, built, f"select 1 from {table}").status == 400

    journey.expect(stack, "PUT", f"/workspaces/{built.workspace}/bi", 200, "bi", token=built.a_token,
                   body={"url": "https://bi.a.example/1"})
    assert _as_read(stack, other, "bi")["url"] is None
    journey.expect(stack, "PUT", f"/workspaces/{built.workspace}/boards/mine", 200, "board", token=built.a_token,
                   body={"title": "mine", "tiles": [{"title": "n", "sql": "select 1 as n", "viz": "number"}]})
    assert _as_read(stack, other, "boards") == []
    assert stack.call("GET", f"/workspaces/{built.workspace}/boards/mine", token=other.a_token).status == 404


def test_a_folder_with_the_same_bare_name_is_never_read(stack, built):
    """Generated policy and escalations file by bare name under the data root. A folder
    there that shares this slug belongs to somebody else, and it must decide nothing."""
    foreign = data_root() / built.workspace
    foreign.mkdir()
    (foreign / "org.json").write_text(json.dumps({"company": "SOMEONE ELSE"}))
    (foreign / "policies.generated.yaml").write_text(
        "default: deny\nrules:\n- {id: foreign-rule, effect: permit, principal: '*', action: '*'}\n")
    (foreign / "escalations.jsonl").write_text(json.dumps(
        {"moment": "escalated", "id": "esc-0001", "principal": "user:foreign", "action": "x"}) + "\n")
    # The marker a created organization carries, so a delete by bare name would act on it.
    (foreign / ".ironwright-workspace").write_text(json.dumps({"created_by": "ironwright", "name": built.workspace}))
    policy = (foreign / "policies.generated.yaml").read_text()
    try:
        gov = _as_read(stack, built, "governance")
        assert "foreign-rule" not in json.dumps(gov)
        assert _as_read(stack, built, "escalations") == []
        got = journey.expect(stack, "POST", f"/workspaces/{built.workspace}/governance/check", 200, "check",
                             token=built.a_token, body={"principal": "agent:x", "action": "action:y"})
        assert got["decision"]["allowed"] is False
        for method, path, body in [("GET", "organization", None), ("GET", "orgpolicy/preview", None),
                                   ("POST", "orgpolicy/apply", {}), ("DELETE", "", None)]:
            got = stack.call(method, f"/workspaces/{built.workspace}/{path}".rstrip("/"), body=body,
                             token=built.a_token)
            assert got.status == 404, (method, path, got.raw)
            assert b"SOMEONE ELSE" not in got.raw and "your own workspace" in got.json()["detail"], got.raw
        assert (foreign / "policies.generated.yaml").read_text() == policy
        assert (foreign / "org.json").exists()
    finally:
        shutil.rmtree(foreign)
