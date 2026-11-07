"""Tier 1 item 5 of docs/enterprise-security.md: TOTP with recovery codes, and an org
owner can require it. Real routes, both backends, against the RFC 6238 vectors."""
from __future__ import annotations

import base64

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ironwright import auth
from ironwright.security import mfa, store
from ironwright.tenancy import service, usage

PASSWORD = "correct-horse-9"
RFC_SECRET = b"12345678901234567890"


@pytest.mark.parametrize("t,code", [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"),
                                    (2000000000, "279037"), (20000000000, "353130")])
def test_totp_matches_rfc_6238(t, code):
    assert mfa.totp(RFC_SECRET, t // 30) == code


@pytest.fixture()
def client(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    from ironwright.platform_api import create_platform_app
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        yield c


def bearer(s):
    return {"Authorization": f"Bearer {s['token']}"}


def signup(c, email="owner@a.example", org="Acme"):
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    assert r.status_code == 201, r.text
    return r.json()


def code_for(secret_b32: str, now: float | None = None, skew: int = 0) -> str:
    return mfa.totp(base64.b32decode(secret_b32), int((now or auth._now()) // 30) + skew)


def enrol(c, s) -> tuple[str, dict, list[str]]:
    """Returns the secret, the rotated session and the recovery codes."""
    r = c.post("/auth/mfa/enrol", headers=bearer(s))
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/Ironwright:")
    done = c.post("/auth/mfa/confirm", headers=bearer(s), json={"code": code_for(secret)})
    assert done.status_code == 200, done.text
    return secret, done.json()["session"], done.json()["recovery_codes"]


def login(c, email="owner@a.example"):
    return c.post("/auth/login", json={"email": email, "password": PASSWORD})


def later(monkeypatch, seconds):
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + seconds)


# ----- enrolment

def test_enrolment_needs_a_first_code_before_it_counts(client):
    s = signup(client)
    client.post("/auth/mfa/enrol", headers=bearer(s))
    assert client.get("/auth/mfa/status", headers=bearer(s)).json()["enabled"] is False
    assert "token" in login(client).json()


def test_a_wrong_first_code_does_not_enable_it(client):
    s = signup(client)
    client.post("/auth/mfa/enrol", headers=bearer(s))
    assert client.post("/auth/mfa/confirm", headers=bearer(s), json={"code": "000000"}).status_code == 422


def test_enrolment_rotates_the_session_and_ends_the_others(client):
    s = signup(client)
    other = login(client).json()
    _, rotated, _ = enrol(client, s)
    assert rotated["token"] != s["token"]
    assert service.resolve(s["token"], auth._now()) is None
    assert service.resolve(other["token"], auth._now()) is None
    assert service.resolve(rotated["token"], auth._now()) is not None


def test_ten_recovery_codes_shown_once_and_stored_as_hashes(client):
    s = signup(client)
    secret, _, codes = enrol(client, s)
    assert len(codes) == 10 and len(set(codes)) == 10
    with store.engine().connect() as con:
        dump = str(con.execute(sa.select(store.user_mfa)).all())
    assert not any(c in dump for c in codes)
    assert secret not in dump


def test_enrolment_is_refused_without_a_server_secret(client, monkeypatch):
    s = signup(client)
    monkeypatch.delenv("IRONWRIGHT_SECRET_KEY")
    assert client.post("/auth/mfa/enrol", headers=bearer(s)).status_code == 503


def test_enrolment_is_audited(client):
    s = signup(client)
    enrol(client, s)
    assert "mfa.enable" in [x["action"] for x in usage.audit_log(s["org_id"])]


# ----- the challenge

def test_a_password_alone_buys_a_ticket_and_no_session(client):
    s = signup(client)
    enrol(client, s)
    r = login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True and "token" not in body and "ticket" in body
    assert "set-cookie" not in r.headers
    assert client.get("/workspaces", headers={"Authorization": f"Bearer {body['ticket']}"}).status_code == 401


def test_the_ticket_and_a_code_open_a_session(client):
    s = signup(client)
    secret, _, _ = enrol(client, s)
    ticket = login(client).json()["ticket"]
    r = client.post("/auth/mfa", json={"ticket": ticket, "code": code_for(secret, skew=1)})
    assert r.status_code == 200, r.text
    assert service.resolve(r.json()["token"], auth._now()) is not None
    assert "set-cookie" in r.headers


def test_a_code_cannot_be_used_twice(client):
    s = signup(client)
    secret, _, _ = enrol(client, s)
    used = code_for(secret)  # the one confirm spent
    ticket = login(client).json()["ticket"]
    assert client.post("/auth/mfa", json={"ticket": ticket, "code": used}).status_code == 401


def test_a_ticket_works_once(client, monkeypatch):
    s = signup(client)
    secret, _, _ = enrol(client, s)
    ticket = login(client).json()["ticket"]
    later(monkeypatch, 30)
    assert client.post("/auth/mfa", json={"ticket": ticket, "code": code_for(secret)}).status_code == 200
    later(monkeypatch, 90)
    assert client.post("/auth/mfa", json={"ticket": ticket, "code": code_for(secret)}).status_code == 401


def test_a_ticket_dies_after_5_minutes(client, monkeypatch):
    s = signup(client)
    secret, _, _ = enrol(client, s)
    ticket = login(client).json()["ticket"]
    later(monkeypatch, 5 * 60)
    assert client.post("/auth/mfa", json={"ticket": ticket, "code": code_for(secret)}).status_code == 401


def test_five_wrong_codes_burn_the_ticket(client):
    s = signup(client)
    secret, _, _ = enrol(client, s)
    ticket = login(client).json()["ticket"]
    for _ in range(5):
        assert client.post("/auth/mfa", json={"ticket": ticket, "code": "000000"}).status_code == 401
    assert client.post("/auth/mfa", json={"ticket": ticket, "code": code_for(secret, skew=1)}).status_code == 401


def test_wrong_codes_count_toward_the_email_lock(client):
    s = signup(client)
    enrol(client, s)
    for _ in range(2):
        ticket = login(client).json()["ticket"]
        for _ in range(5):
            client.post("/auth/mfa", json={"ticket": ticket, "code": "000000"})
    assert login(client).status_code == 429


def test_a_recovery_code_works_once_instead_of_a_code(client):
    s = signup(client)
    _, _, codes = enrol(client, s)
    ticket = login(client).json()["ticket"]
    assert client.post("/auth/mfa", json={"ticket": ticket, "recovery_code": codes[0]}).status_code == 200
    ticket = login(client).json()["ticket"]
    assert client.post("/auth/mfa", json={"ticket": ticket, "recovery_code": codes[0]}).status_code == 401
    me = client.post("/auth/mfa", json={"ticket": ticket, "recovery_code": codes[1]}).json()
    assert client.get("/auth/mfa/status", headers=bearer(me)).json()["recovery_left"] == 8
    assert "mfa.recovery_used" in [x["action"] for x in usage.audit_log(s["org_id"])]


def test_a_known_user_with_mfa_accepting_an_invite_gets_a_ticket(client):
    s = signup(client)
    enrol(client, s)
    b = signup(client, "b@b.example", "Beta")
    r = client.post(f"/orgs/{b['org_slug']}/invites", headers=bearer(b), json={"email": "owner@a.example", "role": "member"})
    joined = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    assert joined["mfa_required"] is True and "token" not in joined


# ----- turning it off

def test_turning_it_off_needs_a_current_code(client, monkeypatch):
    s = signup(client)
    secret, rotated, _ = enrol(client, s)
    assert client.post("/auth/mfa/disable", headers=bearer(rotated), json={"code": "000000"}).status_code == 422
    later(monkeypatch, 30)
    assert client.post("/auth/mfa/disable", headers=bearer(rotated), json={"code": code_for(secret)}).status_code == 200
    assert "token" in login(client).json()
    assert "mfa.disable" in [x["action"] for x in usage.audit_log(s["org_id"])]


# ----- the org switch

def test_only_an_enrolled_owner_can_require_mfa(client):
    s = signup(client)
    r = client.put(f"/orgs/{s['org_slug']}/security", headers=bearer(s), json={"require_mfa": True})
    assert r.status_code == 409
    _, rotated, _ = enrol(client, s)
    r = client.put(f"/orgs/{s['org_slug']}/security", headers=bearer(rotated), json={"require_mfa": True})
    assert r.status_code == 200, r.text
    assert client.get(f"/orgs/{s['org_slug']}/security", headers=bearer(rotated)).json()["require_mfa"] is True
    assert "org.require_mfa" in [x["action"] for x in usage.audit_log(s["org_id"])]


def test_an_admin_cannot_flip_the_switch(client):
    s = signup(client)
    _, owner, _ = enrol(client, s)
    r = client.post(f"/orgs/{s['org_slug']}/invites", headers=bearer(owner), json={"email": "ad@a.example", "role": "admin"})
    admin = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    assert client.put(f"/orgs/{s['org_slug']}/security", headers=bearer(admin),
                      json={"require_mfa": True}).status_code == 403


def _required_org(client):
    s = signup(client)
    _, owner, _ = enrol(client, s)
    assert client.put(f"/orgs/{s['org_slug']}/security", headers=bearer(owner),
                      json={"require_mfa": True}).status_code == 200
    return owner


def test_a_member_without_mfa_can_only_set_it_up(client):
    owner = _required_org(client)
    r = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner), json={"email": "m@a.example", "role": "member"})
    m = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    blocked = client.get("/workspaces", headers=bearer(m))
    assert blocked.status_code == 403 and "two-step" in blocked.json()["detail"]
    assert client.get("/auth/me", headers=bearer(m)).json()["mfa_setup_required"] is True
    _, rotated, _ = enrol(client, m)
    assert client.get("/workspaces", headers=bearer(rotated)).status_code == 200


def test_switching_into_a_required_org_is_not_a_side_door(client):
    other = signup(client, "m@a.example", "Other org")  # older, so sign in lands here
    owner = _required_org(client)
    r = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner), json={"email": "m@a.example", "role": "member"})
    joined = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    assert joined["org_slug"] == owner["org_slug"]
    mine = login(client, "m@a.example").json()
    assert mine["org_slug"] == other["org_slug"]
    assert client.get("/workspaces", headers=bearer(mine)).status_code == 200
    moved = client.post(f"/orgs/{owner['org_slug']}/switch", headers=bearer(mine)).json()
    assert client.get("/workspaces", headers=bearer(moved)).status_code == 403


def test_nobody_can_turn_mfa_off_while_an_org_requires_it(client, monkeypatch):
    owner = _required_org(client)
    secret = None  # the owner's secret is not needed: the refusal comes first
    r = client.post("/auth/mfa/disable", headers=bearer(owner), json={"code": secret or "123456"})
    assert r.status_code == 409
