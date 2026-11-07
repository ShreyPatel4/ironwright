"""Tier 1 items 3 and 8 of docs/enterprise-security.md: the per email soft lock, the per
IP door limit, and auth events in the org's audit hash chain. Real routes, both backends."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ironwright import auth
from ironwright.api import DOORS
from ironwright.tenancy import usage

PASSWORD = "correct-horse-9"
MINUTE = 60


@pytest.fixture()
def client(meta_db):
    from ironwright.platform_api import create_platform_app
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def owner(client):
    r = client.post("/auth/signup", json={"email": "owner@a.example", "password": PASSWORD, "org_name": "Acme"})
    assert r.status_code == 201
    return r.json()


def attempt(c, email="owner@a.example", password="wrong-password-1"):
    return c.post("/auth/login", json={"email": email, "password": password})


def bearer(s):
    return {"Authorization": f"Bearer {s['token']}"}


def actions(org_id):
    return [r["action"] for r in usage.audit_log(org_id)]


def test_ten_failures_lock_the_email_even_against_the_right_password(client, owner):
    for _ in range(9):
        assert attempt(client).status_code == 401
    tenth = attempt(client)
    assert tenth.status_code == 401
    locked = attempt(client, password=PASSWORD)
    assert locked.status_code == 429
    assert "15 minutes" in locked.json()["detail"]


def test_an_unknown_email_locks_the_same_way_with_the_same_words(client, owner):
    for email in ("owner@a.example", "nobody@a.example"):
        for _ in range(10):
            attempt(client, email)
    known, unknown = attempt(client, "owner@a.example"), attempt(client, "nobody@a.example")
    assert (known.status_code, known.content) == (unknown.status_code, unknown.content)
    assert known.status_code == 429


def test_the_lock_lifts_after_15_minutes(client, owner, monkeypatch):
    start = auth._now()
    for _ in range(10):
        attempt(client)
    monkeypatch.setattr(auth, "_now", lambda: start + 15 * MINUTE + 1)
    assert attempt(client, password=PASSWORD).status_code == 200


def test_failures_spread_past_the_window_do_not_lock(client, owner, monkeypatch):
    start = auth._now()
    for i in range(18):
        monkeypatch.setattr(auth, "_now", lambda i=i: start + i * 2 * MINUTE)
        assert attempt(client).status_code == 401
    assert attempt(client, password=PASSWORD).status_code == 200


def test_a_good_sign_in_clears_the_count(client, owner):
    for _ in range(9):
        attempt(client)
    assert attempt(client, password=PASSWORD).status_code == 200
    for _ in range(9):
        attempt(client)
    assert attempt(client, password=PASSWORD).status_code == 200


def test_another_email_is_not_locked_by_this_one(client, owner):
    for _ in range(10):
        attempt(client, "nobody@a.example")
    assert attempt(client, password=PASSWORD).status_code == 200


def test_a_known_email_accepting_an_invite_counts_toward_the_lock(client, owner):
    other = client.post("/auth/signup", json={"email": "b@b.example", "password": PASSWORD, "org_name": "Beta"}).json()
    for _ in range(10):
        r = client.post(f"/orgs/{other['org_slug']}/invites", headers=bearer(other),
                        json={"email": "owner@a.example", "role": "member"})
        assert client.post("/auth/accept-invite", json={"token": r.json()["token"],
                                                         "password": "wrong-password-1"}).status_code == 401
    assert attempt(client, password=PASSWORD).status_code == 429


# ----- the audit chain

def test_sign_in_failure_and_lock_land_in_the_org_chain(client, owner):
    attempt(client, password=PASSWORD)
    for _ in range(10):
        attempt(client)
    got = actions(owner["org_id"])
    assert got.count("auth.signup") == 1 and got.count("auth.login") == 1
    assert got.count("auth.fail") == 10
    assert got.count("auth.lock") == 1
    assert usage.verify_chain(owner["org_id"])["verified"]


def test_a_failure_row_names_no_password(client, owner):
    attempt(client, password="the-wrong-secret-1")
    rows = usage.audit_chain(owner["org_id"], action="auth.fail")["rows"]
    assert rows and "the-wrong-secret-1" not in str(rows)


def test_sign_out_everywhere_is_audited(client, owner):
    assert client.post("/auth/sessions/revoke-all", headers=bearer(owner)).status_code == 200
    assert "auth.signout_all" in actions(owner["org_id"])


def test_an_admin_can_export_the_chain_with_its_hashes(client, owner):
    attempt(client)
    r = client.get(f"/orgs/{owner['org_slug']}/audit/export", headers=bearer(owner))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    lines = [__import__("json").loads(x) for x in r.text.splitlines()]
    assert lines[-1]["verified"] is True
    rows = lines[:-1]
    assert {"seq", "ts", "actor", "action", "detail", "prev_hash", "hash"} <= set(rows[0])
    assert any(x["action"] == "auth.fail" for x in rows)


def test_a_member_cannot_export_the_chain(client, owner):
    r = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                    json={"email": "m@a.example", "role": "member"})
    m = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    assert client.get(f"/orgs/{owner['org_slug']}/audit/export", headers=bearer(m)).status_code == 403


def test_the_chain_can_be_narrowed_to_auth_events(client, owner):
    attempt(client)
    r = client.get(f"/orgs/{owner['org_slug']}/audit/chain?prefix=auth.", headers=bearer(owner)).json()
    assert r["rows"] and all(x["action"].startswith("auth.") for x in r["rows"])


# ----- the per IP door

def test_every_password_door_is_rate_limited_per_ip():
    assert {"/auth/login", "/auth/signup", "/auth/accept-invite", "/auth/mfa",
            "/auth/reset", "/auth/reset-request"} <= DOORS
