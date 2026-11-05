"""scripts/seed_demo.py: Northwind Mutual holds messy exports as uploads, and Rival Co holds nothing of it."""
from __future__ import annotations

import ast
import importlib.util
import sys
import time
from pathlib import Path

import journey
import pytest

from ironwright.ingest import uploads
from ironwright.tenancy import service

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "seed_demo.py"
PASSWORD = "seed-password-for-a-test"


def _script():
    spec = importlib.util.spec_from_file_location("seed_demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SEED_PASSWORD", PASSWORD)
    monkeypatch.delenv("IRONWRIGHT_ENV", raising=False)
    return _script()


def _login(email: str) -> dict:
    return service.login(email, PASSWORD, time.time())


def test_it_makes_both_orgs_with_the_people_the_story_needs(script):
    done = script.seed()
    assert done["created"] is True
    owner, viewer, rival = _login(done["owner"]), _login(done["viewer"]), _login(done["rival"])
    assert owner["org_id"] == viewer["org_id"] != rival["org_id"]
    assert owner["org_slug"] == done["org_slug"] == "northwind-mutual"
    assert rival["org_slug"] == done["rival_slug"] == "rival-co"
    assert service.role_in(owner["user_id"], "northwind-mutual") == "owner"
    assert service.role_in(viewer["user_id"], "northwind-mutual") == "viewer"
    assert service.role_in(rival["user_id"], "rival-co") == "owner"


def test_the_exports_land_as_uploads_and_not_as_a_pack(script, tmp_path):
    done = script.seed()
    org_id = _login(done["owner"])["org_id"]
    held = uploads.list_uploads(org_id)
    assert [u["id"] for u in held] == [done["upload_id"]]
    names = sorted(f["name"] for f in held[0]["files"])
    assert names == sorted(script.FILES)
    expected = {p.name: p.read_bytes() for p in script.export_files(tmp_path)}
    folder = uploads.upload_dir(org_id, done["upload_id"])
    assert {n: (folder / n).read_bytes() for n in names} == expected


def test_the_answer_key_stays_behind(script, tmp_path):
    exported = script.export_files(tmp_path)
    assert sorted(p.name for p in exported) == sorted(script.FILES)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(script.FILES)


def test_the_files_really_are_messy(script, tmp_path):
    by_name = {p.name: p.read_text() for p in script.export_files(tmp_path)}
    assert "," not in by_name["POLADMIN.dat"].splitlines()[0][:40]  # fixed width, with no header
    assert "/" in by_name["AGENCY_BOOK.csv"].splitlines()[1]        # dates as an agency typed them
    assert '"' in by_name["AGENCY_BOOK.csv"]                        # "Vale, Jordan"


def test_a_second_run_changes_nothing(script):
    first = script.seed()
    org_id = _login(first["owner"])["org_id"]
    again = script.seed()
    assert again["created"] is False
    assert {k: v for k, v in again.items() if k != "created"} == {k: v for k, v in first.items() if k != "created"}
    assert len(uploads.list_uploads(org_id)) == 1


def test_a_half_finished_seed_is_finished_by_the_next_run(script):
    """Only the owner got made, then the run died. The next run has to carry on from there."""
    service.revoke(service.signup(script.OWNER, PASSWORD, script.ORG, time.time())["token"])
    done = script.seed()
    assert service.role_in(_login(done["viewer"])["user_id"], "northwind-mutual") == "viewer"
    assert len(uploads.list_uploads(_login(done["owner"])["org_id"])) == 1


def test_a_wrong_password_stops_with_nothing_written(script, monkeypatch):
    service.revoke(service.signup(script.OWNER, "somebody-elses-password", script.ORG, time.time())["token"])
    org_id = service.login(script.OWNER, "somebody-elses-password", time.time())["org_id"]
    with pytest.raises(SystemExit, match=script.OWNER):
        script.seed()
    assert uploads.list_uploads(org_id) == []
    with pytest.raises(service.BadCredentials):
        _login(script.VIEWER)


def test_it_refuses_prod(script, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_ENV", "prod")
    with pytest.raises(SystemExit, match="prod"):
        script.seed()


def test_the_password_never_lives_in_the_repo(script, monkeypatch):
    monkeypatch.delenv("IRONWRIGHT_SEED_PASSWORD")
    with pytest.raises(SystemExit, match="IRONWRIGHT_SEED_PASSWORD"):
        script.seed()


def test_the_owner_profiles_it_through_the_api_and_the_viewer_can_read_it(script, stack):
    done = script.seed()
    owner, viewer = _login(done["owner"])["token"], _login(done["viewer"])["token"]
    job = journey.expect(stack, "POST", f"/uploads/{done['upload_id']}/profile", 202, "seed", token=owner)
    assert job["id"]
    assert journey.expect(stack, "GET", f"/uploads/{done['upload_id']}", 200, "seed", token=viewer)["files"]


def test_rival_co_gets_a_404_on_northwinds_upload(script, stack):
    done = script.seed()
    rival = _login(done["rival"])["token"]
    for method, path in (("GET", f"/uploads/{done['upload_id']}"), ("POST", f"/uploads/{done['upload_id']}/profile")):
        got = stack.call(method, path, token=rival)
        assert (got.status, got.json()) == (404, {"detail": journey.NOT_FOUND})


def test_it_can_run_over_stdin_in_the_image():
    """The image has no scripts folder, so this file gets piped in, and only the stdlib and
    the package are there to import."""
    tree = ast.parse(SCRIPT.read_text())
    tops = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    tops |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert tops <= set(sys.stdlib_module_names) | {"ironwright"}, tops
    assert "__file__" not in SCRIPT.read_text()
