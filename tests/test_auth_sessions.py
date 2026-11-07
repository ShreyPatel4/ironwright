"""Tier 1 items 1 and 2 of docs/enterprise-security.md: cookie sessions, CSRF, rotation,
an absolute lifetime and sign out everywhere. Real routes, SQLite and Postgres.

The client talks https, because httpx will not send a Secure cookie over plain http.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ironwright import auth
from ironwright.tenancy import service

PASSWORD = "correct-horse-9"
HOUR = 3600


@pytest.fixture()
def app(meta_db):
    from ironwright.platform_api import create_platform_app
    return create_platform_app()


@pytest.fixture()
def client(app):
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def fresh(app) -> TestClient:
    """A second browser: its own cookie jar."""
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def signup(c, email="owner@a.example", org="Acme Claims"):
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    assert r.status_code == 201, r.text
    return r.json()


def login(c, email="owner@a.example"):
    r = c.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def csrf(c, s) -> dict[str, str]:
    return {"X-CSRF-Token": s["csrf"]}


# ----- the cookie

def test_login_sets_an_httponly_secure_lax_session_cookie(client):
    signup(client)
    r = client.post("/auth/login", json={"email": "owner@a.example", "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.SESSION_COOKIE}=")
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=lax" in low and "path=/" in low


def test_signup_and_accept_set_the_cookie_too(client):
    s = signup(client)
    assert client.cookies.get(auth.SESSION_COOKIE)
    r = client.post(f"/orgs/{s['org_slug']}/invites", headers=csrf(client, s),
                    json={"email": "m@a.example", "role": "member"})
    assert r.status_code == 201, r.text
    other = fresh(client.app)
    joined = other.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD})
    assert joined.status_code == 200, joined.text
    assert other.cookies.get(auth.SESSION_COOKIE)


def test_the_cookie_alone_opens_the_door_for_reads(client):
    signup(client)
    assert client.get("/auth/me").status_code == 200
    assert client.get("/workspaces").status_code == 200


def test_the_cookie_path_follows_the_base_path(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_BASE_PATH", "/projects/ironwright/app/")
    from ironwright.platform_api import create_platform_app
    with fresh(create_platform_app()) as c:
        r = c.post("/auth/signup", json={"email": "o@a.example", "password": PASSWORD, "org_name": "Acme"})
    assert "path=/projects/ironwright/app;" in r.headers["set-cookie"].lower()


def test_a_cookie_session_cannot_write_without_the_csrf_token(client):
    s = signup(client)
    body = {"email": "m@a.example", "role": "member"}
    assert client.post(f"/orgs/{s['org_slug']}/invites", json=body).status_code == 403
    wrong = client.post(f"/orgs/{s['org_slug']}/invites", json=body, headers={"X-CSRF-Token": "nope"})
    assert wrong.status_code == 403
    assert client.post(f"/orgs/{s['org_slug']}/invites", json=body, headers=csrf(client, s)).status_code == 201


def test_one_sessions_csrf_token_does_not_fit_another(client):
    s = signup(client)
    other = fresh(client.app)
    theirs = login(other)
    assert theirs["csrf"] != s["csrf"]
    r = client.post(f"/orgs/{s['org_slug']}/invites", json={"email": "m@a.example", "role": "member"},
                    headers=csrf(client, theirs))
    assert r.status_code == 403


def test_logout_by_cookie_needs_csrf_and_clears_the_cookie(client):
    s = signup(client)
    assert client.post("/auth/logout").status_code == 403
    r = client.post("/auth/logout", headers=csrf(client, s))
    assert r.status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_a_dead_session_can_still_clear_its_cookie(client):
    s = signup(client)
    service.revoke(s["token"])
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert f'{auth.SESSION_COOKIE}=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()


def test_bearer_clients_need_no_csrf(client):
    s = signup(client)
    api = fresh(client.app)
    r = api.post(f"/orgs/{s['org_slug']}/invites", json={"email": "m@a.example", "role": "member"},
                 headers={"Authorization": f"Bearer {s['token']}"})
    assert r.status_code == 201, r.text


def test_an_authorization_header_never_falls_back_to_the_cookie(client):
    signup(client)
    r = client.get("/workspaces", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_me_hands_a_cookie_session_its_csrf_token(client):
    s = signup(client)
    assert client.get("/auth/me").json()["csrf"] == s["csrf"]


# ----- rotation

def test_a_login_over_an_existing_cookie_replaces_that_session(client):
    first = signup(client)
    again = login(client)
    assert again["token"] != first["token"]
    assert service.resolve(first["token"], auth._now()) is None
    assert service.resolve(again["token"], auth._now()) is not None


def test_a_second_device_keeps_the_first_signed_in(client):
    first = signup(client)
    login(fresh(client.app))
    assert service.resolve(first["token"], auth._now()) is not None


def test_switching_org_rotates_the_session(client):
    s = signup(client)
    other = fresh(client.app)
    b = signup(other, email="b@b.example", org="Beta Org")
    r = other.post(f"/orgs/{b['org_slug']}/invites", headers=csrf(other, b),
                   json={"email": "owner@a.example", "role": "member"})
    joined = fresh(client.app).post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD})
    assert joined.status_code == 200
    s = login(client)
    moved = client.post(f"/orgs/{b['org_slug']}/switch", headers=csrf(client, s))
    assert moved.status_code == 200, moved.text
    assert moved.json()["token"] != s["token"]
    assert service.resolve(s["token"], auth._now()) is None
    assert client.get("/auth/me").json()["org_slug"] == b["org_slug"]


def test_a_role_change_ends_the_members_sessions(client):
    s = signup(client)
    r = client.post(f"/orgs/{s['org_slug']}/invites", headers=csrf(client, s),
                    json={"email": "m@a.example", "role": "member"})
    m = fresh(client.app).post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    assert client.put(f"/orgs/{s['org_slug']}/members/{m['user_id']}", json={"role": "viewer"},
                      headers=csrf(client, s)).status_code == 200
    assert service.resolve(m["token"], auth._now()) is None


# ----- lifetimes

def test_the_session_dies_after_12_hours_however_busy(client, monkeypatch):
    s = signup(client)
    start = auth._now()
    for hours in range(1, 12):
        monkeypatch.setattr(auth, "_now", lambda h=hours: start + h * HOUR)
        assert client.get("/auth/me").status_code == 200, hours
    monkeypatch.setattr(auth, "_now", lambda: start + 12 * HOUR)
    assert client.get("/auth/me").status_code == 401
    assert service.resolve(s["token"], start + 12 * HOUR) is None


# ----- sessions list and sign out everywhere

def test_the_sessions_list_shows_every_device_and_marks_this_one(client):
    signup(client)
    login(fresh(client.app))
    listed = client.get("/auth/sessions").json()
    assert len(listed) == 2
    assert sum(1 for x in listed if x["current"]) == 1
    assert all("token" not in x and "token_hash" not in x for x in listed)


def test_one_session_can_be_signed_out_from_another(client):
    s = signup(client)
    other = fresh(client.app)
    theirs = login(other)
    sid = next(x["id"] for x in client.get("/auth/sessions").json() if not x["current"])
    assert client.delete(f"/auth/sessions/{sid}", headers=csrf(client, s)).status_code == 200
    assert service.resolve(theirs["token"], auth._now()) is None
    assert client.get("/auth/me").status_code == 200


def test_someone_elses_session_id_reads_as_not_found(client):
    signup(client)
    other = fresh(client.app)
    b = signup(other, email="b@b.example", org="Beta Org")
    theirs = other.get("/auth/sessions").json()[0]["id"]
    r = client.delete(f"/auth/sessions/{theirs}", headers=csrf(client, login(client)))
    assert r.status_code == 404
    assert service.resolve(b["token"], auth._now()) is not None


def test_sign_out_everywhere_ends_every_session_including_this_one(client):
    signup(client)
    other = fresh(client.app)
    theirs = login(other)
    mine = login(client)
    assert client.post("/auth/sessions/revoke-all", headers=csrf(client, mine)).status_code == 200
    assert service.resolve(theirs["token"], auth._now()) is None
    assert service.resolve(mine["token"], auth._now()) is None
