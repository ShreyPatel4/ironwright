"""Refresh and a second dataset, in a real browser against the real backend.

`ironwright platform` and `ironwright worker` run as their own processes on a throwaway data
root. There is no route stub here, and a test below holds this file to that. Setup goes over
HTTP on purpose: this file proves the Sources screen, and the wizard has its own walk.

CI runs this in the demo job, where a skip fails the job.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery"
SLOW = 120_000  # a real pipeline run in another process sits behind these waits
SLUG = "acme-orders"
V1 = "order_id,customer,amount\n1001,Ann,10.50\n1002,Bo,11.50\n1003,Cy,12.50\n"
V2_NEW_COLUMN = "order_id,customer,amount,region\n1001,Ann,10.50,N\n1002,Bo,11.50,S\n1003,Cy,12.50,S\n1004,Di,13.50,N\n"
V3_KEY_RENAMED = "order_no,customer,amount,region\n1001,Ann,10.50,N\n1002,Bo,11.50,S\n1003,Cy,12.50,S\n1004,Di,13.50,N\n"
REGIONS = "region_id,region_name\nN,North\nS,South\n"


@pytest.fixture(scope="module")
def console_dist() -> None:
    """console/dist is gitignored, so this builds what the server is about to serve."""
    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm not found: the console cannot be built, so this was not proved")
    console = ROOT / "console"
    if not (console / "node_modules").exists():
        subprocess.run([npm, "ci"], cwd=console, check=True, capture_output=True)
    subprocess.run([npm, "run", "build"], cwd=console, check=True, capture_output=True)


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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(base: str, server: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert server.poll() is None, f"the platform exited early:\n{log.read_text()}"
        try:
            with urllib.request.urlopen(f"{base}/auth/status", timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise AssertionError(f"the platform did not come up:\n{log.read_text()}")


@pytest.fixture(scope="module")
def live(console_dist, tmp_path_factory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("refresh-real")
    env = {k: v for k, v in os.environ.items() if k not in ("IRONWRIGHT_DEV_OPEN", "IRONWRIGHT_DB_URL")}
    env["IRONWRIGHT_DATA"] = str(root)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    cli = [sys.executable, "-m", "ironwright.cli"]
    server_log, worker_log = root / "platform.log", root / "worker.log"
    with server_log.open("w") as out, worker_log.open("w") as work:
        server = subprocess.Popen([*cli, "platform", "--port", str(port)], cwd=root, env=env,
                                  stdout=out, stderr=subprocess.STDOUT)
        worker = subprocess.Popen([*cli, "worker"], cwd=root, env=env, stdout=work, stderr=subprocess.STDOUT)
        try:
            _wait_until_up(base, server, server_log)
            yield base
        finally:
            for process in (worker, server):
                process.terminate()
            for process in (worker, server):
                try:
                    process.wait(15)
                except subprocess.TimeoutExpired:
                    process.kill()


def test_ci_runs_this_file_and_a_skip_fails_the_job():
    demo = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["demo"]
    commands = "\n".join(step.get("run", "") for step in demo["steps"])
    assert "playwright install" in commands
    assert Path(__file__).name in commands and "! grep -q SKIPPED" in commands
    assert demo["defaults"]["run"]["shell"] == "bash"


def test_this_file_stubs_nothing():
    source = Path(__file__).read_text()
    for banned in ("page" + ".route", "context" + ".route", "add_api" + "_route", "_install" + "_gaps"):
        assert banned not in source, banned


class Org:
    """One person in one fresh browser, with a workspace that has already run once."""

    def __init__(self, browser, base: str, name: str, tmp: Path) -> None:
        self.context = browser.new_context()
        self.context.set_default_timeout(20_000)
        self.page = self.context.new_page()
        self.base, self.tmp = base, tmp
        self.errors: list[str] = []
        self.refused: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("response", lambda r: self.refused.append(f"{r.status} {r.request.method} {r.url}")
                     if r.status >= 400 else None)
        self.page.goto(f"{base}/console/")
        self.page.get_by_role("button", name="Sign up").click()
        self.page.get_by_label("org name").fill(f"{name} org")
        self.page.get_by_label("email").fill(f"{name}@acme.test")
        self.page.get_by_label("password").fill(PASSWORD)
        self.page.get_by_role("button", name="Create the org").click()
        self.page.get_by_role("button", name="Go to the console").wait_for()
        self.session = json.loads(self.page.evaluate("localStorage.getItem('iw.session')"))

    def api(self, method: str, path: str, **kw) -> dict:
        r = self.context.request.fetch(f"{self.base}{path}", method=method, timeout=SLOW,
                                       headers={"authorization": f"Bearer {self.session['token']}"}, **kw)
        assert r.ok, f"{method} {path}: {r.status} {r.text()}"
        return r.json()

    def file(self, name: str, text: str) -> Path:
        folder = self.tmp / str(time.monotonic_ns())
        folder.mkdir()
        path = folder / name
        path.write_text(text)
        return path

    def seed(self) -> None:
        made = self.api("POST", "/uploads", multipart={
            "files": {"name": "orders.csv", "mimeType": "text/csv", "buffer": V1.encode()}})
        columns = [{"name": "order_id", "type": "int", "key": True}, {"name": "customer", "type": "string"},
                   {"name": "amount", "type": "decimal(12,2)"}]
        self.api("POST", "/workspaces", data={
            "from_upload": made["id"], "slug": SLUG,
            "models": {"silver": {"01_orders.sql": "select order_id, customer, amount from bronze_orders\n"}},
            "spec": {"kind": "workspace", "version": 1, "title": "Acme orders", "quality": [], "reconcile": [],
                     "sources": [{"name": "orders", "upload": f"{made['id']}/orders.csv", "format": "csv",
                                  "layout": {"delimiter": ",", "header": True, "encoding": "utf-8"},
                                  "columns": columns}]}})
        job = self.api("POST", "/jobs", data={"kind": "run", "workspace": SLUG, "params": {}})
        deadline = time.monotonic() + SLOW / 1000
        while (state := self.api("GET", f"/jobs/{job['id']}")["state"]) not in ("succeeded", "failed", "cancelled"):
            assert time.monotonic() < deadline, "the seed run never finished"
            time.sleep(0.3)
        assert state == "succeeded"

    def open_sources(self) -> None:
        from playwright.sync_api import expect
        page = self.page
        page.get_by_role("button", name="Go to the console").click()
        page.get_by_role("button", name="Acme orders").click()
        page.get_by_role("navigation").get_by_role("button", name="Sources").click()
        expect(page.get_by_role("heading", name="Your data sources")).to_be_visible()
        # What came before this screen has its own tickets. From here on, nothing may be refused.
        self.refused.clear()

    def run(self, expected: str = "succeeded") -> None:
        from playwright.sync_api import expect
        panel = self.page.get_by_label("refresh run")
        self.page.get_by_role("button", name="Run the pipeline").click()
        expect(panel.get_by_text(expected, exact=True)).to_be_visible(timeout=SLOW)

    def versions(self, source: str) -> list[dict]:
        return self.api("GET", f"/workspaces/{SLUG}/sources/{source}/versions")["versions"]

    def assert_nothing_was_swallowed(self) -> None:
        assert self.refused == []
        assert self.errors == []


@pytest.fixture()
def org(browser, live, tmp_path, request) -> Iterator[Org]:
    made = Org(browser, live, request.node.name.replace("_", "-")[:24], tmp_path)
    made.seed()
    yield made
    made.context.close()


def test_a_new_version_shows_its_drift_and_a_spec_update_clears_a_block(org):
    from playwright.sync_api import expect
    page = org.page
    org.open_sources()
    orders = page.get_by_label("source orders")
    expect(orders.get_by_text("version 1", exact=True)).to_be_visible()

    # A column appears. That is information, and the run goes on.
    orders.get_by_label("New version of orders").set_input_files(str(org.file("orders.csv", V2_NEW_COLUMN)))
    orders.get_by_role("button", name="Upload new version").click()
    drift = page.get_by_label("drift for orders")
    expect(drift.get_by_text("info", exact=True)).to_be_visible()
    expect(drift).to_contain_text("The file has a new column region")
    org.run()
    assert [v["version"] for v in org.versions("orders")] == [1, 2]
    assert org.versions("orders")[1]["runs"][-1]["rows"] == 4
    expect(orders.get_by_text("version 2", exact=True)).to_be_visible()

    # The key is renamed. The run is closed until a person decides.
    orders.get_by_label("New version of orders").set_input_files(str(org.file("orders.csv", V3_KEY_RENAMED)))
    orders.get_by_role("button", name="Upload new version").click()
    expect(drift.get_by_text("block", exact=True)).to_be_visible()
    expect(drift).to_contain_text("The key column order_id is gone.")
    expect(page.get_by_role("button", name="Run the pipeline")).to_be_disabled()

    spec = page.get_by_label("spec for orders")
    spec.get_by_label("keep order_id").uncheck()
    spec.get_by_label("keep order_no").check()
    spec.get_by_label("key order_no").check()
    expect(spec).to_contain_text("reads null for rows from this version on")
    spec.get_by_role("button", name="Update the spec").click()
    expect(drift.get_by_text("block", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Run the pipeline")).to_be_enabled()
    org.run()
    assert org.versions("orders")[-1]["version"] == 3
    assert org.versions("orders")[-1]["runs"][-1]["rows"] == 4
    assert {"name": "order_no", "type": "string", "key": True} in org.versions("orders")[-1]["source"]["columns"]
    org.assert_nothing_was_swallowed()


def test_the_run_is_refused_by_the_server_too_and_the_page_says_why(org):
    """The disabled button is manners. The server is the wall."""
    from playwright.sync_api import expect
    page = org.page
    org.open_sources()
    orders = page.get_by_label("source orders")
    orders.get_by_label("New version of orders").set_input_files(str(org.file("orders.csv", V3_KEY_RENAMED)))
    orders.get_by_role("button", name="Upload new version").click()
    expect(page.get_by_label("drift for orders").get_by_text("block", exact=True)).to_be_visible()
    job = org.api("POST", "/jobs", data={"kind": "run", "workspace": SLUG, "params": {}})
    deadline = time.monotonic() + SLOW / 1000
    while (now := org.api("GET", f"/jobs/{job['id']}"))["state"] not in ("succeeded", "failed", "cancelled"):
        assert time.monotonic() < deadline
        time.sleep(0.3)
    assert now["state"] == "failed" and "order_id" in now["error"]
    org.assert_nothing_was_swallowed()


def test_another_dataset_joins_the_workspace_and_runs(org):
    from playwright.sync_api import expect
    page = org.page
    org.open_sources()
    add = page.get_by_label("add another dataset")
    add.get_by_label("Choose files for a new dataset").set_input_files(str(org.file("regions.csv", REGIONS)))
    add.get_by_role("button", name="Upload and profile").click()

    # The worker profiles it for real, and a person reads the draft before it joins.
    expect(add.get_by_label("name of the new source regions")).to_have_value("regions", timeout=SLOW)
    expect(add.get_by_text("region_name", exact=True)).to_be_visible()
    add.get_by_role("button", name="Add to this workspace").click()

    expect(page.get_by_label("source regions")).to_be_visible()
    expect(page.get_by_label("source orders")).to_be_visible()
    org.run()
    assert org.versions("regions")[0]["runs"][-1]["rows"] == 2
    assert org.versions("orders")[0]["runs"]
    org.assert_nothing_was_swallowed()


def test_a_viewer_reads_the_drift_and_gets_no_buttons(org, browser, live):
    from playwright.sync_api import expect
    invite = org.api("POST", f"/orgs/{org.session['org_slug']}/invites", data={"email": "v@acme.test", "role": "viewer"})
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{live}/console/#invite={invite['token']}")
    page.get_by_label("choose a password").fill(PASSWORD)
    page.locator("button[type=submit]").click()
    page.get_by_role("button", name="Go to the console").click()
    page.get_by_role("button", name="Acme orders").click()
    page.get_by_role("navigation").get_by_role("button", name="Sources").click()
    expect(page.get_by_label("source orders")).to_be_visible()
    expect(page.get_by_role("button", name="Upload new version")).to_have_count(0)
    expect(page.get_by_role("button", name="Run the pipeline")).to_have_count(0)
    expect(page.get_by_label("add another dataset")).to_have_count(0)
    context.close()
