"""The MVP's proof: a second org can never see the first org's anything.

This walks app.routes, so a route added next month is covered or this file goes
red. A route whose path holds a param this file cannot classify is a failure,
never a skip. If you landed here from a red test on a new route:

- `/workspaces/{name}/...` needs nothing. The app-wide gate already scopes it.
- A route with a new param (say `/jobs/{id}`) goes in SELF_SCOPED, with a
  function that builds a real resource for org A and returns its path values.
  Its handler filters on ctx.org_id and answers NOT_FOUND. The sweep then holds
  it to the same three checks as everything else.
- A route with no param goes in UNSCOPED with a reason. If it can return tenant
  data (GET /jobs, POST /uploads), also make `world` create some of that data for
  A under a canary string, or test_nothing_b_can_reach_mentions_a has nothing to
  find. A resource named in a request body never reaches the path gate, so that
  handler must call tenancy.service.can_see_workspace itself.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from ironwright import auth
from ironwright.config import data_root
from ironwright.ingest import profiler
from ironwright.platform_api import (
    DEMO_ASK_POSTS,
    NOT_FOUND,
    READ_ONLY_POSTS,
    create_platform_app,
)

PASSWORD = "correct-horse-9"
DEMO = "retail"
GHOST = "no-such-resource-anywhere"
A_EMAIL = "canary-owner@a.example"
A_WS = "canary-ws"

# Routes with no path param, and why each one holds nothing tenant scoped.
UNSCOPED = {
    "/": "redirect to the console",
    "/archetypes": "static scenario catalog",
    "/platform/map": "static map of the product",
    "/auth/status": "instance state, no tenant data",
    "/auth/login": "the door",
    "/auth/signup": "the door",
    "/auth/accept-invite": "the door",
    "/auth/me": "answers for the caller's own session",
    "/auth/logout": "acts on the caller's own session",
    "/auth/sessions": "lists the caller's own sessions",
    "/auth/sessions/revoke-all": "ends the caller's own sessions",
    "/auth/verify": "the door: a link token in the body",
    "/auth/verify/resend": "mails the caller's own address",
    "/auth/reset-request": "the door: answers the same for every email",
    "/auth/reset": "the door: a link token in the body",
    "/workspaces": "filtered by ctx; the canary sweep proves it",
    "/jobs": "filtered by ctx; world queues a canary job, and the POST body is checked in the handler",
    "/uploads": "POST only, and it answers with what the caller just stored",
}
# Param routes that scope themselves: template -> f(client, a_session) -> path values
# of a real resource in org A.
def _a_job(c, a) -> dict[str, str]:
    ws = c.get("/jobs", headers=bearer(a["token"])).json()[0]["workspace"]  # world queued one
    r = c.post("/jobs", headers=bearer(a["token"]), json={"kind": "profile", "workspace": ws})
    assert r.status_code == 200, r.text
    return {"job_id": r.json()["id"]}


def _a_profiled_upload(c, a):
    made = c.post("/uploads", headers=bearer(a), files=[("files", (f"{A_WS}.csv", b"id,name\n1,ann\n"))])
    assert made.status_code == 201, made.text
    profiler.profile(a["org_id"], made.json()["id"], lambda *_: None)
    return {"upload_id": made.json()["id"]}


def _a_second_session(c, a):
    """Another device of A's, so ending it leaves the sweep's own token alive."""
    r = c.post("/auth/login", json={"email": A_EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    listed = c.get("/auth/sessions", headers=bearer(a["token"])).json()
    return {"session_id": next(s["id"] for s in listed if not s["current"])}


SELF_SCOPED: dict[str, object] = {
    "/auth/sessions/{session_id}": _a_second_session,
    "/jobs/{job_id}": _a_job,
    "/jobs/{job_id}/log": _a_job,
    "/jobs/{job_id}/cancel": _a_job,
    "/uploads/{upload_id}": _a_profiled_upload,
    "/uploads/{upload_id}/profile": _a_profiled_upload,
    "/uploads/{upload_id}/proposal": _a_profiled_upload,
}


def bearer(session: str | dict) -> dict[str, str]:
    token = session["token"] if isinstance(session, dict) else session
    return {"Authorization": f"Bearer {token}"}


def _routes(routes, prefix=""):
    """Every route, including lazily included routers. Unknown shapes fail."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _routes(included.routes, prefix + route.include_context.prefix)
        elif isinstance(route, Mount):
            continue
        else:
            assert hasattr(route, "path") and hasattr(route, "methods"), f"unswept route {route!r}"
            yield prefix + route.path, sorted(route.methods or ())


def _sweep(app):
    swept = [(t, m) for t, methods in _routes(app.routes) for m in methods]
    assert len(swept) > 50, "the sweep must see the whole app"
    templates = {t for t, _ in swept}
    assert {"/workspaces/{name}/objects/{type_}/{id_}/asof",
            "/workspaces/{name}/orgpolicy/preview",
            "/workspaces/{name}/escalations",
            "/workspaces/{name}/otel/export",
            "/workspaces/{name}/training/labels",
            "/workspaces/{name}/bi",
            "/workspaces/{name}/engines/check",
            "/orgs/{slug}/switch",
            "/orgs/{slug}/invites"} <= templates, "a router fell out of the sweep"
    return swept


def _fill(template, **values):
    def sub(m):
        return values.get(m.group(1), "1")
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", sub, template)


def _call(c, method, path, headers):
    kw = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
    return c.request(method, path, headers=headers, follow_redirects=False, **kw)


def _scope_param(template):
    """Which request value scopes this route, or fail. Never a silent pass."""
    if template in SELF_SCOPED:
        return "self"
    if template.startswith("/workspaces/{name}"):
        return "name"
    if template.startswith("/orgs/{slug}"):
        return "slug"
    assert "{" not in template, (
        f"{template} takes a param this proof cannot classify: see this file's docstring")
    assert template in UNSCOPED, (
        f"{template} is new and unclassified: see this file's docstring")
    return None


@pytest.fixture()
def world(meta_db):
    app = create_platform_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        a = c.post("/auth/signup", json={"email": A_EMAIL, "password": PASSWORD,
                                         "org_name": "Canary Alpha"}).json()
        b = c.post("/auth/signup", json={"email": "b@b.example", "password": PASSWORD,
                                         "org_name": "Beta org"}).json()
        made = c.post("/workspaces", headers=bearer(a["token"]),
                      json={"name": A_WS, "size": 4, "seed": 3})
        assert made.status_code == 201, made.text
        ws = made.json()["name"]
        queued = c.post("/jobs", headers=bearer(a["token"]), json={"kind": "profile", "workspace": ws})
        assert queued.status_code == 200, queued.text
        yield c, app, a, b, ws


def _scoped_targets(c, app, a, ws):
    """(template, method, A's real path, the same path at a resource nobody has)."""
    for template, method in _sweep(app):
        scope = _scope_param(template)
        if scope == "self":
            values = SELF_SCOPED[template](c, a)
            yield template, method, _fill(template, **values), _fill(
                template, **dict.fromkeys(values, GHOST))
        elif scope == "name":
            yield template, method, _fill(template, name=ws), _fill(template, name=GHOST)
        elif scope == "slug":
            yield template, method, _fill(template, slug=a["org_slug"]), _fill(template, slug=GHOST)


def test_every_route_is_classified(world):
    _, app, *_ = world
    for template, _method in _sweep(app):
        _scope_param(template)


def test_a_self_scoped_route_is_swept_and_a_leaky_one_goes_red(world, monkeypatch):
    """A route can opt out of the path gate, but it cannot opt out of the proof."""
    c, app, a, b, ws = world
    things = {"j1": a["org_id"]}

    @app.get("/things/{id}")
    def thing(id: str):  # no org filter: this is the bug the sweep must catch
        return {"id": id, "org": things.get(id)}

    monkeypatch.setitem(SELF_SCOPED, "/things/{id}", lambda _c, _a: {"id": "j1"})
    swept = [t for t in _scoped_targets(c, app, a, ws) if t[0] == "/things/{id}"]
    assert [(t[1], t[2], t[3]) for t in swept] == [("GET", "/things/j1", f"/things/{GHOST}")]
    with pytest.raises(AssertionError, match="/things/"):
        _b_is_stopped_everywhere(c, app, a, b, ws)


def test_the_canary_is_real(world):
    """Without this, every 404 below could mean A's data never existed."""
    c, _, a, _, ws = world
    assert ws in {w["name"] for w in c.get("/workspaces", headers=bearer(a["token"])).json()}
    assert c.get(f"/workspaces/{ws}/organization", headers=bearer(a["token"])).status_code == 200
    assert [j["workspace"] for j in c.get("/jobs", headers=bearer(a["token"])).json()] == [ws]
    assert c.post(f"/orgs/{a['org_slug']}/switch", headers=bearer(a["token"])).status_code == 200


def _b_is_stopped_everywhere(c, app, a, b, ws):
    checked = 0
    for template, method, real, ghost in _scoped_targets(c, app, a, ws):
        hit = _call(c, method, real, bearer(b["token"]))
        miss = _call(c, method, ghost, bearer(b["token"]))
        assert hit.status_code == 404, f"{method} {template} gave B {hit.status_code}: {hit.text}"
        assert hit.json() == {"detail": NOT_FOUND}, f"{method} {template}: {hit.text}"
        assert (hit.status_code, hit.content) == (miss.status_code, miss.content), (
            f"{method} {template} tells B the resource exists")
        checked += 1
    assert checked > 40
    assert c.get(f"/workspaces/{ws}/organization", headers=bearer(a["token"])).status_code == 200


def test_b_gets_the_same_404_on_everything_a_owns_as_on_nothing(world):
    _b_is_stopped_everywhere(*world)


def test_a_passes_the_gate_on_every_route_b_was_stopped_at(world):
    """The positive control. If the gate 404ed everybody, the test above still passes."""
    c, app, a, _, ws = world
    # The route that deletes the workspace itself goes last, or later requests 404 honestly.
    targets = sorted(_scoped_targets(c, app, a, ws), key=lambda t: t[:2] == ("/workspaces/{name}", "DELETE"))
    for template, method, real, _ in targets:
        if template.endswith("/switch"):  # rotates A's token out from under the sweep
            continue
        r = _call(c, method, real, bearer(a["token"]))
        stopped = r.status_code == 404 and r.content == b'{"detail":"%s"}' % NOT_FOUND.encode()
        assert not stopped, f"{method} {template} stopped A at A's own gate"
        assert r.status_code != 401, f"{method} {template} lost A's session"


def test_nothing_b_can_reach_mentions_a(world):
    c, app, a, b, ws = world
    canaries = (ws, a["org_id"], a["org_slug"], a["user_id"], A_EMAIL, "Canary Alpha")
    for template, method in _sweep(app):
        if method != "GET":
            continue
        r = _call(c, method, _fill(template, name=DEMO, slug=b["org_slug"]), bearer(b["token"]))
        for canary in canaries:
            assert canary not in r.text, f"{template} shows B {canary}"


def test_b_can_take_the_name_a_used_and_gets_its_own(world):
    c, _, a, b, ws = world
    r = c.post("/workspaces", headers=bearer(b["token"]), json={"name": A_WS, "size": 4, "seed": 4})
    assert r.status_code == 201, r.text
    theirs = r.json()["name"]
    assert theirs != ws
    assert c.get(f"/workspaces/{ws}/organization", headers=bearer(b["token"])).status_code == 404
    assert c.get(f"/workspaces/{theirs}/organization", headers=bearer(a["token"])).status_code == 404
    assert c.get(f"/workspaces/{theirs}/organization", headers=bearer(b["token"])).status_code == 200


def test_a_long_name_is_refused_in_the_terms_the_tenant_typed(world):
    c, _, a, _, _ = world
    r = c.post("/workspaces", headers=bearer(a["token"]), json={"name": "x" * 31})
    assert r.status_code == 422 and "30 characters" in r.json()["detail"]
    assert c.post("/workspaces", headers=bearer(a["token"]), json={"name": "x" * 30}).status_code == 201


@pytest.mark.parametrize("name", ["tenants", "demo"])
def test_the_dev_org_cannot_name_a_workspace_over_the_tenant_tree(meta_db, monkeypatch, name):
    """Deleting a workspace removes its folder. These two folders hold everyone."""
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    with TestClient(create_platform_app()) as c:
        assert c.post("/workspaces", json={"name": name}).status_code == 422
    assert not (data_root() / name).exists()


def test_b_cannot_delete_what_a_owns(world):
    c, _, a, b, ws = world
    assert c.delete(f"/workspaces/{ws}", headers=bearer(b["token"])).status_code == 404
    assert c.get(f"/workspaces/{ws}/organization", headers=bearer(a["token"])).status_code == 200


def test_a_folder_with_no_owner_is_nobodys_in_a_tenant_session(world):
    c, _, _, b, _ = world
    d = data_root() / "legacy-org"
    d.mkdir()
    (d / "scenario.yaml").write_text("name: legacy-org\n")
    (d / "org.json").write_text('{"company": "Legacy"}')
    assert "legacy-org" not in c.get("/workspaces", headers=bearer(b["token"])).text
    assert c.get("/workspaces/legacy-org/organization", headers=bearer(b["token"])).status_code == 404


def test_a_tenant_workspace_folder_scopes_by_org(world):
    """Contract 2's layout. T2 fills these folders; the gate already honors them."""
    c, _, a, b, _ = world
    (data_root() / "tenants" / a["org_id"] / "workspaces" / "claims").mkdir(parents=True)
    for_a = c.get("/workspaces/claims/approvals", headers=bearer(a["token"]))
    assert for_a.json() != {"detail": NOT_FOUND}
    assert c.get("/workspaces/claims/approvals", headers=bearer(b["token"])).json() == {"detail": NOT_FOUND}


@pytest.mark.parametrize("name", ["..", "..%2f..", "%2e%2e", ".hidden", "a b", "auth", "tenants"])
def test_odd_names_get_the_same_404(world, name):
    c, _, _, b, _ = world
    r = c.get(f"/workspaces/{name}/organization", headers=bearer(b["token"]))
    assert r.status_code == 404


def test_every_write_on_a_demo_pack_is_refused_for_a_tenant(world):
    """Demo packs are shared. One tenant's write there is every tenant's read."""
    c, app, a, _, _ = world
    checked = 0
    for template, method in _sweep(app):
        if not template.startswith("/workspaces/{name}") or method in ("GET", "HEAD"):
            continue
        r = _call(c, method, _fill(template, name=DEMO), bearer(a["token"]))
        if template in READ_ONLY_POSTS or template in DEMO_ASK_POSTS:
            assert r.status_code != 403, f"{method} {template} is a read"
        else:
            assert r.status_code == 403, f"{method} {template} gave {r.status_code}: {r.text}"
            checked += 1
    assert checked > 15


def test_a_tenant_can_read_a_demo_pack(world):
    c, _, a, _, _ = world
    names = {w["name"] for w in c.get("/workspaces", headers=bearer(a["token"])).json()}
    assert DEMO in names
    assert c.get(f"/workspaces/{DEMO}/sources", headers=bearer(a["token"])).status_code == 200


def _member(c, owner, role):
    tok = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner["token"]),
                 json={"email": f"{role}@a.example", "role": role}).json()["token"]
    return c.post("/auth/accept-invite", json={"token": tok, "password": PASSWORD}).json()


def _writes(app, ws):
    for template, method in _sweep(app):
        if template.startswith("/workspaces/{name}") and method not in ("GET", "HEAD"):
            yield template, method, _fill(template, name=ws)


def test_a_viewer_can_read_and_can_write_nothing(world):
    c, app, a, _, ws = world
    v = _member(c, a, "viewer")
    assert c.get(f"/workspaces/{ws}/organization", headers=bearer(v["token"])).status_code == 200
    for template, method, path in _writes(app, ws):
        r = _call(c, method, path, bearer(v["token"]))
        if template in READ_ONLY_POSTS:
            assert r.status_code != 403, template
        else:
            assert r.status_code == 403, f"{method} {template} let a viewer through: {r.status_code}"
    assert c.post("/workspaces", headers=bearer(v["token"]),
                  json={"name": "nope-ws"}).status_code == 403


@pytest.mark.parametrize("suffix,method", [
    ("runs", "POST"), ("ask", "POST"), ("actions/x", "POST"),
])
def test_a_member_can_run_and_ask(world, suffix, method):
    c, _, a, _, ws = world
    m = _member(c, a, "member")
    assert c.request(method, f"/workspaces/{ws}/{suffix}", headers=bearer(m["token"]),
                     json={"question": "q"}).status_code != 403


@pytest.mark.parametrize("suffix", [
    "approvals/1/decide", "escalations/1/decide", "orgpolicy/apply", "otel/export", "bi/mirror",
])
def test_approving_and_shipping_data_out_need_admin(world, suffix):
    c, _, a, _, ws = world
    m = _member(c, a, "member")
    adm = _member(c, a, "admin")
    assert c.post(f"/workspaces/{ws}/{suffix}", headers=bearer(m["token"]), json={}).status_code == 403
    assert c.post(f"/workspaces/{ws}/{suffix}", headers=bearer(adm["token"]), json={}).status_code != 403


def test_a_new_write_route_nobody_listed_needs_owner(meta_db):
    """Default deny for roles: forgetting the table fails closed."""
    app = create_platform_app()

    @app.post("/workspaces/{name}/brand-new")
    def brand_new(name: str):
        return {"ok": True}

    with TestClient(app) as c:
        a = c.post("/auth/signup", json={"email": "a@a.example", "password": PASSWORD,
                                         "org_name": "Org one"}).json()
        ws = c.post("/workspaces", headers=bearer(a["token"]), json={"name": "some-ws"}).json()["name"]
        adm = _member(c, a, "admin")
        assert c.post(f"/workspaces/{ws}/brand-new", headers=bearer(adm["token"])).status_code == 403
        assert c.post(f"/workspaces/{ws}/brand-new", headers=bearer(a["token"])).status_code == 200


def test_a_new_workspace_route_is_gated_without_anyone_opting_in(meta_db):
    app = create_platform_app()

    @app.get("/workspaces/{name}/brand-new")
    def brand_new(name: str):
        return {"secret": name}

    with TestClient(app) as c:
        a = c.post("/auth/signup", json={"email": "a@a.example", "password": PASSWORD,
                                         "org_name": "Org one"}).json()
        b = c.post("/auth/signup", json={"email": "b@a.example", "password": PASSWORD,
                                         "org_name": "Org two"}).json()
        ws = c.post("/workspaces", headers=bearer(a["token"]), json={"name": "some-ws"}).json()["name"]
        assert c.get(f"/workspaces/{ws}/brand-new", headers=bearer(a["token"])).status_code == 200
        assert c.get(f"/workspaces/{ws}/brand-new", headers=bearer(b["token"])).status_code == 404


def test_the_principal_is_the_session_and_never_the_header(world):
    c, _, a, _, _ = world
    me = c.get("/auth/me", headers={**bearer(a["token"]), "X-Principal": "agent:spoofed"}).json()
    assert me["principal"] == f"user:{a['user_id']}"
    assert auth.is_agent(me["principal"]) is False


@pytest.fixture(scope="module")
def demo_data_root(tmp_path_factory):
    """One real retail run, so tenants read real objects off a real demo pack."""
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("isolation-demo")
    mp.setenv("IRONWRIGHT_DATA", str(root))
    mp.setenv("IRONWRIGHT_DEV_OPEN", "1")
    mp.delenv("IRONWRIGHT_DB_URL", raising=False)
    with TestClient(create_platform_app()) as c:
        assert c.post(f"/workspaces/{DEMO}/runs", json={"seed": 7}).status_code == 200
    mp.undo()
    return root


def test_reading_a_demo_pack_leaves_no_trace_another_tenant_can_see(demo_data_root, monkeypatch):
    """A demo pack is the one thing two tenants share, so it is the likely leak."""
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(demo_data_root))
    app = create_platform_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        a = c.post("/auth/signup", json={"email": A_EMAIL, "password": PASSWORD,
                                         "org_name": "Canary Alpha"}).json()
        b = c.post("/auth/signup", json={"email": "b@b.example", "password": PASSWORD,
                                         "org_name": "Beta org"}).json()
        ah, bh = bearer(a["token"]), bearer(b["token"])
        item = c.get(f"/workspaces/{DEMO}/objects/Item", params={"limit": 1}, headers=ah).json()[0]["_id"]
        for path in (f"/objects/Item/{item}", f"/objects/Item/{item}/dashboard", "/ask/examples",
                     f"/objects/Item/{item}/asof?ts=2200-01-01T00:00:00Z"):
            assert c.get(f"/workspaces/{DEMO}{path}", headers=ah).status_code == 200, path
        q = c.post(f"/workspaces/{DEMO}/governance/check", headers=ah,
                   json={"principal": a["principal"], "action": "action:delist_item"})
        assert q.status_code == 200, q.text
        canaries = (a["org_id"], a["user_id"], a["org_slug"], A_EMAIL)
        for template, method in _sweep(app):
            if method != "GET" or not template.startswith("/workspaces/{name}"):
                continue
            r = _call(c, method, _fill(template, name=DEMO), bh)
            for canary in canaries:
                assert canary not in r.text, f"{template} shows B {canary}"
        for trace in c.get(f"/workspaces/{DEMO}/traces", headers=bh).json():
            text = c.get(f"/workspaces/{DEMO}/traces/{trace['trace_id']}", headers=bh).text
            for canary in canaries:
                assert canary not in text, f"trace {trace['trace_id']} shows B {canary}"
