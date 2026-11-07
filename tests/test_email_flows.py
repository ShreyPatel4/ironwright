"""Tier 1 item 6 of docs/enterprise-security.md: verify, reset and invite links. Single
use, 30 minutes, only the hash stored, sent through the outbox. The outbox writes to a
file locally and to a provider set by env. Real routes, both backends."""
from __future__ import annotations

import json
import re

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from ironwright import auth
from ironwright.security import outbox, store
from ironwright.tenancy import service, usage

PASSWORD = "correct-horse-9"
NEW_PASSWORD = "a-brand-new-secret-7"
MINUTE = 60


@pytest.fixture()
def client(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_PUBLIC_URL", "https://app.example")
    from ironwright.platform_api import create_platform_app
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        yield c


def mail() -> list[dict]:
    path = outbox.file_path()
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def token_in(message: dict, kind: str) -> str:
    return re.search(rf"#{kind}=([A-Za-z0-9_-]+)", message["text"]).group(1)


def last_to(email: str) -> dict:
    return [m for m in mail() if m["to"] == email][-1]


def signup(c, email="owner@a.example"):
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": "Acme"})
    assert r.status_code == 201, r.text
    return r.json()


def bearer(s):
    return {"Authorization": f"Bearer {s['token']}"}


# ----- verify

def test_signup_mails_a_verify_link_and_it_works_once(client):
    s = signup(client)
    assert client.get("/auth/me", headers=bearer(s)).json()["email_verified"] is False
    token = token_in(last_to("owner@a.example"), "verify")
    assert client.post("/auth/verify", json={"token": token}).status_code == 200
    assert client.get("/auth/me", headers=bearer(s)).json()["email_verified"] is True
    assert client.post("/auth/verify", json={"token": token}).status_code == 404


def test_a_verify_link_dies_after_30_minutes(client, monkeypatch):
    signup(client)
    token = token_in(last_to("owner@a.example"), "verify")
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + 30 * MINUTE)
    assert client.post("/auth/verify", json={"token": token}).status_code == 404


def test_only_the_hash_of_a_link_is_stored(client):
    signup(client)
    token = token_in(last_to("owner@a.example"), "verify")
    with store.engine().connect() as con:
        dump = str(con.execute(sa.select(store.auth_links)).all())
    assert token not in dump


def test_an_unverified_email_can_still_sign_in(client):
    signup(client)
    assert client.post("/auth/login", json={"email": "owner@a.example", "password": PASSWORD}).status_code == 200


def test_a_signup_on_a_taken_email_mails_its_owner_and_no_link(client):
    signup(client)
    before = len(mail())
    again = client.post("/auth/signup", json={"email": "owner@a.example", "password": NEW_PASSWORD,
                                              "org_name": "Mallory"})
    assert again.status_code == 201
    sent = mail()[before:]
    assert [m["to"] for m in sent] == ["owner@a.example"]
    assert "#verify=" not in sent[0]["text"] and "#reset=" not in sent[0]["text"]


def test_a_new_verify_link_can_be_asked_for(client):
    s = signup(client)
    assert client.post("/auth/verify/resend", headers=bearer(s)).status_code == 200
    token = token_in(last_to("owner@a.example"), "verify")
    assert client.post("/auth/verify", json={"token": token}).status_code == 200


# ----- reset

def test_reset_request_answers_the_same_for_a_stranger_and_mails_nobody(client):
    signup(client)
    before = len(mail())
    known = client.post("/auth/reset-request", json={"email": "owner@a.example"})
    sent = len(mail()) - before
    stranger = client.post("/auth/reset-request", json={"email": "nobody@a.example"})
    assert (known.status_code, known.content) == (stranger.status_code, stranger.content)
    assert known.status_code == 202
    assert sent == 1 and len(mail()) - before == 1


def test_a_reset_sets_the_password_and_ends_every_session(client):
    s = signup(client)
    client.post("/auth/reset-request", json={"email": "owner@a.example"})
    token = token_in(last_to("owner@a.example"), "reset")
    r = client.post("/auth/reset", json={"token": token, "password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    assert "token" not in r.json()
    assert service.resolve(s["token"], auth._now()) is None
    old = client.post("/auth/login", json={"email": "owner@a.example", "password": PASSWORD})
    assert old.status_code == 401
    new = client.post("/auth/login", json={"email": "owner@a.example", "password": NEW_PASSWORD})
    assert new.status_code == 200
    assert "auth.password_reset" in [x["action"] for x in usage.audit_log(s["org_id"])]


def test_a_reset_link_works_once(client):
    signup(client)
    client.post("/auth/reset-request", json={"email": "owner@a.example"})
    token = token_in(last_to("owner@a.example"), "reset")
    assert client.post("/auth/reset", json={"token": token, "password": NEW_PASSWORD}).status_code == 200
    assert client.post("/auth/reset", json={"token": token, "password": "another-new-one-8"}).status_code == 404


def test_a_reset_link_dies_after_30_minutes(client, monkeypatch):
    signup(client)
    client.post("/auth/reset-request", json={"email": "owner@a.example"})
    token = token_in(last_to("owner@a.example"), "reset")
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + 30 * MINUTE)
    assert client.post("/auth/reset", json={"token": token, "password": NEW_PASSWORD}).status_code == 404


def test_a_weak_new_password_is_refused_and_the_link_survives(client):
    signup(client)
    client.post("/auth/reset-request", json={"email": "owner@a.example"})
    token = token_in(last_to("owner@a.example"), "reset")
    assert client.post("/auth/reset", json={"token": token, "password": "qwerty123456"}).status_code == 422
    assert client.post("/auth/reset", json={"token": token, "password": NEW_PASSWORD}).status_code == 200


def test_a_reset_lifts_the_lock(client):
    signup(client)
    for _ in range(10):
        client.post("/auth/login", json={"email": "owner@a.example", "password": "wrong-password-1"})
    client.post("/auth/reset-request", json={"email": "owner@a.example"})
    token = token_in(last_to("owner@a.example"), "reset")
    client.post("/auth/reset", json={"token": token, "password": NEW_PASSWORD})
    assert client.post("/auth/login", json={"email": "owner@a.example", "password": NEW_PASSWORD}).status_code == 200


def test_reset_mail_per_email_is_capped(client):
    signup(client)
    before = len(mail())
    answers = {client.post("/auth/reset-request", json={"email": "owner@a.example"}).status_code for _ in range(6)}
    assert answers == {202}
    assert len(mail()) - before == 3


def test_the_link_uses_the_configured_origin_and_base_path_never_the_host(client, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_BASE_PATH", "/projects/ironwright/app")
    signup(client)
    client.post("/auth/reset-request", json={"email": "owner@a.example"}, headers={"host": "evil.example"})
    text = last_to("owner@a.example")["text"]
    assert "https://app.example/projects/ironwright/app/console/#reset=" in text
    assert "evil.example" not in text


# ----- invites

def test_an_invite_is_mailed_and_lives_30_minutes(client, monkeypatch):
    s = signup(client)
    r = client.post(f"/orgs/{s['org_slug']}/invites", headers=bearer(s), json={"email": "m@a.example", "role": "member"})
    assert r.status_code == 201
    token = token_in(last_to("m@a.example"), "invite")
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + 30 * MINUTE)
    assert client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).status_code == 404


def test_a_mailed_invite_can_be_accepted_once(client):
    s = signup(client)
    client.post(f"/orgs/{s['org_slug']}/invites", headers=bearer(s), json={"email": "m@a.example", "role": "member"})
    token = token_in(last_to("m@a.example"), "invite")
    assert client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).status_code == 200
    assert client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).status_code == 404


# ----- the provider

def test_the_provider_is_chosen_by_env_and_gets_the_message(monkeypatch, meta_db):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "m1"})

    monkeypatch.setenv("IRONWRIGHT_MAIL", "resend")
    monkeypatch.setenv("IRONWRIGHT_MAIL_KEY", "re_test_key_123")
    monkeypatch.setenv("IRONWRIGHT_MAIL_FROM", "Ironwright <no-reply@app.example>")
    monkeypatch.setattr(outbox, "_transport", httpx.MockTransport(handler))
    outbox.send("a@b.example", "Hello", "Body text")
    assert len(seen) == 1
    assert seen[0].url == "https://api.resend.com/emails"
    assert seen[0].headers["authorization"] == "Bearer re_test_key_123"
    assert json.loads(seen[0].content) == {"from": "Ironwright <no-reply@app.example>", "to": ["a@b.example"],
                                           "subject": "Hello", "text": "Body text"}
    assert not outbox.file_path().exists()


def test_an_unknown_provider_is_refused(monkeypatch, meta_db):
    monkeypatch.setenv("IRONWRIGHT_MAIL", "carrier-pigeon")
    with pytest.raises(outbox.MailError):
        outbox.send("a@b.example", "Hello", "Body")


def test_the_local_mailbox_is_private(client):
    signup(client)
    assert outbox.file_path().stat().st_mode & 0o077 == 0
