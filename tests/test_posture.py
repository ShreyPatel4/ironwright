"""The serving posture: what a real `ironwright serve|platform|mcp` exposes.

Every HTTP test goes through cli.main with uvicorn.run captured, so what is
proved is the object that actually gets served, not a parallel construction.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import pathlib
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ironwright import cli
from ironwright.api import MAX_BODY_BYTES, harden
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app
from ironwright.tracing import Tracer

REPO = pathlib.Path(__file__).resolve().parents[1]
PACK = "retail"
LOGIN = {"email": "nobody@example.com", "password": "wrong-password"}


@pytest.fixture(scope="module", autouse=True)
def built_pack(tmp_path_factory):
    root = tmp_path_factory.mktemp("posture")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        pack = load_pack(PACK)
        tracer = Tracer(pack.paths.ensure().traces)
        args = argparse.Namespace(seed=None, limit=None)
        for stage in ("generate", "migrate", "reconcile", "ontology"):
            cli.STAGES[stage](pack, tracer, args)
        yield root
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


@pytest.fixture()
def launch(monkeypatch):
    """Run the CLI up to the point uvicorn would start, return what it was given."""
    def run(*argv: str) -> dict:
        seen: dict = {}

        def fake_run(app, **kwargs):
            seen.update(kwargs, app=app)

        monkeypatch.setattr("uvicorn.run", fake_run)
        cli.main(list(argv))
        return seen
    return run


@pytest.fixture()
def platform(launch):
    return launch("platform")["app"]


# ------------------------------------------------------------------ binding

@pytest.mark.parametrize("argv", [("serve", "--pack", PACK), ("platform",)])
def test_serve_commands_bind_loopback_by_default(launch, argv):
    assert launch(*argv)["host"] == "127.0.0.1"


@pytest.mark.parametrize("argv", [("serve", "--pack", PACK), ("platform",)])
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "example.com", "", "127.1"])
def test_non_loopback_host_without_the_flag_refuses_to_start(launch, capsys, argv, host):
    with pytest.raises(SystemExit) as e:
        launch(*argv, "--host", host)
    assert e.value.code != 0 and "--i-know-this-is-exposed" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts_need_no_flag(launch, host):
    assert launch("platform", "--host", host)["host"] == host


def test_exposure_needs_both_host_and_flag(launch, capsys):
    assert launch("platform", "--i-know-this-is-exposed")["host"] == "127.0.0.1"
    assert launch("platform", "--host", "0.0.0.0", "--i-know-this-is-exposed")["host"] == "0.0.0.0"
    assert "exposed" in capsys.readouterr().err


def test_the_exposure_flag_cannot_be_abbreviated(launch):
    with pytest.raises(SystemExit):
        launch("platform", "--host", "0.0.0.0", "--i-know-this-is")


def test_forwarded_headers_are_not_trusted(launch):
    assert launch("platform")["proxy_headers"] is False


# ------------------------------------------------------------------ CORS and origin

def test_no_cross_origin_is_allowed_by_default(platform):
    c = TestClient(platform)
    r = c.get("/workspaces", headers={"origin": "https://evil.example"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
    pre = c.options("/workspaces", headers={"origin": "https://evil.example",
                                             "access-control-request-method": "POST"})
    assert pre.status_code == 400 and "access-control-allow-origin" not in pre.headers


def test_configured_console_origin_is_the_only_one_allowed(launch, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_CONSOLE_ORIGIN", "https://console.example")
    c = TestClient(launch("platform")["app"])
    ok = c.get("/workspaces", headers={"origin": "https://console.example"})
    assert ok.headers["access-control-allow-origin"] == "https://console.example"
    bad = c.get("/workspaces", headers={"origin": "https://console.example.evil.test"})
    assert "access-control-allow-origin" not in bad.headers


def test_preflight_will_not_carry_a_principal_header(launch, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_CONSOLE_ORIGIN", "https://console.example")
    c = TestClient(launch("platform")["app"])
    r = c.options("/workspaces", headers={"origin": "https://console.example",
                                           "access-control-request-method": "POST",
                                           "access-control-request-headers": "x-principal"})
    assert r.status_code == 400


def test_wildcard_console_origin_is_refused(launch, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_CONSOLE_ORIGIN", "*")
    with pytest.raises(ValueError):
        launch("platform")


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_foreign_origin_cannot_write_even_without_preflight(platform, origin):
    # A cross-site form post skips preflight, so CORS headers alone would not stop it.
    r = TestClient(platform).post("/workspaces", headers={"origin": origin, "content-type": "text/plain"},
                                  content='{"name": "planted"}')
    assert r.status_code == 403


def test_configured_console_origin_can_write(launch, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_CONSOLE_ORIGIN", "https://console.example")
    c = TestClient(launch("platform")["app"])
    r = c.post("/auth/login", headers={"origin": "https://console.example"}, json=LOGIN)
    assert r.status_code == 401


def test_same_origin_write_passes_the_origin_check(platform):
    r = TestClient(platform).post("/auth/login", headers={"origin": "http://testserver"}, json=LOGIN)
    assert r.status_code == 401


# ------------------------------------------------------------------ security headers

@pytest.mark.parametrize("path", ["/auth/status", "/definitely/not/a/route"])
def test_security_headers_on_every_answer(platform, path):
    h = TestClient(platform).get(path).headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in h


def test_security_headers_survive_the_refusals(platform):
    c = TestClient(platform)
    refused = c.post("/auth/login", headers={"origin": "https://evil.example"}, json=LOGIN)
    assert refused.status_code == 403 and "content-security-policy" in refused.headers
    big = c.post("/auth/login", content=b"x" * (MAX_BODY_BYTES + 1))
    assert big.status_code == 413 and "content-security-policy" in big.headers


def test_csp_lets_the_console_and_bi_frame_work_and_nothing_else(platform):
    csp = dict(d.strip().split(" ", 1) for d in
               TestClient(platform).get("/auth/status").headers["content-security-policy"].split(";"))
    assert csp["default-src"] == "'self'"
    assert csp["script-src"] == "'self'"                  # no inline script, ever
    assert csp["frame-ancestors"] == "'self'"             # only we may frame us
    assert csp["frame-src"] == "http: https:"             # the user's BI URL, sandboxed by the console
    assert "https://fonts.googleapis.com" in csp["style-src"]
    assert "unsafe-inline" not in csp["style-src"]
    assert "https://fonts.gstatic.com" in csp["font-src"]
    assert csp["object-src"] == "'none'"


# ------------------------------------------------------------------ body cap

def test_oversize_declared_body_is_refused(platform):
    r = TestClient(platform).post("/auth/login", content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413


def test_oversize_chunked_body_is_refused(platform):
    # No Content-Length to check: only counting the bytes as they arrive catches this.
    def chunks():
        for _ in range(3):
            yield b"x" * (MAX_BODY_BYTES // 2)

    r = TestClient(platform).post("/auth/login", content=chunks())
    assert r.status_code == 413


def test_normal_body_passes_the_cap(platform):
    assert TestClient(platform).post("/auth/login", json=LOGIN).status_code == 401


# ------------------------------------------------------------------ login rate limit

def _limited(clock=None):
    app = harden(create_platform_app(), **({"clock": clock} if clock else {}))
    return lambda ip: TestClient(app, client=(ip, 5000))


def _attempts_until_429(client, cap: int = 50) -> int:
    for n in range(1, cap + 1):
        if client.post("/auth/login", json=LOGIN).status_code == 429:
            return n
    raise AssertionError(f"never rate limited in {cap} attempts")


def test_login_is_rate_limited_per_ip(platform):
    c = TestClient(platform, client=("10.0.0.1", 5000))
    assert _attempts_until_429(c) > 1
    r = c.post("/auth/login", json=LOGIN)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_one_ip_being_limited_does_not_lock_out_another():
    client = _limited()
    _attempts_until_429(client("10.0.0.1"))
    assert client("10.0.0.2").post("/auth/login", json=LOGIN).status_code == 401


def test_a_forged_forwarded_header_does_not_buy_more_attempts(platform):
    c = TestClient(platform, client=("10.0.0.1", 5000))
    _attempts_until_429(c)
    r = c.post("/auth/login", headers={"x-forwarded-for": "10.9.9.9"}, json=LOGIN)
    assert r.status_code == 429


def test_rate_limit_window_expires():
    now = [1000.0]
    client = _limited(lambda: now[0])("10.0.0.1")
    _attempts_until_429(client)
    now[0] += 3600
    assert client.post("/auth/login", json=LOGIN).status_code == 401


def test_rate_limit_applies_to_login_only(platform):
    c = TestClient(platform, client=("10.0.0.1", 5000))
    _attempts_until_429(c)
    assert c.get("/auth/status").status_code == 200


# ------------------------------------------------------------------ kill switch

# POST routes that only read: these stay up. Anything else that writes is off,
# so a route added later is off by default rather than forgotten.
READ_ONLY_WRITES = {"/auth/login", "/auth/logout", "/workspaces/retail/lake/query",
                    "/workspaces/retail/boards/x/run", "/workspaces/retail/governance/check"}


def _routes(app):
    for route in app.routes:
        if isinstance(route, APIRoute):
            path = route.path.replace("{name}", PACK)
            yield re.sub(r"\{[^}]+\}", "x", path), route.methods - {"HEAD", "OPTIONS"}


def _hit(client, method, path):
    return client.request(method, path, json={} if method != "GET" else None).status_code


def test_kill_switch_stays_out_of_the_way_when_off(platform):
    c = TestClient(platform, raise_server_exceptions=False)
    assert c.post(f"/workspaces/{PACK}/ask", json={}).status_code != 503


@pytest.mark.parametrize("value", ["1", "true", "on"])
def test_kill_switch_turns_off_ask_and_actions(platform, monkeypatch, value):
    monkeypatch.setenv("IRONWRIGHT_KILL_SWITCH", value)
    c = TestClient(platform)
    for path in ("ask", "ask/replay", "actions/delist_item", "approvals/1/decide", "runs",
                 "escalations", "escalations/x/decide"):
        assert c.post(f"/workspaces/{PACK}/{path}", json={}).status_code == 503, path


def test_kill_switch_can_be_set_off_explicitly(platform, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_KILL_SWITCH", "0")
    c = TestClient(platform, raise_server_exceptions=False)
    assert c.post(f"/workspaces/{PACK}/ask", json={}).status_code != 503


def test_kill_switch_covers_every_write_route_and_no_read_route(platform, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_KILL_SWITCH", "1")
    c = TestClient(platform, raise_server_exceptions=False)
    seen_read_writes = set()
    for path, methods in _routes(platform):
        for method in methods:
            status = _hit(c, method, path)
            if method == "GET":
                assert status != 503, f"read went down: GET {path}"
            elif path in READ_ONLY_WRITES:
                seen_read_writes.add(path)
                assert status != 503, f"read went down: {method} {path}"
            else:
                assert status == 503, f"write stayed up: {method} {path}"
    assert seen_read_writes == READ_ONLY_WRITES


def test_kill_switch_leaves_real_reads_answering(platform, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_KILL_SWITCH", "1")
    c = TestClient(platform)
    assert c.get(f"/workspaces/{PACK}/ontology").status_code == 200
    r = c.post(f"/workspaces/{PACK}/lake/query", json={"sql": "select 1 as one"})
    assert r.status_code == 200


def test_kill_switch_on_the_single_pack_api(launch, monkeypatch):
    app = launch("serve", "--pack", PACK)["app"]
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/actions/delist_item", json={"params": {}}).status_code != 503
    monkeypatch.setenv("IRONWRIGHT_KILL_SWITCH", "1")
    assert c.post("/actions/delist_item", json={"params": {}}).status_code == 503
    assert c.post("/functions/nope", json={"args": {}}).status_code != 503
    assert c.get("/").status_code == 200


# ------------------------------------------------------------------ MCP

def _mcp_tools(server) -> list:
    return asyncio.run(server.list_tools())


def _mcp_call(server, name: str, args: dict) -> str:
    return asyncio.run(server.call_tool(name, args)).content[0].text


def test_mcp_exposes_exactly_the_ontology_functions_and_actions():
    from ironwright.mcp_server import build_server
    pack = load_pack(PACK)
    want = ({f"fn_{n}" for n in pack.ontology.get("functions", {})}
            | {f"act_{n}" for n in pack.ontology.get("actions", {})})
    assert want and {t.name for t in _mcp_tools(build_server(PACK))} == want


def test_mcp_caller_cannot_pick_its_identity_or_approve():
    from ironwright.mcp_server import build_server
    for tool in _mcp_tools(build_server(PACK)):
        fields = set(tool.input_schema.get("properties", {}))
        assert not fields & {"principal", "actor", "on_behalf_of", "approved_by"}, tool.name


@pytest.mark.parametrize("principal", ["user:ceo", "vendor:V-00001", "agent:", "mcp-client"])
def test_mcp_refuses_to_run_as_anything_but_an_agent(monkeypatch, principal):
    from ironwright.mcp_server import build_server
    monkeypatch.setenv("IRONWRIGHT_PRINCIPAL", principal)
    with pytest.raises(ValueError):
        build_server(PACK)


def test_mcp_runs_on_stdio_and_binds_nothing(monkeypatch):
    seen = {}

    class Fake:
        def run(self, *args, **kwargs):
            seen.update(args=args, kwargs=kwargs)

    monkeypatch.setattr("ironwright.mcp_server.build_server", lambda name: Fake())
    cli.main(["mcp", "--pack", PACK])
    assert seen == {"args": (), "kwargs": {}}   # MCPServer.run() defaults to stdio


def test_kill_switch_turns_off_mcp_actions_and_leaves_functions(monkeypatch):
    from ironwright.mcp_server import build_server
    pack = load_pack(PACK)
    server = build_server(PACK)
    act = next(iter(pack.ontology["actions"]))
    fn = next(iter(pack.ontology["functions"]))
    assert "kill switch" not in _mcp_call(server, f"act_{act}", {"params": {}})
    monkeypatch.setenv("IRONWRIGHT_KILL_SWITCH", "1")
    assert "kill switch" in json.loads(_mcp_call(server, f"act_{act}", {"params": {}}))["error"]
    assert "kill switch" not in _mcp_call(server, f"fn_{fn}", {"args": {}})


# ------------------------------------------------------------------ threat model

THREAT_MODEL = REPO / "docs" / "threat-model.md"
TEST_LINK = re.compile(r"tests/(test_\w+\.py)::(test_\w+)")


def _control_rows() -> list[str]:
    text = THREAT_MODEL.read_text()
    section = text.split("## Controls", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|")]
    return rows[2:]   # past the table header and its rule


def test_threat_model_has_the_sections_the_ticket_asks_for():
    text = THREAT_MODEL.read_text()
    for heading in ("## Assets", "## Actors", "## Trust boundaries", "## Controls"):
        assert heading in text, heading
    assert "```mermaid" in text
    assert "—" not in text   # house style: never em dashes


def test_every_test_the_threat_model_links_really_exists():
    links = TEST_LINK.findall(THREAT_MODEL.read_text())
    assert links
    for filename, name in links:
        path = REPO / "tests" / filename
        assert path.exists(), filename
        defined = {n.name for n in ast.walk(ast.parse(path.read_text()))
                   if isinstance(n, ast.FunctionDef)}
        assert name in defined, f"{filename}::{name}"


def test_every_control_links_a_test_or_says_it_is_pending():
    rows = _control_rows()
    assert rows
    for row in rows:
        assert TEST_LINK.search(row) or "PENDING" in row, row


@pytest.mark.xfail(strict=True, reason="A1 to A4 not merged yet; integration fills these rows and drops this mark")
def test_no_control_is_still_pending():
    assert not [r for r in _control_rows() if "PENDING" in r]
