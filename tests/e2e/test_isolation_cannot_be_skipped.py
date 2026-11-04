"""Org B gets a 404 on everything of org A's, and nothing can turn that check off.

No test in this file may carry a skip or an xfail. A test below reads this file and
holds it to that.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import journey
import pytest
import yaml
from test_isolation_routes import _routes, _scope_param

from ironwright.platform_api import NOT_FOUND, create_platform_app

ROOT = Path(__file__).resolve().parents[2]


class Leaky:
    """A real stack with one lie in it, as `lie(method, path, token, real_answer)`."""

    def __init__(self, real, lie) -> None:
        self.real, self.lie = real, lie

    def call(self, method, path, **kw):
        answer = self.real.call(method, path, **kw)
        return self.lie(method, path, kw.get("token"), answer) or answer

    def pump(self, job_id):
        self.real.pump(job_id)


def _scoped_routes() -> set[tuple[str, str]]:
    app = create_platform_app()
    return {(m, t) for t, methods in _routes(app.routes) for m in methods if _scope_param(t)}


def test_the_table_is_exactly_the_scoped_routes_of_the_app():
    """A new route goes red here until the live sweep covers it. T1's classifier
    decides what is scoped, so there is one classification and not two."""
    assert set(journey.ISOLATION_ROUTES) == _scoped_routes()


def test_the_driver_answers_with_the_apps_not_found():
    assert journey.NOT_FOUND == NOT_FOUND


def test_org_b_gets_a_404_on_every_route_of_org_a(stack):
    a = journey.fresh_state()
    checked = journey.isolation(stack, a)
    assert checked >= journey.MIN_ISOLATION_ROUTES


@pytest.mark.parametrize("marker", ["/uploads/", "/jobs/", "/organization", "/lake/tables", "/orgs/"])
def test_one_leaky_route_goes_red(stack, marker):
    seen = {}

    def lie(method, path, token, answer):
        # The sweep is the only caller using B's token on A's paths with a 404 answer.
        if token and marker in path and answer.status == 404:
            seen["path"] = path
            return journey.Answer(200, b'{"rows": []}')
        return None

    with pytest.raises(journey.Broken, match="isolation"):
        journey.isolation(Leaky(stack, lie), journey.fresh_state())
    assert marker in seen["path"]


def test_a_404_with_a_different_body_goes_red(stack):
    """A different body tells B the thing exists."""
    def lie(method, path, token, answer):
        if "/uploads/" in path and answer.status == 404:
            return journey.Answer(404, b'{"detail": "that upload belongs to someone else"}')
        return None

    with pytest.raises(journey.Broken, match="isolation"):
        journey.isolation(Leaky(stack, lie), journey.fresh_state())


@pytest.mark.parametrize("listing", ["/jobs", "/workspaces"])
def test_a_listing_that_names_a_resource_of_org_a_goes_red(stack, listing):
    state = journey.fresh_state()

    def lie(method, path, token, answer):
        if method == "GET" and path == listing and token == state.b_token:
            return journey.Answer(200, f'["{state.isolation_workspace}", "{state.isolation_job}"]'.encode())
        return None

    with pytest.raises(journey.Broken, match="isolation"):
        journey.isolation(Leaky(stack, lie), state)


def test_a_job_body_naming_a_workspace_of_org_a_goes_red(stack):
    """The gate cannot see a body, so POST /jobs is its own check."""
    def lie(method, path, token, answer):
        return journey.Answer(200, b'{"id": "x"}') if method == "POST" and path == "/jobs" and answer.status == 404 else None

    with pytest.raises(journey.Broken, match="isolation"):
        journey.isolation(Leaky(stack, lie), journey.fresh_state())


def test_a_stack_that_answers_404_to_everyone_proves_nothing(stack):
    """If org A cannot read its own things, B's 404s mean nothing."""
    state = journey.fresh_state()

    def lie(method, path, token, answer):
        if method == "GET" and token == state.a_token and ("/uploads/" in path or "/jobs/" in path):
            return journey.Answer(404, b'{"detail": "no such resource"}')
        return None

    with pytest.raises(journey.Broken, match="isolation"):
        journey.isolation(Leaky(stack, lie), state)


def test_a_write_from_org_b_that_lands_goes_red(stack):
    """The answer can be a clean 404 while the delete happened. Org A reads again after."""
    state = journey.fresh_state()
    swept = {"done": False}

    def lie(method, path, token, answer):
        if token == state.b_token and method == "DELETE":
            swept["done"] = True
        if swept["done"] and token == state.a_token and method == "GET" and "/organization" in path:
            return journey.Answer(404, b'{"detail": "no such resource"}')
        return None

    with pytest.raises(journey.Broken, match="isolation"):
        journey.isolation(Leaky(stack, lie), state)


def test_a_short_table_goes_red(stack, monkeypatch):
    monkeypatch.setattr(journey, "ISOLATION_ROUTES", journey.ISOLATION_ROUTES[:3])
    with pytest.raises(journey.Broken, match="isolation"):
        journey.isolation(stack, journey.fresh_state())


def test_isolation_still_runs_when_the_walk_breaks_first(stack, capsys, monkeypatch):
    def boom(_stack, _state):
        raise journey.Broken("upload", "the upload route fell over")

    monkeypatch.setattr(journey, "STEPS", [journey.STEPS[0], ("upload", boom), *journey.STEPS[2:]])
    assert journey.run(stack, blocked={}) == 1
    out = capsys.readouterr().out
    assert "ISOLATION ok" in out
    assert "JOURNEY broken at upload" in out


def test_a_clean_walk_with_a_leak_is_still_a_failure(stack, capsys, monkeypatch):
    monkeypatch.setattr(journey, "STEPS", [journey.STEPS[0]])

    def lie(method, path, token, answer):
        return journey.Answer(200, b"{}") if "/uploads/" in path and answer.status == 404 else None

    assert journey.run(Leaky(stack, lie), blocked={}) == 1
    out = capsys.readouterr().out
    assert "ISOLATION broken" in out
    assert "ISOLATION ok" not in out


def test_isolation_cannot_be_listed_as_blocked(stack):
    with pytest.raises(ValueError, match="isolation"):
        journey.run(stack, blocked={"isolation": "not today"})


def test_the_driver_has_no_switch_for_it():
    flags = {a.dest for a in journey.parser()._actions} - {"help"}
    assert flags == {"base", "wait", "budget"}
    assert not re.search(r"environ|getenv", (ROOT / "tests/e2e/journey.py").read_text())


def test_no_test_here_can_be_skipped():
    tree = ast.parse(Path(__file__).read_text())
    text = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} & {"skip", "skipif", "xfail", "importorskip"}
    assert not text


def _e2e_job() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["e2e"]


def test_ci_cannot_skip_the_e2e_job():
    """`needs` matters: a job skipped because a job before it failed never reports red."""
    job = _e2e_job()
    assert not {"if", "needs", "continue-on-error"} & set(job)
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = workflow[True]  # YAML reads the bare key `on` as true
    assert "pull_request" in triggers and not (triggers["pull_request"] or {})


def test_ci_fails_when_the_walk_prints_no_isolation_line():
    steps = _e2e_job()["steps"]
    walk = [s for s in steps if "tests/e2e/journey.py" in s.get("run", "")]
    assert len(walk) == 1
    assert "if" not in walk[0] and "continue-on-error" not in walk[0]
    assert "grep -q '^ISOLATION ok'" in walk[0]["run"]
    assert not [s for s in steps if s.get("continue-on-error")]


def test_ci_keeps_artifacts_on_failure():
    job = _e2e_job()
    assert job["timeout-minutes"] <= 25
    upload = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert len(upload) == 1 and upload[0]["if"] == "failure()"
