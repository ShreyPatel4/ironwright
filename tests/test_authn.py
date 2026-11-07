"""Real identity on the platform API.

The sweep enumerates app.routes instead of a hand-kept list, so a route added
next month is closed by default or this file goes red. Everything here runs
with the dev flag off; the flag has its own tests at the bottom.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from ironwright import auth, platform_api
from ironwright.ontology.runtime import Ontology
from ironwright.ontology.store import OntologyStore
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import db
from ironwright.tracing import Tracer

EMAIL = "tester@example.com"
PASSWORD = "correct-horse-9"
# "/" only redirects to the console and carries no data.
PUBLIC = {"/auth/status", "/auth/login", "/auth/signup", "/auth/accept-invite", "/",
          "/auth/verify", "/auth/reset-request", "/auth/reset"}
EIGHT_HOURS = 8 * 3600


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def closed(monkeypatch, tmp_path):
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))


@pytest.fixture()
def console_dist(monkeypatch, tmp_path):
    """A stand-in build, because dist/ is ignored and the mount only exists with it."""
    dist = tmp_path / "console-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>console</title>")
    monkeypatch.setattr(platform_api, "CONSOLE_DIST", dist)
    return dist


@pytest.fixture()
def client():
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def token(client):
    auth.set_account(EMAIL, PASSWORD)
    return client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()["token"]


@pytest.fixture(scope="module")
def retail(tmp_path_factory):
    """One real pipeline run, so spoof tests read real objects and real audit rows."""
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("authn-retail")
    mp.setenv("IRONWRIGHT_DATA", str(root))
    mp.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    auth.set_account(EMAIL, PASSWORD)
    with TestClient(create_platform_app()) as c:
        tok = c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()["token"]
        ran = c.post("/workspaces/retail/runs", json={"seed": 7}, headers=bearer(tok))
        assert ran.status_code == 200, ran.text
    mp.undo()
    return root


@pytest.fixture()
def real(retail, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(retail))
    with TestClient(create_platform_app()) as c:
        tok = c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()["token"]
        c.headers.update(bearer(tok))
        yield c


def _routes(routes, prefix=""):
    """Every route under app.routes, however this FastAPI stores included routers.

    Newer FastAPI keeps an include as one lazy entry instead of flattening it,
    and a flat loop would quietly miss every router. Anything this cannot
    classify is a failure, never a skip.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _routes(included.routes, prefix + route.include_context.prefix)
        elif isinstance(route, Mount):
            continue
        else:
            assert hasattr(route, "path") and hasattr(route, "methods"), f"unswept route {route!r}"
            yield prefix, route


def _sweep(app):
    for prefix, route in _routes(app.routes):
        template = prefix + route.path
        path = re.sub(r"\{[^}]+\}", "1", template)
        for method in sorted(route.methods or ()):
            yield template, method, path


def _request(c, method, path, **kw):
    if method in ("POST", "PUT", "PATCH"):
        kw["json"] = {}
    return c.request(method, path, follow_redirects=False, **kw)


# ----- the door

def test_no_token_gets_401_on_every_non_public_route(console_dist):
    app = create_platform_app()
    answered = set()
    with TestClient(app, raise_server_exceptions=False) as c:
        swept = list(_sweep(app))
        assert len(swept) > 50, "the sweep must see the whole app"
        templates = {t for t, _, _ in swept}
        # One route from each included router, so a walker that stops seeing
        # includes fails here instead of passing on fewer routes.
        assert {"/workspaces/{name}/objects/{type_}/{id_}/asof",
                "/workspaces/{name}/orgpolicy/preview",
                "/workspaces/{name}/escalations",
                "/workspaces/{name}/otel/export",
                "/workspaces/{name}/training/labels",
                "/workspaces/{name}/bi",
                "/workspaces/{name}/engines/check"} <= templates
        for template, method, path in swept:
            r = _request(c, method, path)
            if r.status_code != 401:
                answered.add(template)
                assert template in PUBLIC, f"{method} {template} answered {r.status_code} with no token"
    assert answered == PUBLIC


def test_only_the_console_is_mounted_and_it_serves_without_a_token(console_dist):
    app = create_platform_app()
    assert {r.path for r in app.routes if isinstance(r, Mount)} == {"/console"}
    with TestClient(app) as c:
        assert c.get("/console/").status_code == 200
        home = c.get("/", follow_redirects=False)
        assert home.status_code in (302, 307) and home.headers["location"] == "/console/"


def test_generated_docs_are_not_a_public_side_door(client):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code in (401, 404), path


def test_a_401_says_how_to_authenticate(client):
    r = client.get("/workspaces")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("value", ["Bearer", "Bearer not-a-session", "Basic abc", "not-a-session"])
def test_a_bad_authorization_header_gets_401(client, token, value):
    assert client.get("/workspaces", headers={"Authorization": value}).status_code == 401


def test_a_real_token_in_the_old_header_is_not_a_credential(client, token):
    assert client.get("/workspaces", headers={"x-session": token}).status_code == 401
    assert client.get("/auth/me", headers={"x-session": token}).status_code == 401


def test_a_valid_token_opens_the_door(client, token):
    assert client.get("/workspaces", headers=bearer(token)).status_code == 200
    me = client.get("/auth/me", headers=bearer(token)).json()
    assert me["principal"] == f"user:{me['user_id']}" and me["email"] == EMAIL


def test_logout_kills_the_token(client, token):
    assert client.post("/auth/logout", headers=bearer(token)).status_code == 200
    assert client.get("/workspaces", headers=bearer(token)).status_code == 401


# ----- spoofing

def test_spoofed_header_without_a_token_is_still_401(client):
    r = client.get("/workspaces", headers={"X-Principal": "user:category.manager.1"})
    assert r.status_code == 401


def test_spoofed_header_is_ignored_with_a_token(real):
    item = real.get("/workspaces/retail/objects/Item", params={"limit": 1}).json()[0]["_id"]
    spoof = {"X-Principal": "agent:spoofed"}
    dash = real.get(f"/workspaces/retail/objects/Item/{item}/dashboard", headers=spoof).json()
    assert dash["_as"] == real.get("/auth/me").json()["principal"]


def test_spoofed_header_is_ignored_on_the_asof_router(real):
    item = real.get("/workspaces/retail/objects/Item", params={"limit": 1}).json()[0]["_id"]
    asof = real.get(f"/workspaces/retail/objects/Item/{item}/asof",
                    params={"ts": "2200-01-01T00:00:00Z"},
                    headers={"X-Principal": "agent:spoofed"}).json()
    assert asof["_as"] == real.get("/auth/me").json()["principal"]


def test_platform_routes_do_not_declare_the_header():
    # asof.py is the one known holdout, outside this ticket's scope. The
    # middleware binds its header to the session; see the PR's cross-ticket asks.
    known = {"/workspaces/{name}/objects/{type_}/{id_}/asof"}

    def headers(dep):
        yield from (p.alias.lower() for p in dep.header_params)
        for sub in dep.dependencies:
            yield from headers(sub)

    declaring = {prefix + r.path for prefix, r in _routes(create_platform_app().routes)
                 if isinstance(r, APIRoute) and "x-principal" in set(headers(r.dependant))}
    assert declaring == known


def test_action_body_cannot_name_an_agent_as_the_actor(real):
    r = real.post("/workspaces/retail/actions/delist_item",
                  json={"params": {}, "actor": "agent:spoofed"})
    assert r.status_code == 200, r.text
    actors = {row["actor"] for row in real.get("/workspaces/retail/audit",
                                               params={"limit": 500}).json()}
    assert "agent:spoofed" not in actors
    assert real.get("/auth/me").json()["principal"] in actors


def _real_item_and_open_store(offset):
    store = OntologyStore(load_pack("retail").paths.ontology_db)
    try:
        item = store.con.execute("select id from objects where type = 'Item' order by id limit 1").fetchone()[0]
        rows = store.con.execute("select id, props from objects where type = 'Store' order by id").fetchall()
    finally:
        store.con.close()
    return item, [sid for sid, props in rows if json.loads(props).get("status") == "OPEN"][offset]


def _park_an_approval(c, offset):
    """A real parked action. An account's principal is user:<id> now (contract 1),
    which no pack policy names, so the principal the retail policy lets propose
    this acts through the engine directly, as an agent or a batch would."""
    item, store = _real_item_and_open_store(offset)
    pack = load_pack("retail")
    ont = Ontology(pack, Tracer(pack.paths.ensure().traces))
    try:
        parked = ont.act("propose_facing_change",
                         {"item_id": item, "store_id": store, "new_facings": 4},
                         actor="user:category.manager.1")
    finally:
        ont.store.con.close()
    assert parked["status"] == "needs_approval", parked
    return next(p for p in c.get("/workspaces/retail/approvals").json()
                if p["params"].get("item_id") == item and p["params"].get("store_id") == store)


@pytest.mark.parametrize("decision,offset", [("deny", 0), ("approve", 1)])
def test_a_decision_cannot_be_signed_by_an_agent(real, decision, offset):
    row = _park_an_approval(real, offset)
    r = real.post(f"/workspaces/retail/approvals/{row['seq']}/decide",
                  json={"approved_by": "agent:spoofed", "decision": decision})
    assert r.status_code == 422, r.text
    rows = real.get("/workspaces/retail/audit", params={"limit": 500}).json()
    assert not [x for x in rows if "agent:spoofed" in (x["actor"], x["approved_by"])]


def test_a_role_can_still_sign_an_approval_with_the_door_closed(real):
    row = _park_an_approval(real, 2)
    r = real.post(f"/workspaces/retail/approvals/{row['seq']}/decide",
                  json={"approved_by": "role:space_planning_lead", "decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] in ("applied", "applied_pending_sync"), r.text


# ----- agent principals

def test_no_account_can_hold_an_agent_principal(client, token):
    """Principals are user:<id>, built by the server. An email cannot change that."""
    auth.set_account("agent:triage@example.com", PASSWORD)
    s = client.post("/auth/login", json={"email": "agent:triage@example.com", "password": PASSWORD}).json()
    assert s["principal"] == f"user:{s['user_id']}" and not auth.is_agent(s["principal"])


def test_a_live_session_dies_if_its_membership_goes(client, token):
    with db.engine().begin() as con:
        con.execute(db.memberships.delete())
    assert client.get("/workspaces", headers=bearer(token)).status_code == 401


# ----- sessions

def _session_count() -> int:
    with db.engine().connect() as con:
        return len(con.execute(db.sessions.select()).all())


def test_token_expires_after_8h_idle(client, token, monkeypatch):
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + EIGHT_HOURS + 1)
    assert client.get("/workspaces", headers=bearer(token)).status_code == 401
    assert _session_count() == 0


def test_activity_slides_the_idle_window(client, token, monkeypatch):
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + EIGHT_HOURS - 60)
    assert client.get("/workspaces", headers=bearer(token)).status_code == 200
    # Past 8 hours from sign in, alive because it was used. The 12 hour cap is in test_auth_sessions.
    monkeypatch.setattr(auth, "_now", lambda: start + EIGHT_HOURS + 3600)
    assert client.get("/workspaces", headers=bearer(token)).status_code == 200


def test_login_rotates_the_session_it_came_in_on(client, token):
    again = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD},
                        headers=bearer(token)).json()["token"]
    assert again != token
    assert client.get("/workspaces", headers=bearer(token)).status_code == 401
    assert client.get("/workspaces", headers=bearer(again)).status_code == 200


def test_a_login_from_another_device_leaves_this_one_signed_in(client, token):
    """One session per user was the rule before sign out everywhere existed. Now each
    device has its own, and the sessions list can end any of them."""
    again = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()["token"]
    assert again != token
    assert client.get("/workspaces", headers=bearer(token)).status_code == 200


def test_login_only_rotates_its_own_account(client, token):
    auth.set_account("other@example.com", PASSWORD)
    other = client.post("/auth/login", json={"email": "other@example.com", "password": PASSWORD})
    assert other.status_code == 200
    assert client.get("/workspaces", headers=bearer(token)).status_code == 200


def test_login_clears_sessions_that_already_expired(client, token, monkeypatch):
    start = auth._now()
    auth.set_account("other@example.com", PASSWORD)
    monkeypatch.setattr(auth, "_now", lambda: start + EIGHT_HOURS + 1)
    client.post("/auth/login", json={"email": "other@example.com", "password": PASSWORD})
    assert _session_count() == 1


# ----- the dev flag

@pytest.mark.parametrize("value", ["0", "", "true", "yes", "11"])
def test_only_exactly_1_opens_the_dev_door(client, monkeypatch, value):
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", value)
    assert client.get("/workspaces").status_code == 401
    assert client.get("/auth/status").json()["dev_open"] is False


def test_dev_flag_keeps_todays_behavior(real, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    c = TestClient(real.app)
    assert c.get("/auth/status").json()["dev_open"] is True
    assert c.get("/workspaces").status_code == 200
    item = c.get("/workspaces/retail/objects/Item", params={"limit": 1}).json()[0]["_id"]
    url = f"/workspaces/retail/objects/Item/{item}/dashboard"
    assert c.get(url).json()["_as"] == auth.CONSOLE_PRINCIPAL
    assert c.get(url, headers={"X-Principal": "vendor:V-00001"}).json()["_as"] == "vendor:V-00001"
    asof = c.get(f"/workspaces/retail/objects/Item/{item}/asof",
                 params={"ts": "2200-01-01T00:00:00Z"},
                 headers={"X-Principal": "vendor:V-00001"}).json()
    assert asof["_as"] == "vendor:V-00001"


def test_status_reports_the_door_closed_by_default(client):
    assert client.get("/auth/status").json()["dev_open"] is False
