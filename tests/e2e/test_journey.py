"""The whole journey, in process. Each step not on the hosted stack yet is a strict xfail
that names who it waits on, so it goes red the day the piece lands and someone has to
take the mark off. Only journey.Broken counts as the expected failure. Anything else is
a real bug and stays red.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import journey
import pytest
import yaml

from ironwright import workspaces_tenant as wt
from ironwright.config import ensure_tenant
from ironwright.ingest import profiler
from ironwright.lake import lake_location

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tenant_ws"


def _waiting_on(who: str):
    return pytest.mark.xfail(strict=True, raises=journey.Broken, reason=f"waiting on {who}")


def _signed_up_with_upload(stack) -> journey.State:
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    journey.upload(stack, state)
    return state


def test_the_walk_reaches_exactly_the_first_blocked_step(stack, capsys):
    """journey.BLOCKED is what CI runs with. When the piece lands, this goes red."""
    assert journey.run(stack, blocked=journey.BLOCKED) == 0
    out = capsys.readouterr().out
    assert f"JOURNEY incomplete, stopped at {next(iter(journey.BLOCKED))}" in out
    assert "ISOLATION ok" in out


def test_a_blocked_step_that_passes_goes_red(stack, capsys):
    assert journey.run(stack, blocked={"upload": "pretend this waits on someone"}) == 1
    assert "still listed in BLOCKED" in capsys.readouterr().out


def test_blocked_names_only_real_steps():
    assert set(journey.BLOCKED) <= {name for name, _ in journey.STEPS}


def test_upload_answers_for_its_own_org(stack):
    state = _signed_up_with_upload(stack)
    got = journey.expect(stack, "GET", f"/uploads/{state.upload_id}", 200, "upload", token=state.a_token)
    assert sorted(f["name"] for f in got["files"]) == ["customers.csv", "orders.csv"]


@_waiting_on("no ticket yet: POST /jobs answers 404 for a workspace that does not exist, and a profile job runs before one can")
def test_a_profile_job_runs_through_the_api(stack):
    state = _signed_up_with_upload(stack)
    journey.profile_job(stack, state)


@_waiting_on("nobody yet: no route calls workspaces_tenant.create_workspace. W1's ticket assumes one exists")
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


def test_a_tenant_workspace_can_pick_a_warehouse_on_object_storage(stack, monkeypatch):
    """CI hit this on compose: the walk was green, and the first tenant run had no warehouse."""
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    org_id = _fixture_workspace(stack, state)
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://bucket/prefix")
    loc = lake_location(wt.load_workspace(org_id, state.workspace))
    assert f"tenants/{org_id}/workspaces/{state.workspace}/lake" in str(loc)


def _fixture_workspace(stack, state: journey.State) -> str:
    """A workspace made behind the API, to reach the steps past the missing route."""
    org_id = journey.expect(stack, "GET", "/auth/me", 200, "setup", token=state.a_token)["org_id"]
    root = ensure_tenant(org_id)
    shutil.copytree(FIXTURE / "uploads" / "fixture", root / "uploads" / "fixture")
    spec = yaml.safe_load((FIXTURE / "workspace.yaml").read_text())
    models = {layer: {f.name: f.read_text() for f in (FIXTURE / "models" / layer).glob("*.sql")}
              for layer in ("silver", "gold")}
    wt.create_workspace(org_id, state.workspace, spec, models=models,
                        ontology=yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
                        policies=yaml.safe_load((FIXTURE / "policies.yaml").read_text()))
    return org_id


def test_a_run_job_on_a_tenant_workspace_succeeds_with_a_real_worker(stack):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    _fixture_workspace(stack, state)
    journey.run_job(stack, state)


@_waiting_on("T2 or W2: GET /workspaces lists packs and scenario orgs, never a tenant workspace")
def test_a_tenant_workspace_shows_up_in_its_own_list(stack):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    _fixture_workspace(stack, state)
    journey.workspace_is_listed(stack, state)


@_waiting_on("W2: _pack() in platform_api answers 404 for anything that is not a demo pack")
def test_ask_reaches_a_tenant_workspace(stack):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    _fixture_workspace(stack, state)
    journey.run_job(stack, state)
    journey.expect(stack, "POST", f"/workspaces/{state.workspace}/ask", 200, "ask", token=state.a_token,
                   body={"question": "how many claims are there"})


@_waiting_on("W2: GET /workspaces/{ws}/ontology/proposal and POST .../accept")
def test_the_ontology_is_proposed_and_accepted(stack):
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    _fixture_workspace(stack, state)
    journey.run_job(stack, state)
    journey.ontology_accept(stack, state)


@_waiting_on("everything above: this is the acceptance test for the ticket")
def test_the_whole_journey_ends_on_a_grounded_answer(stack):
    state = journey.fresh_state()
    for _name, step in journey.STEPS:
        step(stack, state)
    assert "[ev:" in state.answer["answer"]
