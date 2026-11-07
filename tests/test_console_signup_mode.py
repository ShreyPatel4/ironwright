"""The door in a real browser when signups are by invite and a demo org is set, as in prod.

Nobody can make an org, so the console does not offer to. A visitor takes the demo door
instead, lands on the workspaces as a viewer, and can open a demo pack. No route stubs.
"""
from __future__ import annotations

import threading
import time

import pytest
import uvicorn
from cryptography.fernet import Fernet
from test_console_settings import (  # noqa: F401  fixtures are found by name
    browser,
    console_dist,
)

from ironwright.api import harden
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import db


@pytest.fixture()
def invite_only(console_dist, monkeypatch, tmp_path):  # noqa: F811
    for name in ("IRONWRIGHT_DEV_OPEN", "IRONWRIGHT_DB_URL", "IRONWRIGHT_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("IRONWRIGHT_SIGNUP", "invite")
    monkeypatch.setenv("IRONWRIGHT_DEMO_ORG", "ironwright-demo")
    server = uvicorn.Server(uvicorn.Config(harden(create_platform_app()), host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(10)
    db.dispose_current()


def test_a_visitor_takes_the_demo_door_and_opens_a_pack(browser, invite_only):  # noqa: F811
    from playwright.sync_api import expect
    context = browser.new_context()
    context.set_default_timeout(15_000)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{invite_only}/console/")
    expect(page.get_by_role("button", name="Enter the console")).to_be_visible()
    expect(page.get_by_role("button", name="Sign up")).to_have_count(0)
    expect(page.get_by_text("by invite")).to_be_visible()
    page.get_by_role("button", name="Try the demo").click()
    expect(page.get_by_role("heading", name="Workspaces")).to_be_visible()
    expect(page.get_by_text("viewer", exact=False).first).to_be_visible()
    page.get_by_role("button", name="Retail", exact=False).first.click()
    expect(page.get_by_role("button", name="Pipeline")).to_be_visible()
    assert errors == []
    context.close()
