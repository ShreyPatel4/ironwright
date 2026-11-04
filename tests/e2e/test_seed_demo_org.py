"""scripts/seed_demo_org.py: one synthetic pack as a real workspace in a real org."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import journey
import pytest

from ironwright.config import data_root
from ironwright.tenancy import service

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "seed-password-for-a-test"


def _script():
    spec = importlib.util.spec_from_file_location("seed_demo_org", ROOT / "scripts" / "seed_demo_org.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def seed(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SEED_PASSWORD", PASSWORD)
    return _script().seed


def test_it_makes_an_org_and_a_workspace_whose_run_passes(seed):
    done = seed()
    assert done["created"] is True
    assert done["evals_passed"] is True
    assert done["workspace"] == "demo"


def test_the_owner_can_log_in_and_read_it_through_the_api(seed, stack):
    done = seed()
    token = service.login(done["email"], PASSWORD, time.time())["token"]
    got = journey.expect(stack, "GET", f"/uploads/{done['upload_id']}", 200, "seed", token=token)
    assert got["files"]
    got = journey.expect(stack, "POST", "/jobs", 200, "seed", token=token,
                         body={"kind": "run", "workspace": done["workspace"], "params": {}})
    assert got["id"]


def test_a_second_org_cannot_see_it(seed, stack):
    done = seed()
    state = journey.fresh_state()
    journey.signup_a(stack, state)
    for path in (f"/uploads/{done['upload_id']}", f"/workspaces/{done['workspace']}/lake/tables"):
        got = stack.call("GET", path, token=state.a_token)
        assert (got.status, got.json()) == (404, {"detail": journey.NOT_FOUND})


def test_a_second_run_changes_nothing(seed):
    first = seed()
    again = seed()
    assert again["created"] is False
    assert (again["org_id"], again["workspace"], again["upload_id"]) == (first["org_id"], first["workspace"], first["upload_id"])


def test_a_wrong_password_on_a_second_run_stops_it(seed, monkeypatch):
    seed()
    before = sorted(p.name for p in (data_root() / "tenants").iterdir())
    monkeypatch.setenv("IRONWRIGHT_SEED_PASSWORD", "some-other-password")
    with pytest.raises(SystemExit, match="password"):
        seed()
    # Signup answers a taken email with a decoy org. Nothing may land under that id.
    assert sorted(p.name for p in (data_root() / "tenants").iterdir()) == before


def test_it_leaves_the_shared_demo_folder_alone(seed):
    """Generate wipes a pack's folder, and demo/ is shared with every tenant."""
    seed()
    assert not (data_root() / "demo").exists()


def test_it_writes_no_ground_truth_into_the_tenant_tree(seed):
    """The generator's answer key is not a tenant file."""
    done = seed()
    tree = data_root() / "tenants" / done["org_id"]
    assert not list(tree.rglob("_ground_truth*"))


def test_it_refuses_prod(seed, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_ENV", "prod")
    with pytest.raises(SystemExit, match="prod"):
        seed()


def test_it_needs_a_password_from_the_environment(seed, monkeypatch):
    monkeypatch.delenv("IRONWRIGHT_SEED_PASSWORD")
    with pytest.raises(SystemExit, match="IRONWRIGHT_SEED_PASSWORD"):
        seed()


def test_the_script_runs_from_stdin_since_the_image_has_no_scripts_folder():
    """`docker compose exec -T api python - < scripts/seed_demo_org.py`: nothing in it can
    lean on __file__ or on an import from the repo's scripts folder."""
    text = (ROOT / "scripts" / "seed_demo_org.py").read_text()
    assert "__file__" not in text


# The routes are here. scripts/seed_demo_org.py stops before an ontology, so it has nothing to ask.
@pytest.mark.xfail(strict=True, raises=journey.Broken, reason="W4: the seed has to propose and accept an ontology")
def test_the_seeded_org_can_ask_a_grounded_question(seed, stack):
    done = seed()
    token = service.login(done["email"], PASSWORD, time.time())["token"]
    state = journey.fresh_state()
    state.a_token, state.workspace = token, done["workspace"]
    journey.ask(stack, state)
