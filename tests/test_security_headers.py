"""Tier 1 item 9 of docs/enterprise-security.md: response headers and closed CORS,
on the served platform app, on answers and on refusals alike."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ironwright.api import harden
from ironwright.platform_api import create_platform_app


@pytest.fixture()
def app(meta_db):
    return harden(create_platform_app())


def csp_of(headers) -> dict[str, str]:
    return dict(d.strip().split(" ", 1) for d in headers["content-security-policy"].split(";"))


@pytest.mark.parametrize("path", ["/auth/status", "/no/such/route"])
def test_hsts_is_on_every_answer(app, path):
    h = TestClient(app).get(path).headers
    assert h["strict-transport-security"] == "max-age=63072000; includeSubDomains"


def test_nobody_may_frame_us(app):
    assert csp_of(TestClient(app).get("/auth/status").headers)["frame-ancestors"] == "'none'"


def test_no_inline_script_is_allowed(app):
    csp = csp_of(TestClient(app).get("/auth/status").headers)
    assert csp["script-src"] == "'self'"
    assert "unsafe" not in csp["script-src"]


def test_permissions_policy_turns_off_device_apis(app):
    policy = TestClient(app).get("/auth/status").headers["permissions-policy"]
    for feature in ("camera=()", "microphone=()", "geolocation=()", "payment=()", "usb=()"):
        assert feature in policy


def test_the_headers_survive_a_refusal(app):
    r = TestClient(app).post("/auth/login", headers={"origin": "https://evil.example"},
                             json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 403
    assert "strict-transport-security" in r.headers and "permissions-policy" in r.headers


def test_cors_is_closed_by_default(app):
    r = TestClient(app).options("/auth/login", headers={"origin": "https://console.example",
                                                         "access-control-request-method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_a_configured_console_origin_may_send_the_csrf_header(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_CONSOLE_ORIGIN", "https://console.example")
    r = TestClient(harden(create_platform_app())).options(
        "/auth/logout", headers={"origin": "https://console.example", "access-control-request-method": "POST",
                                 "access-control-request-headers": "x-csrf-token"})
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == "https://console.example"
