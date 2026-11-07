"""The platform served under a prefix, the way coconutlabs.org mounts it.

Vercel rewrites coconutlabs.org/projects/ironwright/app/* to the Worker on workers.dev, keeping
the path. The Worker hands the request to the container as it came. So the app sees
/projects/ironwright/app/... behind the rewrite and /... at the root of the workers.dev host,
and has to answer both. Next.js also strips a trailing slash before it rewrites, so nothing
the browser lands on may depend on one.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ironwright import basepath, hosted, platform_api

PREFIX = "/projects/ironwright/app"
INDEX = ('<!doctype html><html><head><meta charset="utf-8"><script type="module" src="./assets/app.js">'
         "</script></head><body></body></html>")


@pytest.fixture()
def console(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX)
    (dist / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(platform_api, "CONSOLE_DIST", dist)
    return dist


@pytest.fixture()
def client(tmp_path, monkeypatch, console):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("IRONWRIGHT_BASE_PATH", PREFIX)
    for name in ("IRONWRIGHT_DEV_OPEN", "IRONWRIGHT_ENV", "IRONWRIGHT_DB_URL", "IRONWRIGHT_LAKE_URI"):
        monkeypatch.delenv(name, raising=False)
    return TestClient(hosted.create_hosted_app(), base_url="http://ironwright-platform.example.workers.dev")


@pytest.mark.parametrize("given, want", [("", ""), ("/", ""), ("/projects/ironwright/app/", PREFIX),
                                         ("projects/ironwright/app", PREFIX), (PREFIX, PREFIX)])
def test_the_prefix_is_one_leading_slash_and_no_trailing_one(monkeypatch, given, want):
    monkeypatch.setenv("IRONWRIGHT_BASE_PATH", given)
    assert basepath.prefix() == want


@pytest.mark.parametrize("bad", ["/a/../b", "/a b", "/a?x", "/a#x", "//evil.example"])
def test_a_prefix_that_is_not_a_plain_path_is_refused(monkeypatch, bad):
    monkeypatch.setenv("IRONWRIGHT_BASE_PATH", bad)
    with pytest.raises(ValueError, match="IRONWRIGHT_BASE_PATH"):
        basepath.prefix()


@pytest.mark.parametrize("root", ["", PREFIX])
def test_health_answers_under_the_prefix_and_at_the_root(client, root):
    assert client.get(f"{root}/healthz").json() == {"status": "ok"}
    assert client.get(f"{root}/auth/status").status_code == 200


def test_a_path_that_only_starts_like_the_prefix_is_not_stripped(client):
    assert client.get(f"{PREFIX}x/healthz").status_code == 404


def test_the_bare_prefix_lands_on_the_console_under_the_prefix(client):
    r = client.get(PREFIX, follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == f"{PREFIX}/console/"


def test_the_root_still_lands_on_the_console_at_the_root(client):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/console/"


@pytest.mark.parametrize("path", ["/console/", "/console"])
def test_the_console_page_names_its_own_base_with_or_without_the_slash(client, path):
    """Next strips the slash, then the app would add it back: a loop. So /console answers too."""
    r = client.get(PREFIX + path, follow_redirects=False)
    assert r.status_code == 200
    assert f'<head><base href="{PREFIX}/console/">' in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_the_console_page_at_the_root_names_the_root(client):
    r = client.get("/console", follow_redirects=False)
    assert r.status_code == 200 and '<base href="/console/">' in r.text


def test_assets_resolve_under_the_prefix(client):
    r = client.get(f"{PREFIX}/console/assets/app.js")
    assert r.status_code == 200 and r.text == "console.log(1)"


def test_a_redirect_the_app_builds_from_its_own_host_keeps_the_prefix(client):
    """StaticFiles answers a folder without its slash with an absolute URL on the Host it saw.
    Behind the rewrite that host is not the browser's, so the Location goes back to a path."""
    r = client.get(f"{PREFIX}/console/assets", follow_redirects=False)
    if r.status_code in (301, 302, 307, 308):
        assert r.headers["location"].startswith(f"{PREFIX}/console/assets")


def test_a_redirect_to_another_host_is_left_alone():
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", b"https://elsewhere.example/x")]})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    import asyncio
    scope = {"type": "http", "path": f"{PREFIX}/go", "raw_path": f"{PREFIX}/go".encode(), "headers": [(b"host", b"h")]}
    asyncio.run(basepath.BasePath(app, PREFIX)(scope, receive, send))
    assert dict(sent[0]["headers"])[b"location"] == b"https://elsewhere.example/x"


def test_the_signup_door_works_through_the_prefix(client):
    r = client.post(f"{PREFIX}/auth/signup", json={"email": "a@example.com", "password": "correct horse battery",
                                                   "org_name": "Acme"})
    assert r.status_code == 201, r.text
    me = client.get(f"{PREFIX}/auth/me", headers={"authorization": f"Bearer {r.json()['token']}"})
    assert me.status_code == 200 and me.json()["org_slug"] == r.json()["org_slug"]


def test_without_a_prefix_nothing_changes(tmp_path, monkeypatch, console):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("IRONWRIGHT_BASE_PATH", raising=False)
    c = TestClient(hosted.create_hosted_app())
    assert c.get("/healthz").status_code == 200
    assert c.get(f"{PREFIX}/healthz").status_code == 404
    assert '<base href="/console/">' in c.get("/console/").text
