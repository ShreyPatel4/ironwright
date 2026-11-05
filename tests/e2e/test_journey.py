"""The whole journey, in process. Only journey.Broken counts as a broken step."""
from __future__ import annotations

from pathlib import Path

import journey

from ironwright import workspaces_tenant as wt
from ironwright.ingest import profiler
from ironwright.lake import lake_location

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tenant_ws"


def _signed_up_with_upload(stack) -> journey.State:
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    journey.upload(stack, state)
    return state


def test_nothing_is_blocked_any_more():
    assert journey.BLOCKED == {}


def test_the_walk_completes_and_isolation_holds(stack, capsys):
    assert journey.run(stack, blocked=journey.BLOCKED) == 0
    out = capsys.readouterr().out
    assert "JOURNEY complete" in out
    assert "ISOLATION ok" in out


def test_a_broken_step_on_the_blocked_list_stops_the_walk_without_failing_it(stack, capsys, monkeypatch):
    def broken(_stack, _state):
        raise journey.Broken("upload", "pretend")

    monkeypatch.setattr(journey, "STEPS", [("signup_a", journey.signup_a), ("upload", broken)])
    assert journey.run(stack, blocked={"upload": "pretend this waits on someone"}) == 0
    assert "JOURNEY incomplete, stopped at upload" in capsys.readouterr().out


def test_a_blocked_step_that_passes_goes_red(stack, capsys):
    assert journey.run(stack, blocked={"upload": "pretend this waits on someone"}) == 1
    assert "still listed in BLOCKED" in capsys.readouterr().out


def test_blocked_names_only_real_steps():
    assert set(journey.BLOCKED) <= {name for name, _ in journey.STEPS}


def test_upload_answers_for_its_own_org(stack):
    state = _signed_up_with_upload(stack)
    got = journey.expect(stack, "GET", f"/uploads/{state.upload_id}", 200, "upload", token=state.a_token)
    assert sorted(f["name"] for f in got["files"]) == ["customers.csv", "orders.csv"]


def test_a_profile_job_runs_through_the_api(stack):
    state = _signed_up_with_upload(stack)
    journey.profile_job(stack, state)


def test_accepting_the_proposal_makes_a_workspace(stack):
    state = _signed_up_with_upload(stack)
    org_id = journey.expect(stack, "GET", "/auth/me", 200, "accept_proposal", token=state.a_token)["org_id"]
    profiler.profile(org_id, state.upload_id, lambda *_: None)
    state.proposal = journey.expect(stack, "GET", f"/uploads/{state.upload_id}/proposal", 200,
                                    "accept_proposal", token=state.a_token)["workspace"]
    journey.accept_proposal(stack, state)


def test_a_proposal_with_a_fixed_width_file_can_become_a_workspace(stack):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    raw = (FIXTURE.parent / "ingest" / "mainframe" / "ITEMMAST.DAT").read_bytes()
    made = journey.expect(stack, "POST", "/uploads", 201, "upload", token=state.a_token, files=[("ITEMMAST.DAT", raw)])
    org_id = journey.expect(stack, "GET", "/auth/me", 200, "setup", token=state.a_token)["org_id"]
    profiler.profile(org_id, made["id"], lambda *_: None)
    proposal = journey.expect(stack, "GET", f"/uploads/{made['id']}/proposal", 200, "setup", token=state.a_token)
    wt.create_workspace(org_id, state.workspace, proposal["workspace"])


def test_a_tenant_workspace_can_pick_a_warehouse_on_object_storage(stack, tenant_workspace, monkeypatch):
    """CI hit this on compose: the walk was green, and the first tenant run had no warehouse."""
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    org_id = tenant_workspace(state)
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://bucket/prefix")
    loc = lake_location(wt.load_workspace(org_id, state.workspace))
    assert f"tenants/{org_id}/workspaces/{state.workspace}/lake" in str(loc)


def test_a_run_job_on_a_tenant_workspace_succeeds_with_a_real_worker(stack, tenant_workspace):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    tenant_workspace(state)
    journey.run_job(stack, state)


def test_a_tenant_workspace_shows_up_in_its_own_list(stack, tenant_workspace):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    tenant_workspace(state)
    journey.workspace_is_listed(stack, state)


def test_ask_reaches_a_tenant_workspace(stack, tenant_workspace):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    tenant_workspace(state)
    journey.run_job(stack, state)
    journey.expect(stack, "POST", f"/workspaces/{state.workspace}/ask", 200, "ask", token=state.a_token,
                   body={"question": "how many claims are there"})


def test_the_ontology_is_proposed_and_accepted(stack, tenant_workspace):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    tenant_workspace(state)
    journey.run_job(stack, state)
    journey.ontology_accept(stack, state)


def test_the_whole_journey_ends_on_a_grounded_answer(stack):
    state = journey.fresh_state()
    for _name, step in journey.STEPS:
        step(stack, state)
    assert "[ev:" in state.answer["answer"]
