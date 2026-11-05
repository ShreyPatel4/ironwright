"""Per-org limits, the usage they read, and an org's own model key.

Every limit is tried three times: the last request that fits, the first that does
not, and the counter afterwards, which must not have moved. A refusal that still
counts would lock an org out sooner than its plan says.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ironwright import auth, platform_api
from ironwright.config import uploads_root
from ironwright.jobs import handlers
from ironwright.llm import DEFAULT_MODELS, resolve_llm
from ironwright.ontology.runtime import Ontology
from ironwright.packs import Pack
from ironwright.platform_api import NOT_FOUND, create_platform_app
from ironwright.tenancy import byokey, quotas, service, usage
from ironwright.tenancy.models import DEV_ORG_ID

PASSWORD = "correct-horse-9"
NOON = 1_790_000_000.0  # a fixed moment, well inside one UTC day
NEXT_DAY = NOON + 86_400


@pytest.fixture()
def org(meta_db) -> str:
    return service.signup("owner@a.example", PASSWORD, "Acme", NOON)["org_id"]


def test_a_charge_fits_up_to_the_limit_and_a_refusal_counts_nothing(org):
    assert usage.charge(org, "runs", 2, NOON) is True
    assert usage.charge(org, "runs", 2, NOON) is True
    assert usage.charge(org, "runs", 2, NOON) is False
    assert usage.used(org, "runs", NOON) == 2


def test_a_charge_bigger_than_what_is_left_is_refused_whole(org):
    assert usage.charge(org, "runs", 10, NOON, n=9) is True
    assert usage.charge(org, "runs", 10, NOON, n=2) is False
    assert usage.used(org, "runs", NOON) == 9


def test_the_count_starts_again_the_next_utc_day(org):
    assert usage.charge(org, "runs", 1, NOON) is True
    assert usage.charge(org, "runs", 1, NOON) is False
    assert usage.charge(org, "runs", 1, NEXT_DAY) is True


def test_two_orgs_never_share_a_counter(org):
    other = service.signup("b@b.example", PASSWORD, "Birch", NOON)["org_id"]
    assert usage.charge(org, "runs", 1, NOON) is True
    assert usage.charge(other, "runs", 1, NOON) is True
    assert usage.used(other, "runs", NOON) == 1


def test_racing_charges_never_pass_the_limit(org):
    """Every thread must finish, or a dead one hides the race (build log #15)."""
    start, wins, errors = threading.Barrier(8), [], []

    def spend() -> None:
        try:
            start.wait(10)
            for _ in range(5):
                if usage.charge(org, "runs", 10, NOON):
                    wins.append(1)
        except Exception as e:  # noqa: BLE001  any thread error fails the test below
            errors.append(e)

    threads = [threading.Thread(target=spend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert len(wins) == 10
    assert usage.used(org, "runs", NOON) == 10


def test_a_new_org_is_on_free(org):
    assert quotas.plan_of(org) == "free"
    assert quotas.limit(org, "runs") == quotas.PLANS["free"]["runs"]


@pytest.mark.parametrize("plan", ["free", "team"])
def test_a_plan_names_every_limit(plan):
    assert set(quotas.PLANS[plan]) == {"storage_bytes", "uploads", "runs", "ask", "llm_tokens", "members"}


def test_team_is_more_than_free_on_every_limit():
    for metric, free in quotas.PLANS["free"].items():
        assert quotas.PLANS["team"][metric] > free, metric


def test_a_plan_change_applies_and_is_audited(org):
    quotas.set_plan(org, "team", "operator:shrey", NOON)
    assert quotas.plan_of(org) == "team"
    assert quotas.limit(org, "runs") == quotas.PLANS["team"]["runs"]
    row = usage.audit_log(org)[-1]
    assert (row["action"], row["actor"]) == ("plan.change", "operator:shrey")
    assert row["detail"] == {"from": "free", "to": "team"}


def test_an_unknown_plan_is_refused_and_changes_nothing(org):
    with pytest.raises(ValueError, match="no plan"):
        quotas.set_plan(org, "enterprise", "operator:shrey", NOON)
    assert quotas.plan_of(org) == "free"
    assert usage.audit_log(org) == []


def test_the_dev_org_has_no_limits(meta_db):
    assert quotas.limit(DEV_ORG_ID, "runs") is None
    for _ in range(3):
        quotas.spend(DEV_ORG_ID, "runs", NOON)
    assert usage.used(DEV_ORG_ID, "runs", NOON) == 0


# ----- the doors, over HTTP

TIGHT = {"storage_bytes": 1000, "uploads": 2, "runs": 2, "ask": 2, "llm_tokens": 100, "members": 3}


def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


def join(c, owner, role, email=None) -> dict:
    tok = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                 json={"email": email or f"{role}@a.example", "role": role}).json()["token"]
    return c.post("/auth/accept-invite", json={"token": tok, "password": PASSWORD}).json()


@pytest.fixture()
def api(meta_db, monkeypatch):
    """The real app, with a free plan small enough to reach."""
    monkeypatch.setitem(quotas.PLANS, "free", dict(TIGHT))
    monkeypatch.setattr(auth, "_now", lambda: NOON)
    with TestClient(create_platform_app()) as c:
        a = c.post("/auth/signup", json={"email": "a@a.example", "password": PASSWORD, "org_name": "Org A"}).json()
        b = c.post("/auth/signup", json={"email": "b@b.example", "password": PASSWORD, "org_name": "Org B"}).json()
        ws = c.post("/workspaces", headers=bearer(a), json={"name": "ws-a", "size": 4, "seed": 3}).json()["name"]
        yield c, a, b, ws


def test_runs_through_post_jobs_trip_at_the_boundary(api):
    c, a, _, ws = api
    body = {"kind": "run", "workspace": ws}
    assert [c.post("/jobs", headers=bearer(a), json=body).status_code for _ in range(2)] == [200, 200]
    over = c.post("/jobs", headers=bearer(a), json=body)
    assert over.status_code == 429
    assert "2 runs" in over.json()["detail"] and "free" in over.json()["detail"]
    assert int(over.headers["retry-after"]) > 0
    assert usage.used(a["org_id"], "runs", NOON) == 2


def test_runs_through_the_console_door_share_the_same_count(api, monkeypatch):
    c, a, _, ws = api
    monkeypatch.setattr("ironwright.jobs.routes.wt.load_workspace", lambda org, name: None)
    path = f"/workspaces/{ws}/runs"
    assert c.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": ws}).status_code == 200
    assert c.post(path, headers=bearer(a), json={"wait": False}).status_code == 202
    assert c.post(path, headers=bearer(a), json={"wait": False}).status_code == 429
    assert usage.used(a["org_id"], "runs", NOON) == 2


def test_a_profile_job_is_not_a_run(api):
    c, a, _, ws = api
    for _ in range(3):
        assert c.post("/jobs", headers=bearer(a), json={"kind": "profile", "workspace": ws}).status_code == 200
    assert usage.used(a["org_id"], "runs", NOON) == 0


def test_a_request_refused_for_another_reason_costs_nothing(api):
    c, a, b, ws = api
    assert c.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": ws,
                                                    "params": {"stages": ["nope"]}}).status_code == 422
    assert c.post("/jobs", headers=bearer(b), json={"kind": "run", "workspace": ws}).status_code == 404
    assert usage.used(a["org_id"], "runs", NOON) == 0
    assert usage.used(b["org_id"], "runs", NOON) == 0


def test_an_org_out_of_runs_still_learns_nothing_about_another_org(api):
    c, _, b, ws = api
    for _ in range(2):
        usage.charge(b["org_id"], "runs", 2, NOON)
    assert c.post("/jobs", headers=bearer(b), json={"kind": "run", "workspace": ws}).status_code == 404


def test_uploads_trip_at_the_boundary(api):
    c, a, _, _ = api
    send = {"files": [("files", ("one.csv", b"id\n1\n"))]}
    assert [c.post("/uploads", headers=bearer(a), **send).status_code for _ in range(2)] == [201, 201]
    over = c.post("/uploads", headers=bearer(a), **send)
    assert over.status_code == 429 and "2 uploads" in over.json()["detail"]
    assert usage.used(a["org_id"], "uploads", NOON) == 2


def _stored(org_id: str, n: int) -> None:
    d = uploads_root(org_id) / ("a" * 32)
    d.mkdir(parents=True)
    (d / "held.csv").write_bytes(b"x" * n)


def test_storage_fits_exactly_and_one_byte_more_is_refused(api):
    _, a, _, _ = api
    org = a["org_id"]
    _stored(org, 600)
    quotas.start_upload(org, "400", NOON)
    with pytest.raises(quotas.QuotaExceeded, match="storage"):
        quotas.start_upload(org, "401", NOON)
    assert usage.used(org, "uploads", NOON) == 1, "a storage refusal must not spend an upload"


@pytest.mark.parametrize("length", [None, "not-a-number", "-5", "0"])
def test_a_full_org_is_refused_whatever_length_the_request_claims(api, length):
    """A chunked body has no length. It can pass the limit by one request, T3's caps
    bound that request, and then the org is full."""
    _, a, _, _ = api
    _stored(a["org_id"], 1000)
    with pytest.raises(quotas.QuotaExceeded, match="storage"):
        quotas.start_upload(a["org_id"], length, NOON)


def test_the_upload_door_applies_the_storage_limit(api):
    c, a, _, _ = api
    _stored(a["org_id"], 1000)
    r = c.post("/uploads", headers=bearer(a), files=[("files", ("one.csv", b"id\n1\n"))])
    assert r.status_code == 429 and "storage" in r.json()["detail"]
    assert usage.used(a["org_id"], "uploads", NOON) == 0


def test_a_viewer_turned_away_at_the_door_spends_no_upload(api):
    c, a, _, _ = api
    v = join(c, a, "viewer")
    assert c.post("/uploads", headers=bearer(v), files=[("files", ("one.csv", b"id\n1\n"))]).status_code == 403
    assert usage.used(a["org_id"], "uploads", NOON) == 0


def test_a_request_over_the_upload_caps_spends_no_upload(api, monkeypatch):
    c, a, _, _ = api
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILES", "1")
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILE_BYTES", "1")
    r = c.post("/uploads", headers=bearer(a), files=[("files", ("one.csv", b"x" * 40_000))])
    assert r.status_code == 413
    assert usage.used(a["org_id"], "uploads", NOON) == 0


def _invite(c, owner, email, role="member"):
    return c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner), json={"email": email, "role": role})


def test_members_trip_at_the_boundary_and_a_live_invite_holds_a_seat(api):
    c, a, _, _ = api
    assert _invite(c, a, "two@a.example").status_code == 201  # owner, plus this one
    assert _invite(c, a, "three@a.example").status_code == 201
    over = _invite(c, a, "four@a.example")
    assert over.status_code == 429 and "3" in over.json()["detail"] and "member" in over.json()["detail"]


def test_a_person_who_already_has_a_seat_needs_no_new_one(api):
    c, a, _, _ = api
    join(c, a, "member", "two@a.example")
    assert _invite(c, a, "three@a.example").status_code == 201
    assert _invite(c, a, "three@a.example").status_code == 201, "a new invite replaces the old one"
    assert _invite(c, a, "Two@A.example", "admin").status_code == 201, "a role change is not a new member"


def test_an_expired_invite_gives_its_seat_back(api, monkeypatch):
    c, a, _, _ = api
    assert _invite(c, a, "two@a.example").status_code == 201
    assert _invite(c, a, "three@a.example").status_code == 201
    monkeypatch.setattr(auth, "_now", lambda: NOON + 8 * 86_400)
    a = c.post("/auth/login", json={"email": "a@a.example", "password": PASSWORD}).json()
    assert _invite(c, a, "four@a.example").status_code == 201


def test_a_bad_email_is_still_a_422_when_the_org_is_full(api):
    c, a, _, _ = api
    _invite(c, a, "two@a.example")
    _invite(c, a, "three@a.example")
    assert _invite(c, a, "not an email").status_code == 422


def test_usage_shows_every_metric_with_its_limit(api):
    c, a, _, ws = api
    c.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": ws})
    _stored(a["org_id"], 10)
    r = c.get(f"/orgs/{a['org_slug']}/usage", headers=bearer(a))
    assert r.status_code == 200
    body = r.json()
    assert body["plan"] == "free" and body["day"] == usage.day_of(NOON)
    assert body["metrics"]["runs"] == {"used": 1, "limit": 2}
    assert body["metrics"]["storage_bytes"] == {"used": 10, "limit": 1000}
    assert body["metrics"]["members"] == {"used": 1, "limit": 3}
    assert set(body["metrics"]) == set(TIGHT)


def test_a_viewer_can_read_usage(api):
    c, a, _, _ = api
    v = join(c, a, "viewer")
    assert c.get(f"/orgs/{a['org_slug']}/usage", headers=bearer(v)).status_code == 200


def test_org_b_cannot_read_org_a_usage(api):
    c, a, b, _ = api
    r = c.get(f"/orgs/{a['org_slug']}/usage", headers=bearer(b))
    assert r.status_code == 404 and r.json() == {"detail": NOT_FOUND}


def test_usage_answers_for_the_org_in_the_path_not_the_session(api):
    """Someone in two orgs reads each by its slug."""
    c, a, b, ws = api
    c.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": ws})
    both = join(c, b, "member", "a@a.example")  # a@a.example joins B, session now on B
    r = c.get(f"/orgs/{a['org_slug']}/usage", headers=bearer(both))
    assert r.status_code == 200 and r.json()["metrics"]["runs"]["used"] == 1


def test_no_tenant_route_can_change_a_plan(api):
    c, a, _, _ = api
    for method in ("PUT", "POST", "PATCH"):
        assert c.request(method, f"/orgs/{a['org_slug']}/plan", headers=bearer(a),
                         json={"plan": "team"}).status_code in (404, 405)
    assert quotas.plan_of(a["org_id"]) == "free"


def test_a_plan_change_moves_the_boundary(api):
    c, a, _, ws = api
    body = {"kind": "run", "workspace": ws}
    for _ in range(2):
        c.post("/jobs", headers=bearer(a), json=body)
    assert c.post("/jobs", headers=bearer(a), json=body).status_code == 429
    quotas.set_plan(a["org_id"], "team", "operator:test", NOON)
    assert c.post("/jobs", headers=bearer(a), json=body).status_code == 200


# ----- an org's own model key

CANARY = "sk-canary-" + "7f3a9c" * 5 + "Zq42"
PLATFORM_KEY = "platform-key-should-not-travel"
QUESTION = "invoice detail amount for invoice INV-1"
ONTOLOGY = {"object_types": {"Invoice": {"description": "An invoice", "sources": []}},
            "functions": {"invoice_detail": {
                "description": "Invoice detail and amount.", "params": {"invoice_id": "Invoice"}, "returns": "one",
                "sql": "select id, title from objects where type = 'Invoice' and id = $invoice_id"}}}
POLICIES = {"default": "deny", "rules": [{"id": "read", "effect": "permit", "principal": "user:*",
                                          "action": "function:*"}]}


class Wire:
    """Stands where the network is. Records what would have left the machine."""

    def __init__(self, tokens=(30, 10)):
        self.calls, self.tokens, self.error = [], tokens, None

    def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if self.error:
            raise self.error
        prompt = kw["json"]["messages"][-1]["content"]
        ev = re.search(r"cite as \[ev:([^\]]+)\]", prompt).group(1) if "cite as" in prompt else "x"
        text = f"INV-1 is on file. [ev:{ev}]"
        usage_in, usage_out = self.tokens
        body = ({"content": [{"text": text}], "usage": {"input_tokens": usage_in, "output_tokens": usage_out}}
                if "anthropic" in url else
                {"choices": [{"message": {"content": text}}],
                 "usage": {"prompt_tokens": usage_in, "completion_tokens": usage_out}})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


@pytest.fixture()
def wire(monkeypatch):
    w = Wire()
    monkeypatch.setattr("ironwright.llm.httpx.post", w.post)
    monkeypatch.delenv("IRONWRIGHT_LLM", raising=False)
    monkeypatch.delenv("IRONWRIGHT_LLM_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLATFORM_KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return w


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())


def test_the_org_key_is_first_and_it_is_the_one_sent(wire, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    res = resolve_llm(("anthropic", CANARY)).complete("s", "p")
    assert res.text and wire.calls[0]["headers"]["x-api-key"] == CANARY


def test_the_platform_key_is_second(wire, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    resolve_llm(None).complete("s", "p")
    assert wire.calls[0]["headers"]["x-api-key"] == PLATFORM_KEY


@pytest.mark.parametrize("configured", [None, "anthropic-with-no-key"])
def test_the_deterministic_template_is_third(wire, monkeypatch, configured):
    if configured:
        monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY")
    llm = resolve_llm(None)
    assert llm.offline and llm.complete("s", "p").text is None and not wire.calls


def test_an_org_key_for_openai_goes_to_openai_with_openai_s_model(wire, monkeypatch):
    """The platform's model name means nothing to the org's provider."""
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    monkeypatch.setenv("IRONWRIGHT_LLM_MODEL", "claude-platform-choice")
    resolve_llm(("openai", CANARY)).complete("s", "p")
    call = wire.calls[0]
    assert "openai.com" in call["url"] and call["headers"]["Authorization"] == f"Bearer {CANARY}"
    assert call["json"]["model"] == DEFAULT_MODELS["openai"]


def test_a_key_is_stored_encrypted_and_only_last_4_are_readable(org, secret):
    byokey.store(org, "anthropic", CANARY, "user:x", NOON)
    assert byokey.status(org) == {"provider": "anthropic", "last4": "Zq42", "updated_at": NOON, "updated_by": "user:x"}
    assert byokey.load(org) == ("anthropic", CANARY)
    with usage.engine().connect() as con:
        row = con.execute(sa.select(usage.org_llm_keys)).mappings().one()
    assert CANARY not in str(dict(row))


def test_a_ciphertext_moved_to_another_org_does_not_open(org, secret):
    other = service.signup("b@b.example", PASSWORD, "Birch", NOON)["org_id"]
    byokey.store(org, "anthropic", CANARY, "user:x", NOON)
    with usage.engine().begin() as con:
        con.execute(usage.org_llm_keys.update().where(usage.org_llm_keys.c.org_id == org).values(org_id=other))
    with pytest.raises(byokey.Unreadable):
        byokey.load(other)


def test_a_key_under_an_old_secret_cannot_be_read_and_says_nothing(org, secret, monkeypatch, caplog):
    byokey.store(org, "anthropic", CANARY, "user:x", NOON)
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    with caplog.at_level(logging.DEBUG), pytest.raises(byokey.Unreadable) as e:
        byokey.load(org)
    assert CANARY not in caplog.text + str(e.value)


def test_no_secret_means_no_key_can_be_stored(org, monkeypatch):
    monkeypatch.delenv("IRONWRIGHT_SECRET_KEY", raising=False)
    with pytest.raises(byokey.NoSecret):
        byokey.store(org, "anthropic", CANARY, "user:x", NOON)
    assert byokey.status(org) is None


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_a_hosted_server_with_no_secret_will_not_start(meta_db, monkeypatch, env):
    monkeypatch.setenv("IRONWRIGHT_ENV", env)
    monkeypatch.delenv("IRONWRIGHT_SECRET_KEY", raising=False)
    with pytest.raises(SystemExit, match="IRONWRIGHT_SECRET_KEY"):
        create_platform_app()


def test_a_malformed_secret_will_not_start_anywhere(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", "hunter2")
    with pytest.raises(SystemExit, match="IRONWRIGHT_SECRET_KEY") as e:
        create_platform_app()
    assert "hunter2" not in str(e.value)


def test_a_local_server_with_no_secret_starts(meta_db, monkeypatch):
    monkeypatch.delenv("IRONWRIGHT_ENV", raising=False)
    monkeypatch.delenv("IRONWRIGHT_SECRET_KEY", raising=False)
    assert create_platform_app()


def _key_path(session):
    return f"/orgs/{session['org_slug']}/llm-key"


def _put(c, session, provider="anthropic", key=CANARY):
    return c.put(_key_path(session), headers=bearer(session), json={"provider": provider, "key": key})


def test_an_owner_stores_a_key_and_only_ever_sees_last_4(api, secret):
    c, a, _, _ = api
    r = _put(c, a)
    assert r.status_code == 200 and r.json()["last4"] == "Zq42" and r.json()["provider"] == "anthropic"
    assert CANARY not in r.text
    got = c.get(_key_path(a), headers=bearer(a))
    assert got.json()["last4"] == "Zq42" and CANARY not in got.text


def test_with_no_key_stored_the_routes_say_so_plainly(api, secret):
    c, a, _, _ = api
    assert c.get(_key_path(a), headers=bearer(a)).json() == {"provider": None}
    gone = c.delete(_key_path(a), headers=bearer(a))
    assert gone.status_code == 200 and gone.json() == {"removed": False}
    assert usage.audit_log(a["org_id"]) == []


def test_a_server_with_no_secret_refuses_the_key_in_plain_words(api, monkeypatch):
    c, a, _, _ = api
    monkeypatch.delenv("IRONWRIGHT_SECRET_KEY", raising=False)
    r = _put(c, a)
    assert r.status_code == 503 and "IRONWRIGHT_SECRET_KEY" in r.json()["detail"]
    assert CANARY not in r.text


@pytest.mark.parametrize("body", [
    json.dumps(CANARY),
    json.dumps({"key": CANARY}),
    json.dumps({"provider": "anthropic", "key": [CANARY]}),
    json.dumps({"provider": "not-a-provider", "key": CANARY}),
    json.dumps({"provider": "ollama", "key": CANARY}),
    json.dumps({"provider": "anthropic", "key": CANARY + " " + CANARY}),
    json.dumps([CANARY]),
    '{"provider": "anthropic", "key": "' + CANARY,
], ids=["bare-string", "no-provider", "key-is-a-list", "unknown-provider", "keyless-provider",
        "space-in-key", "list-body", "not-json"])
def test_a_refused_body_never_comes_back_in_the_answer(api, secret, body):
    c, a, _, _ = api
    r = c.put(_key_path(a), headers={**bearer(a), "content-type": "application/json"}, content=body)
    assert r.status_code == 422
    assert CANARY not in r.text and "Zq42" not in r.text
    assert byokey.status(a["org_id"]) is None


def test_a_key_too_short_to_hide_behind_its_last_4_is_refused(api, secret):
    c, a, _, _ = api
    r = _put(c, a, key="short-key")
    assert r.status_code == 422 and "short-key" not in r.text


@pytest.mark.parametrize("role", ["viewer", "member"])
def test_below_admin_cannot_touch_the_key_or_the_audit(api, secret, role):
    c, a, _, _ = api
    _put(c, a)
    who = join(c, a, role)
    path, h = _key_path(a), bearer(who)
    assert c.put(path, headers=h, json={"provider": "anthropic", "key": CANARY}).status_code == 403
    assert c.delete(path, headers=h).status_code == 403
    assert c.get(path, headers=h).status_code == 403
    assert c.get(f"/orgs/{a['org_slug']}/audit", headers=h).status_code == 403


def test_an_admin_can_store_a_key(api, secret):
    c, a, _, _ = api
    adm = join(c, a, "admin")
    assert _put(c, adm).status_code == 200


def test_org_b_cannot_read_or_change_org_a_key(api, secret):
    c, a, b, _ = api
    _put(c, a)
    path = _key_path(a)
    for r in (c.get(path, headers=bearer(b)), c.delete(path, headers=bearer(b)),
              c.put(path, headers=bearer(b), json={"provider": "openai", "key": "x" * 30}),
              c.get(f"/orgs/{a['org_slug']}/audit", headers=bearer(b))):
        assert r.status_code == 404 and r.json() == {"detail": NOT_FOUND}
    assert byokey.load(a["org_id"]) == ("anthropic", CANARY)
    assert byokey.status(b["org_id"]) is None
    assert c.get(_key_path(b), headers=bearer(b)).json() == {"provider": None}


def test_every_key_and_plan_change_is_audited_without_the_key(api, secret):
    c, a, _, _ = api
    _put(c, a)
    _put(c, a, provider="openai", key="sk-second-" + "b" * 20 + "Yy11")
    c.delete(_key_path(a), headers=bearer(a))
    quotas.set_plan(a["org_id"], "team", "operator:test", NOON)
    r = c.get(f"/orgs/{a['org_slug']}/audit", headers=bearer(a))
    assert r.status_code == 200 and CANARY not in r.text
    rows = r.json()
    assert [x["action"] for x in rows] == ["llm_key.set", "llm_key.set", "llm_key.remove", "plan.change"]
    assert rows[0]["actor"] == a["principal"]
    assert rows[0]["detail"] == {"provider": "anthropic", "last4": "Zq42"}
    assert rows[2]["detail"] == {"provider": "openai", "last4": "Yy11"}


@pytest.fixture()
def asking(api, monkeypatch, tmp_path):
    """The real Ask route and the real ask(), over a small pack with one object.
    A tenant workspace needs an accepted ontology and a run to answer, and these
    tests are about the meter, so only finding the pack is stood in for."""
    pack = Pack(name="quota_ws", dir=tmp_path, spec={"title": "Quota fixture"}, ontology=ONTOLOGY, policies=POLICIES,
                usecases={})
    pack.paths.ensure()
    ont = Ontology(pack, attach_lake=False)
    ont.store.upsert("Invoice", "INV-1", {"id": "INV-1", "title": "One"}, "One", changed_by="t", action_run="t")
    ont.store.con.close()

    @contextlib.contextmanager
    def opened(_pack):
        o = Ontology(pack, attach_lake=False)
        try:
            yield o
        finally:
            o.store.con.close()

    monkeypatch.setattr(platform_api, "_ask_pack", lambda name, c: pack)
    monkeypatch.setattr(platform_api, "_ontology", opened)
    c, a, b, ws = api
    yield c, a, b, ws, pack


def _ask(c, session, ws):
    return c.post(f"/workspaces/{ws}/ask", headers=bearer(session), json={"question": QUESTION})


def test_ask_questions_trip_at_the_boundary(asking, wire):
    c, a, _, ws, _ = asking
    assert [_ask(c, a, ws).status_code for _ in range(2)] == [200, 200]
    over = _ask(c, a, ws)
    assert over.status_code == 429 and "2 Ask questions" in over.json()["detail"]
    assert usage.used(a["org_id"], "ask", NOON) == 2


def test_with_no_key_anywhere_ask_answers_from_the_template(asking, wire):
    c, a, _, ws, _ = asking
    r = _ask(c, a, ws)
    assert r.status_code == 200 and r.json()["narrator"] == "offline-template"
    assert not wire.calls


def test_ask_uses_the_org_key_and_its_tokens_do_not_count_against_the_platform(asking, wire, secret, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    c, a, _, ws, _ = asking
    _put(c, a)
    r = _ask(c, a, ws)
    assert r.status_code == 200 and r.json()["narrator"] == DEFAULT_MODELS["anthropic"]
    assert wire.calls[0]["headers"]["x-api-key"] == CANARY
    assert usage.used(a["org_id"], "llm_tokens", NOON) == 0
    assert usage.used(a["org_id"], "llm_tokens_byo", NOON) == 40
    assert c.get(f"/orgs/{a['org_slug']}/usage", headers=bearer(a)).json()["byo_llm_tokens"] == 40


def test_platform_tokens_trip_at_the_boundary_and_the_org_is_told_why(asking, wire, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    monkeypatch.setitem(quotas.PLANS, "free", {**TIGHT, "ask": 50})
    c, a, _, ws, _ = asking
    org = a["org_id"]
    usage.add(org, "llm_tokens", NOON, 99)  # one under
    r = _ask(c, a, ws)
    assert r.status_code == 200 and wire.calls[0]["headers"]["x-api-key"] == PLATFORM_KEY
    assert usage.used(org, "llm_tokens", NOON) == 139, "what was really spent, past the limit"
    asked = usage.used(org, "ask", NOON)
    over = _ask(c, a, ws)
    assert over.status_code == 429 and "model tokens" in over.json()["detail"]
    assert len(wire.calls) == 1
    assert usage.used(org, "ask", NOON) == asked, "a refusal must not spend a question"


def test_platform_tokens_exactly_at_the_limit_are_over(asking, wire, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    c, a, _, ws, _ = asking
    usage.add(a["org_id"], "llm_tokens", NOON, 100)
    assert _ask(c, a, ws).status_code == 429


def test_an_org_out_of_platform_tokens_can_still_use_its_own_key(asking, wire, secret, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    c, a, _, ws, _ = asking
    usage.add(a["org_id"], "llm_tokens", NOON, 100)
    assert _ask(c, a, ws).status_code == 429
    _put(c, a)
    assert _ask(c, a, ws).status_code == 200
    assert wire.calls[-1]["headers"]["x-api-key"] == CANARY


def test_org_b_ask_never_travels_on_org_a_key(asking, wire, secret):
    c, a, b, _, _ = asking
    _put(c, a)
    bws = c.post("/workspaces", headers=bearer(b), json={"name": "ws-b", "size": 4, "seed": 3}).json()["name"]
    r = _ask(c, b, bws)
    assert r.status_code == 200 and r.json()["narrator"] == "offline-template"
    assert not wire.calls


def test_a_key_that_cannot_be_read_never_falls_through_to_the_platform(asking, wire, secret, monkeypatch):
    """The org chose its own provider. Its cells do not move to ours because a secret rotated."""
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    c, a, _, ws, _ = asking
    _put(c, a)
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    r = _ask(c, a, ws)
    assert r.status_code == 200 and r.json()["narrator"] == "offline-template"
    assert not wire.calls


def test_the_dev_org_ask_stays_offline_with_a_provider_configured(meta_db, wire, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    assert quotas.start_ask(DEV_ORG_ID, NOON) is None


def _everything_stored(pack) -> str:
    """Every table in the store, and every file the pack wrote."""
    out = []
    eng = usage.engine()
    with eng.connect() as con:
        for name in sa.inspect(eng).get_table_names():
            out += [str(tuple(r)) for r in con.execute(sa.text(f'select * from "{name}"'))]
    for path in pack.dir.rglob("*"):
        if path.is_file() and path.suffix in (".jsonl", ".json", ".log"):
            out.append(path.read_text(errors="replace"))
    return "\n".join(out)


def test_the_key_is_in_no_answer_log_or_row_even_when_the_provider_fails(asking, wire, secret, caplog, capfd):
    c, a, _, ws, pack = asking
    answers = []
    with caplog.at_level(logging.DEBUG):
        answers.append(_put(c, a))
        answers.append(_ask(c, a, ws))
        wire.error = RuntimeError(f"401 for key {CANARY}")
        answers.append(_ask(c, a, ws))
        for path in (_key_path(a), f"/orgs/{a['org_slug']}/usage", f"/orgs/{a['org_slug']}/audit",
                     f"/workspaces/{ws}/ask/log"):
            answers.append(c.get(path, headers=bearer(a)))
    assert wire.calls, "the key must really have been used, or this proves nothing"
    assert answers[2].status_code == 200 and answers[2].json()["narrator"] == "offline-template"
    assert all(CANARY not in r.text for r in answers)
    assert CANARY not in caplog.text
    captured = capfd.readouterr()
    assert CANARY not in captured.out + captured.err
    assert CANARY not in _everything_stored(pack)
    assert CANARY not in os.environ.values(), "an org's key in the environment would reach every org"


def test_usage_counts_seats_the_way_the_invite_door_does(api):
    """A console that said 1 of 3 here would be followed by a 429 on the next invite."""
    c, a, _, _ = api
    _invite(c, a, "two@a.example")
    _invite(c, a, "three@a.example")
    seats = c.get(f"/orgs/{a['org_slug']}/usage", headers=bearer(a)).json()["metrics"]["members"]
    assert seats == {"used": 3, "limit": 3}
    assert _invite(c, a, "four@a.example").status_code == 429


# ----- W2's doors: accepting a proposal queues a run, and a proposal can be narrated

def _accept(c, session, ws):
    return c.post(f"/workspaces/{ws}/ontology/accept", headers=bearer(session), json={"proposal_id": "p1"})


@pytest.fixture()
def accepted(monkeypatch):
    """Only the proposer's own work is stood in for. The route and the queue are real."""
    calls = []
    monkeypatch.setattr("ironwright.ontology.routes.proposer.accept",
                        lambda *a: calls.append(a) or {"accepted": True})
    return calls


def test_accepting_a_proposal_spends_a_run(api, accepted):
    c, a, _, ws = api
    r = _accept(c, a, ws)
    assert r.status_code == 200 and r.json()["job_id"]
    assert usage.used(a["org_id"], "runs", NOON) == 1


def test_an_org_out_of_runs_cannot_accept_and_nothing_changes(api, accepted):
    """Accept writes the ontology, then queues. A refusal has to come before the write,
    or the org is left with an accepted model and no run behind it."""
    c, a, _, ws = api
    for _ in range(2):
        assert _accept(c, a, ws).status_code == 200
    over = _accept(c, a, ws)
    assert over.status_code == 429 and "2 runs" in over.json()["detail"]
    assert len(accepted) == 2
    assert usage.used(a["org_id"], "runs", NOON) == 2


def test_an_accept_that_fails_costs_no_run(api, monkeypatch):
    from ironwright.ontology import propose as proposer

    def stale(*_a):
        raise proposer.StaleProposal("old")

    monkeypatch.setattr("ironwright.ontology.routes.proposer.accept", stale)
    c, a, _, ws = api
    assert _accept(c, a, ws).status_code == 409
    assert usage.used(a["org_id"], "runs", NOON) == 0


def _narrate_job(org_id, monkeypatch, params=None) -> list:
    """The real handler. The pack and the proposer's work are stood in for, and the
    model it was handed is then called, so the wire shows which key travelled."""
    seen = []

    def store(_pack, llm=None):
        seen.append(llm)
        if llm is not None:
            llm.complete("s", "p")
        return {"objects": [], "links": [], "functions": []}

    monkeypatch.setattr(handlers, "_load", lambda org, ws: SimpleNamespace(is_tenant=True))
    monkeypatch.setattr("ironwright.ontology.propose.store_proposal", store)
    job = handlers.JobCtx(org_id=org_id, principal="user:x", job_id="j1", cancelled=lambda: False)
    handlers.HANDLERS["ontology_propose"](job, "ws", {"narrate": True} if params is None else params, lambda _: None)
    return seen


def test_narrating_a_proposal_counts_platform_tokens(api, wire, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    _, a, _, _ = api
    _narrate_job(a["org_id"], monkeypatch)
    assert wire.calls[0]["headers"]["x-api-key"] == PLATFORM_KEY
    assert usage.used(a["org_id"], "llm_tokens", auth._now()) == 40


def test_narrating_a_proposal_uses_the_org_key_first(api, wire, secret, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    c, a, _, _ = api
    _put(c, a)
    _narrate_job(a["org_id"], monkeypatch)
    assert wire.calls[0]["headers"]["x-api-key"] == CANARY
    assert usage.used(a["org_id"], "llm_tokens", auth._now()) == 0
    assert usage.used(a["org_id"], "llm_tokens_byo", auth._now()) == 40


def test_a_proposal_out_of_platform_tokens_is_still_made_without_the_paragraph(api, wire, monkeypatch):
    """Rules make the proposal. Narration is the only part that costs tokens, and a
    worker has no request to answer 429 to."""
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    _, a, _, _ = api
    usage.add(a["org_id"], "llm_tokens", auth._now(), 100)
    seen = _narrate_job(a["org_id"], monkeypatch)
    assert seen[0].offline and not wire.calls


def test_a_key_that_cannot_be_read_never_narrates_on_the_platform(api, wire, secret, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    c, a, _, _ = api
    _put(c, a)
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    assert _narrate_job(a["org_id"], monkeypatch)[0].offline
    assert not wire.calls


def test_a_proposal_nobody_asked_to_narrate_gets_no_model(api, wire, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LLM", "anthropic")
    _, a, _, _ = api
    assert _narrate_job(a["org_id"], monkeypatch, params={}) == [None]


def test_a_refused_upload_gives_its_upload_back(api):
    """Only a stored upload counts. A wrong extension or an empty file stored nothing."""
    c, a, _, _ = api
    for name, raw in (("notes.pdf", b"x"), ("empty.csv", b"")):
        assert c.post("/uploads", headers=bearer(a), files=[("files", (name, raw))]).status_code in (400, 415)
    assert usage.used(a["org_id"], "uploads", NOON) == 0
    assert c.post("/uploads", headers=bearer(a), files=[("files", ("one.csv", b"id\n1\n"))]).status_code == 201
    assert usage.used(a["org_id"], "uploads", NOON) == 1


def test_the_storage_refusal_reads_in_units_a_person_uses(meta_db):
    org = "a" * 32
    with pytest.raises(quotas.QuotaExceeded) as refused:
        quotas.start_upload(org, str(2 * quotas.GIB), NOON)
    assert "1.0 GB" in refused.value.detail and "1073741824" not in refused.value.detail
