"""Who may make an account, and how a visitor tries the product without one.

IRONWRIGHT_SIGNUP is open, invite or closed. Prod defaults to invite, so the day we launch
nobody can mint orgs on our bill. A visitor still gets in through the demo door: a viewer
seat in one named demo org, which reads the demo packs and writes nothing.
"""
from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ironwright import auth, hosted
from ironwright.api import DOORS
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import service

PASSWORD = "correct horse battery"
SIGNUP = {"email": "a@example.com", "password": PASSWORD, "org_name": "Acme"}
PROD = {"IRONWRIGHT_SECRET_KEY": Fernet.generate_key().decode(),
        "IRONWRIGHT_DB_URL": "postgresql://u:p@db.example:5432/iw",
        "IRONWRIGHT_STATE_URI": "s3://ironwright-state/prod",
        "IRONWRIGHT_LAKE_URI": "s3://ironwright-state/lake"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    for name in ("IRONWRIGHT_DEV_OPEN", "IRONWRIGHT_ENV", "IRONWRIGHT_SIGNUP", "IRONWRIGHT_DEMO_ORG",
                 *PROD):
        monkeypatch.delenv(name, raising=False)


def _client() -> TestClient:
    return TestClient(create_platform_app())


@pytest.mark.parametrize("env_name, want", [("local", "open"), ("staging", "open"), ("prod", "invite")])
def test_the_default_is_open_except_in_prod(monkeypatch, env_name, want):
    monkeypatch.setenv("IRONWRIGHT_ENV", env_name)
    assert auth.signup_mode() == want


def test_the_env_picks_the_mode(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_ENV", "prod")
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "open")
    assert auth.signup_mode() == "open"


def test_a_mode_we_do_not_know_is_refused(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "maybe")
    with pytest.raises(ValueError, match="IRONWRIGHT_SIGNUP"):
        auth.signup_mode()


def test_status_says_the_mode_so_the_console_can_hide_the_button(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "invite")
    got = _client().get("/auth/status").json()
    assert got["signup"] == "invite" and got["demo"] is False


def test_open_signs_up(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "open")
    assert _client().post("/auth/signup", json=SIGNUP).status_code == 201


def _owner_invites(email: str) -> str:
    owner = service.signup("owner@example.com", PASSWORD, "Owners", time.time())
    return service.invite(owner["org_slug"], email, "member", time.time())


@pytest.mark.parametrize("mode", ["invite", "closed"])
def test_invite_and_closed_refuse_a_new_org(monkeypatch, mode):
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", mode)
    r = _client().post("/auth/signup", json=SIGNUP)
    assert r.status_code == 403
    assert "invite" in r.json()["detail"] if mode == "invite" else "closed" in r.json()["detail"]
    assert not service.any_users()


def test_invite_still_lets_an_invited_person_in(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "invite")
    token = _owner_invites("b@example.com")
    r = _client().post("/auth/accept-invite", json={"token": token, "password": PASSWORD})
    assert r.status_code == 200, r.text


def test_closed_refuses_an_invite_too(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "closed")
    token = _owner_invites("b@example.com")
    assert _client().post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).status_code == 403


def test_closed_still_lets_people_sign_in(monkeypatch):
    service.signup("a@example.com", PASSWORD, "Acme", time.time())
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "closed")
    assert _client().post("/auth/login", json={"email": "a@example.com", "password": PASSWORD}).status_code == 200


# ---------------------------------------------------------------- the demo door
def test_no_demo_org_means_no_demo_door():
    assert _client().post("/auth/demo").status_code == 404


def test_a_visitor_gets_a_viewer_seat_in_the_demo_org(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "invite")
    monkeypatch.setenv("IRONWRIGHT_DEMO_ORG", "ironwright-demo")
    c = _client()
    assert c.get("/auth/status").json()["demo"] is True
    r = c.post("/auth/demo")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["role"] == "viewer" and got["org_slug"] == "ironwright-demo" and got["demo"] is True
    me = c.get("/auth/me", headers={"authorization": f"Bearer {got['token']}"})
    assert me.status_code == 200 and me.json()["role"] == "viewer"


def test_two_visitors_do_not_sign_each_other_out(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEMO_ORG", "ironwright-demo")
    c = _client()
    first, second = c.post("/auth/demo").json(), c.post("/auth/demo").json()
    assert first["user_id"] != second["user_id"]
    for seat in (first, second):
        assert c.get("/auth/me", headers={"authorization": f"Bearer {seat['token']}"}).status_code == 200


def test_a_visitor_reads_the_demo_packs_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEMO_ORG", "ironwright-demo")
    c = _client()
    h = {"authorization": f"Bearer {c.post('/auth/demo').json()['token']}"}
    names = {w["name"] for w in c.get("/workspaces", headers=h).json()}
    assert "retail" in names
    assert c.post("/workspaces", json={"name": "mine", "size": 4, "seed": 1}, headers=h).status_code == 403
    assert c.post("/orgs/ironwright-demo/invites", json={"email": "x@example.com", "role": "owner"},
                  headers=h).status_code == 403


def test_the_demo_door_joins_a_seeded_org_and_never_makes_a_second(monkeypatch):
    seeded = service.signup("demo-owner@example.com", PASSWORD, "Ironwright Demo", time.time())
    monkeypatch.setenv("IRONWRIGHT_DEMO_ORG", seeded["org_slug"])
    got = _client().post("/auth/demo").json()
    assert got["org_id"] == seeded["org_id"] and got["role"] == "viewer"


def test_a_visitor_seat_cannot_be_signed_into_with_a_password(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEMO_ORG", "ironwright-demo")
    c = _client()
    seat = c.post("/auth/demo").json()
    assert c.post("/auth/login", json={"email": seat["email"], "password": PASSWORD}).status_code == 401


def test_the_demo_door_is_rate_limited_like_the_other_doors():
    assert "/auth/demo" in DOORS and "/auth/demo" in auth.PUBLIC_ROUTES


def test_a_demo_org_slug_that_is_not_a_slug_is_refused(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEMO_ORG", "Not A Slug!")
    with pytest.raises(ValueError, match="IRONWRIGHT_DEMO_ORG"):
        create_platform_app()


# ---------------------------------------------------------------- prod posture
def test_prod_boots_with_everything_set(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_ENV", "prod")
    for k, v in PROD.items():
        monkeypatch.setenv(k, v)
    hosted.check_posture()


@pytest.mark.parametrize("missing", ["IRONWRIGHT_SECRET_KEY", "IRONWRIGHT_DB_URL", "IRONWRIGHT_STATE_URI",
                                     "IRONWRIGHT_LAKE_URI"])
def test_prod_refuses_to_boot_without_its_secrets(monkeypatch, missing):
    monkeypatch.setenv("IRONWRIGHT_ENV", "prod")
    for k, v in PROD.items():
        if k != missing:
            monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match=missing):
        hosted.check_posture()


def test_prod_refuses_a_sqlite_database(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_ENV", "prod")
    for k, v in PROD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("IRONWRIGHT_DB_URL", "sqlite:////tmp/meta.sqlite")
    with pytest.raises(SystemExit, match="IRONWRIGHT_DB_URL"):
        hosted.check_posture()


def test_prod_refuses_a_bad_signup_mode_at_boot(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_ENV", "prod")
    for k, v in PROD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "sometimes")
    with pytest.raises(SystemExit, match="IRONWRIGHT_SIGNUP"):
        hosted.check_posture()


def test_staging_is_not_held_to_prod(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_ENV", "staging")
    hosted.check_posture()
