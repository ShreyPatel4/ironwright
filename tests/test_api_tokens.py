"""Tier 1 item 7 of docs/enterprise-security.md: org scoped API tokens with scopes read,
ask, run and act:request. Shown once, stored as a hash, and never able to approve.
Real routes, both backends."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ironwright import auth
from ironwright import workspaces_tenant as wt
from ironwright.config import ensure_tenant
from ironwright.platform_api import NOT_FOUND
from ironwright.security import store, tokens
from ironwright.tenancy import usage

PASSWORD = "correct-horse-9"
ALL = ["read", "ask", "run", "act:request"]
SCOPE_REFUSAL = "this API token's scopes do not allow this"
FIXTURE = Path(__file__).parent / "fixtures" / "tenant_ws"


@pytest.fixture()
def app(meta_db):
    from ironwright.platform_api import create_platform_app
    return create_platform_app()


@pytest.fixture()
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def bearer(t):
    return {"Authorization": f"Bearer {t['token'] if isinstance(t, dict) else t}"}


def signup(c, email="owner@a.example", org="Acme"):
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    assert r.status_code == 201, r.text
    return r.json()


def make(c, owner, scopes, name="ci"):
    r = c.post(f"/orgs/{owner['org_slug']}/tokens", headers=bearer(owner), json={"name": name, "scopes": scopes})
    assert r.status_code == 201, r.text
    return r.json()


def test_a_token_is_shown_once_and_stored_as_a_hash(client):
    owner = signup(client)
    made = make(client, owner, ["read"])
    assert made["token"].startswith("iwt_")
    listed = client.get(f"/orgs/{owner['org_slug']}/tokens", headers=bearer(owner)).json()
    assert [t["name"] for t in listed] == ["ci"] and all("token" not in t for t in listed)
    with store.engine().connect() as con:
        assert made["token"] not in str(con.execute(sa.select(store.api_tokens)).all())


def test_an_unknown_scope_is_refused(client):
    owner = signup(client)
    for scopes in (["approve"], ["admin"], [], ["read", "act:approve"]):
        r = client.post(f"/orgs/{owner['org_slug']}/tokens", headers=bearer(owner), json={"name": "x", "scopes": scopes})
        assert r.status_code == 422, scopes


def test_a_member_cannot_make_one(client):
    owner = signup(client)
    r = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner), json={"email": "m@a.example", "role": "member"})
    m = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    r = client.post(f"/orgs/{owner['org_slug']}/tokens", headers=bearer(m), json={"name": "x", "scopes": ["read"]})
    assert r.status_code == 403


def test_read_reads_and_writes_nothing(client):
    owner = signup(client)
    t = make(client, owner, ["read"])
    assert client.get("/workspaces", headers=bearer(t)).status_code == 200
    r = client.post("/workspaces/retail/ask", headers=bearer(t), json={"question": "how many stores"})
    assert r.status_code == 403 and r.json()["detail"] == SCOPE_REFUSAL
    r = client.post("/jobs", headers=bearer(t), json={"kind": "profile", "workspace": "retail"})
    assert r.status_code == 403 and r.json()["detail"] == SCOPE_REFUSAL


def test_ask_can_ask_and_not_read(client):
    owner = signup(client)
    t = make(client, owner, ["ask"])
    r = client.post("/workspaces/retail/ask", headers=bearer(t), json={"question": "how many stores"})
    assert SCOPE_REFUSAL not in r.text and r.status_code != 401
    assert client.get("/workspaces", headers=bearer(t)).json()["detail"] == SCOPE_REFUSAL


def test_run_can_queue_a_job(client):
    owner = signup(client)
    t = make(client, owner, ["run"])
    r = client.post("/jobs", headers=bearer(t), json={"kind": "profile", "workspace": "nope"})
    assert SCOPE_REFUSAL not in r.text and r.status_code != 401


def test_act_request_can_ask_for_an_action(client):
    owner = signup(client)
    t = make(client, owner, ["act:request"])
    r = client.post("/workspaces/retail/actions/delist_item", headers=bearer(t), json={"params": {}})
    assert SCOPE_REFUSAL not in r.text and r.status_code != 401


def test_a_token_never_reaches_an_org_route(client, app):
    """Not its own org's settings, members, audit, tokens or security either."""
    owner = signup(client)
    t = make(client, owner, ALL)
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/orgs/{slug}"):
            path = re.sub(r"\{[^}]+\}", "x", route.path.replace("{slug}", owner["org_slug"]))
            for method in route.methods - {"HEAD"}:
                r = client.request(method, path, headers=bearer(t), json={})
                assert r.status_code in (403, 404), f"{method} {path} gave a token {r.status_code}"


@pytest.mark.parametrize("path", ["/workspaces/retail/approvals/1/decide", "/workspaces/retail/escalations/x/decide",
                                  "/workspaces/retail/orgpolicy/apply", "/workspaces", "/workspaces/retail/bi/mirror",
                                  "/workspaces/retail/otel/export", "/auth/mfa/enrol", "/auth/sessions/revoke-all",
                                  "/uploads", "/workspaces/retail/sources"])
def test_an_all_scopes_token_can_never_approve_or_administer(client, path):
    owner = signup(client)
    t = make(client, owner, ALL)
    r = client.post(path, headers=bearer(t), json={"approved_by": "user:x", "approve": True})
    assert r.status_code == 403, f"{path} gave {r.status_code}"


def test_every_unsafe_route_outside_the_scopes_is_refused(client, app):
    """Default deny: a route added later is closed to tokens until a scope names it."""
    owner = signup(client)
    t = make(client, owner, ALL)
    allowed = {(m, p) for scope in ALL for m, p in tokens.SCOPES[scope]}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"GET", "HEAD"}:
            if (method, route.path) in allowed or route.path in auth.PUBLIC_ROUTES:
                continue
            path = re.sub(r"\{[^}]+\}", "x", route.path.replace("{name}", "retail"))
            r = client.request(method, path, headers=bearer(t), json={})
            assert r.status_code in (403, 404), f"{method} {route.path} gave a token {r.status_code}"


def test_another_orgs_workspace_is_404_to_a_token(client):
    a = signup(client)
    b = signup(client, "b@b.example", "Beta")
    made = client.post("/workspaces", headers=bearer(a), json={"name": "a-ws", "size": 4, "seed": 3})
    assert made.status_code == 201
    t = make(client, b, ALL)
    assert client.get(f"/workspaces/{made.json()['name']}/organization", headers=bearer(t)).status_code == 404


def test_a_revoked_token_is_dead_at_once(client):
    owner = signup(client)
    t = make(client, owner, ["read"])
    assert client.delete(f"/orgs/{owner['org_slug']}/tokens/{t['id']}", headers=bearer(owner)).status_code == 200
    assert client.get("/workspaces", headers=bearer(t)).status_code == 401


def test_another_orgs_token_id_reads_as_not_found(client):
    a = signup(client)
    b = signup(client, "b@b.example", "Beta")
    t = make(client, a, ["read"])
    assert client.delete(f"/orgs/{b['org_slug']}/tokens/{t['id']}", headers=bearer(b)).status_code == 404
    assert client.get("/workspaces", headers=bearer(t)).status_code == 200


def test_a_token_principal_is_not_a_user_and_not_an_agent(client):
    owner = signup(client)
    t = make(client, owner, ["read"])
    me = client.get("/auth/me", headers=bearer(t))
    assert me.status_code == 403  # no account behind it
    found = auth.session_ctx(t["token"])
    assert found.principal == f"service:{t['id']}" and not auth.is_agent(found.principal)
    assert found.user_id != owner["user_id"] and found.org_id == owner["org_id"]


def test_create_and_revoke_are_audited(client):
    owner = signup(client)
    t = make(client, owner, ["read"])
    client.delete(f"/orgs/{owner['org_slug']}/tokens/{t['id']}", headers=bearer(owner))
    got = [x["action"] for x in usage.audit_log(owner["org_id"])]
    assert "token.create" in got and "token.revoke" in got


def test_a_token_dies_with_its_org(client):
    owner = signup(client)
    t = make(client, owner, ["read"])
    assert client.post(f"/orgs/{owner['org_slug']}/delete", headers=bearer(owner),
                       json={"confirm": owner["org_slug"]}).status_code in (200, 202)
    assert client.get("/workspaces", headers=bearer(t)).status_code == 401


# ----- each write scope doing its job on a workspace the org owns

@pytest.fixture()
def claims(client):
    """A real tenant workspace, run green, so the scopes meet real handlers."""
    owner = signup(client)
    root = ensure_tenant(owner["org_id"])
    shutil.copytree(FIXTURE / "uploads" / "fixture", root / "uploads" / "fixture")
    models = {layer: {f.name: f.read_text() for f in (FIXTURE / "models" / layer).glob("*.sql")}
              for layer in ("silver", "gold")}
    wt.create_workspace(owner["org_id"], "claims", yaml.safe_load((FIXTURE / "workspace.yaml").read_text()),
                        models=models, ontology=yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
                        policies=yaml.safe_load((FIXTURE / "policies.yaml").read_text()))
    assert wt.run_workspace(owner["org_id"], "claims")["passed"]
    return owner


def test_run_runs_the_workspace(client, claims):
    t = make(client, claims, ["run"])
    r = client.post("/workspaces/claims/runs", headers=bearer(t), json={})
    assert r.status_code == 200, r.text
    assert r.json()["workspace"] == "claims" and r.json()["evals"]["passed"] is True


def test_run_queues_a_job_under_the_token(client, claims):
    t = make(client, claims, ["run"])
    r = client.post("/jobs", headers=bearer(t), json={"kind": "run", "workspace": "claims"})
    assert r.status_code == 200, r.text
    owner_view = client.get(f"/jobs/{r.json()['id']}", headers=bearer(claims)).json()
    assert owner_view["created_by"] == f"service:{t['id']}"


def test_ask_gets_a_real_answer_spoken_as_the_token(client, claims):
    t = make(client, claims, ["ask"])
    r = client.post("/workspaces/claims/ask", headers=bearer(t), json={"question": "how many claims are there"})
    assert r.status_code == 200, r.text
    assert r.json()["principal"] == f"service:{t['id']}" and r.json()["status"] in ("answered", "refused")


def test_act_request_passes_the_gate_and_reaches_the_handler(client, claims):
    """Tenant workspaces have no governed action route yet, so the handler answers that
    in its own words. What this proves is that the scope and the gate let it through."""
    t = make(client, claims, ["act:request"])
    r = client.post("/workspaces/claims/actions/close_claim", headers=bearer(t), json={"params": {}})
    assert r.json()["detail"] not in (SCOPE_REFUSAL, NOT_FOUND)
    without = make(client, claims, ["read"], name="reader")
    r = client.post("/workspaces/claims/actions/close_claim", headers=bearer(without), json={"params": {}})
    assert r.status_code == 403 and r.json()["detail"] == SCOPE_REFUSAL
