"""The screens the film shows, while they load and when they fail.

A real server with nothing stubbed. Slow is a real delay on the wire and failed is a browser that
really went offline. An empty inbox on a failed read is the bug these hold shut: it tells an
approver nothing is waiting when the truth is that nobody knows.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery"


@pytest.fixture(scope="module")
def browser() -> Iterator:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except sync_api.Error as e:
            pytest.skip(f"no chromium for playwright, so this was not proved: {e}")
        yield chromium
        chromium.close()


@pytest.fixture(scope="module")
def live(browser, tmp_path_factory) -> Iterator[str]:
    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm not found: the console cannot be built, so this was not proved")
    console = ROOT / "console"
    if not (console / "node_modules").exists():
        subprocess.run([npm, "ci"], cwd=console, check=True, capture_output=True)
    subprocess.run([npm, "run", "build"], cwd=console, check=True, capture_output=True)
    root = tmp_path_factory.mktemp("polish")
    env = {k: v for k, v in os.environ.items() if k not in ("IRONWRIGHT_DEV_OPEN", "IRONWRIGHT_DB_URL")}
    env["IRONWRIGHT_DATA"] = str(root)
    env["PYTHONPATH"] = str(ROOT)  # this checkout, not whatever the venv points at
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log = root / "platform.log"
    with log.open("w") as out:
        server = subprocess.Popen([sys.executable, "-m", "ironwright.cli", "platform", "--port", str(port)],
                                  cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while True:
                assert server.poll() is None, log.read_text()
                try:
                    urllib.request.urlopen(f"{base}/auth/status", timeout=2).close()
                    break
                except (urllib.error.URLError, OSError):
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.2)
            yield base
        finally:
            server.terminate()
            server.wait(15)


class Person:
    """A fresh signup, parked at the console's rail on a demo company."""

    def __init__(self, browser, base: str) -> None:
        self.context = browser.new_context()
        self.context.set_default_timeout(20_000)
        self.page = self.context.new_page()
        self.cdp = self.context.new_cdp_session(self.page)
        self.cdp.send("Network.enable")
        page = self.page
        page.goto(f"{base}/console/")
        page.get_by_role("button", name="Sign up").click()
        page.get_by_label("org name").fill("Polish org")
        page.get_by_label("email").fill(f"polish-{uuid.uuid4().hex[:8]}@acme.test")
        page.get_by_label("password").fill(PASSWORD)
        page.get_by_role("button", name="Create the org").click()
        page.get_by_role("button", name="Go to the console").click()

    def open_demo(self) -> None:
        self.page.locator("button.card").filter(has_text="demo company").first.click()

    def rail(self, name: str) -> None:
        self.page.locator("nav.rail").get_by_role("button", name=name, exact=True).click()

    def slow_wire(self, ms: int = 1500) -> None:
        self.cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": ms, "downloadThroughput": -1, "uploadThroughput": -1})


@pytest.fixture()
def person(browser, live) -> Iterator[Person]:
    who = Person(browser, live)
    yield who
    who.context.close()


def _expect():
    from playwright.sync_api import expect
    return expect


def test_approvals_says_it_is_loading_and_never_claims_an_empty_inbox_first(person):
    person.open_demo()
    person.slow_wire()
    person.rail("Approvals")
    _expect()(person.page.get_by_text("Loading the approvals.")).to_be_visible()
    _expect()(person.page.get_by_text("Inbox empty")).to_have_count(0)


def test_a_failed_approvals_read_shows_the_error_and_no_empty_inbox(person):
    person.open_demo()
    person.rail("Approvals")
    person.page.get_by_role("button", name="Refresh").wait_for()
    person.context.set_offline(True)
    person.page.get_by_role("button", name="Refresh").click()
    _expect()(person.page.locator(".err").first).to_be_visible()
    _expect()(person.page.get_by_text("Inbox empty")).to_have_count(0)


def test_audit_says_it_is_loading_before_it_draws_a_table(person):
    person.open_demo()
    person.slow_wire()
    person.rail("Audit")
    _expect()(person.page.get_by_text("Loading the audit log.")).to_be_visible()
    _expect()(person.page.locator("table")).to_have_count(0)


def test_a_failed_audit_read_shows_the_error_and_no_empty_log(person):
    person.open_demo()
    person.rail("Audit")
    person.page.get_by_role("button", name="Refresh").wait_for()
    person.context.set_offline(True)
    person.page.get_by_role("button", name="Refresh").click()
    _expect()(person.page.locator(".err").first).to_be_visible()
    _expect()(person.page.get_by_text("No actions are recorded yet.")).to_have_count(0)


def test_an_audit_log_with_no_rows_says_so(person):
    """This data root has run nothing, so the demo company's log really has no rows."""
    person.open_demo()
    person.rail("Audit")
    _expect()(person.page.get_by_text("No actions are recorded yet.")).to_be_visible()
    _expect()(person.page.locator("table")).to_have_count(0)


def test_settings_and_usage_say_they_are_loading(person):
    person.slow_wire()
    person.page.locator("nav.rail").get_by_role("button", name="Settings").click()
    _expect()(person.page.get_by_text("Loading your org's settings.")).to_be_visible()
    person.page.get_by_role("tab", name="Usage").click()
    _expect()(person.page.get_by_text("Loading your usage.")).to_be_visible()
    _expect()(person.page.get_by_text("Your plan is")).to_be_visible()


def test_ci_runs_this_file_where_a_skip_fails_the_job():
    demo = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["demo"]
    step = next(s["run"] for s in demo["steps"] if Path(__file__).name in s.get("run", ""))
    assert "! grep -q SKIPPED" in step


def test_the_console_copy_has_no_em_dash():
    for path in (ROOT / "console" / "src").rglob("*.jsx"):
        assert "—" not in path.read_text(), path
