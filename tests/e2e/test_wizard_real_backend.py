"""The wizard in a real browser against the real backend, the way I run it by hand.

`ironwright platform` and `ironwright worker` run as their own processes on a throwaway
data root. There is no route stub, no fake api and no adapter in this file, and a test
below holds it to that. tests/test_console_onboarding.py covers the wizard's edge cases.
This file answers one question: does the journey work against what the server really does?

CI runs this in the demo job, where a skip fails the job.

Two walks, because the two fixtures prove different things. The clean CSVs have clear keys
and a clear join as they stand. The messy tenant_ws CSVs need the silver draft to get there.
Both go all the way to a cited answer.
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
TWO_CSVS = sorted((ROOT / "tests" / "fixtures" / "tenant_ws" / "uploads" / "fixture").glob("*.csv"))
# Clear keys and a clear join, so the rules can propose an ontology over them as they stand.
CLEAN_CSVS = sorted((ROOT / "tests" / "fixtures" / "ingest" / "join").glob("*.csv"))
PASSWORD = "correct horse battery"
SLOW = 120_000  # a real pipeline run in another process sits behind these waits


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
    root = tmp_path_factory.mktemp("wizard-real")
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
    """The whole point of the file. A stub here would hide the next route mismatch."""
    source = Path(__file__).read_text()
    for banned in ("page" + ".route", "context" + ".route", "add_api" + "_route", "_install" + "_gaps"):
        assert banned not in source, banned


class Walk:
    """One person in one fresh browser, with every refusal the page saw written down."""

    def __init__(self, browser, base: str, email: str) -> None:
        self.context = browser.new_context()
        self.context.set_default_timeout(20_000)
        self.page = self.context.new_page()
        self.base, self.email = base, email
        self.errors: list[str] = []
        self.refused: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("response", lambda r: self.refused.append(f"{r.status} {r.request.method} {r.url}")
                     if r.status >= 400 else None)

    def to_map(self, files: list[Path]) -> None:
        from playwright.sync_api import expect
        page = self.page
        page.goto(f"{self.base}/console/")
        page.get_by_role("button", name="Sign up").click()
        page.get_by_label("org name").fill(f"{self.email} org")
        page.get_by_label("email").fill(f"{self.email}@acme.test")
        page.get_by_label("password").fill(PASSWORD)
        page.get_by_role("button", name="Create the org").click()

        # Upload, which starts the profile job.
        expect(page.get_by_role("heading", name="Upload your files")).to_be_visible()
        page.get_by_label("Choose files").set_input_files([str(f) for f in files])
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/profile")) as started:
            page.get_by_role("button", name="Upload and profile").click()
        assert started.value.status == 202
        expect(page.get_by_role("heading", name="Profile")).to_be_visible()

        # Profile: the job the upload started, polled to its end, shows the real profile.
        page.get_by_role("button", name="Continue to Map").click(timeout=SLOW)
        expect(page.get_by_role("heading", name="Map")).to_be_visible()

    def to_proposal(self, slug: str) -> dict:
        from playwright.sync_api import expect
        page = self.page
        page.get_by_label("workspace name").fill(slug)
        page.get_by_role("button", name="Create the workspace").click()
        expect(page.get_by_role("heading", name="Run")).to_be_visible()
        page.get_by_role("button", name="Run the pipeline").click()
        page.get_by_role("button", name="Continue to Ontology").click(timeout=SLOW)
        expect(page.get_by_role("heading", name="Ontology")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/ontology/proposal"), timeout=SLOW) as read:
            page.get_by_role("button", name="Propose an ontology").click()
        return read.value.json()

    def accept_and_ask(self) -> None:
        """A person accepts, which queues the run that builds it. Then the question comes
        from the org's own records, and the answer cites what it gathered."""
        from playwright.sync_api import expect
        page = self.page
        page.get_by_role("button", name="Accept this ontology").click(timeout=SLOW)
        expect(page.get_by_role("heading", name="Ask")).to_be_visible(timeout=SLOW)
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/ask")) as asked:
            page.get_by_label("example questions").get_by_role("button").first.click(timeout=SLOW)
        answer = asked.value.json()
        assert answer["status"] == "answered", json.dumps(answer, indent=1)
        gathered = {c["evidence_id"] for c in answer["calls"]}
        assert answer["citations"] and set(answer["citations"]) <= gathered, answer
        expect(page.get_by_label("answer")).to_contain_text(f"[ev:{answer['citations'][0]}]")
        expect(page.get_by_label(f"evidence {answer['citations'][0]}")).to_be_visible()

    def assert_nothing_was_swallowed(self) -> None:
        """A swallowed 404 or 405 is how this journey broke without any page error."""
        assert self.refused == []
        assert self.errors == []


def test_sign_up_to_a_cited_answer_over_clean_csvs(browser, live):
    walk = Walk(browser, live, "clean")
    walk.to_map(CLEAN_CSVS)
    proposal = walk.to_proposal("acme-sales")
    assert proposal["objects"], proposal
    walk.accept_and_ask()
    walk.assert_nothing_was_swallowed()
    walk.context.close()
    resume_in_a_new_browser(browser, live, "clean")


def resume_in_a_new_browser(browser, base: str, email: str) -> None:
    """Nothing about the journey lives in the browser, so only the server can place this:
    the upload list, the label on its job and the tenant ontology read all have to work."""
    from playwright.sync_api import expect
    walk = Walk(browser, base, email)
    page = walk.page
    page.goto(f"{base}/console/")
    page.get_by_label("email").fill(f"{email}@acme.test")
    page.get_by_label("password").fill(PASSWORD)
    page.get_by_role("button", name="Enter the console").click()
    expect(page.get_by_role("heading", name="Ask")).to_be_visible()
    page.get_by_role("button", name="Upload", exact=False).first.click()
    expect(page.get_by_text("newest, and the one the next steps use")).to_be_visible()
    for f in CLEAN_CSVS:
        expect(page.get_by_text(f.name, exact=True)).to_be_visible()
    walk.assert_nothing_was_swallowed()
    walk.context.close()


def test_the_messy_csvs_reach_the_ontology_step_on_their_first_run(browser, live):
    """The walk that used to stop at Upload (a 404), then Profile (a 405), then Run (drift
    on its own spec). The draft holds no column_6, so nobody has to know to untick it."""
    walk = Walk(browser, live, "messy")
    walk.to_map(TWO_CSVS)
    assert walk.page.get_by_label("keep claims.column_6").count() == 0
    walk.to_proposal("acme-claims")
    walk.assert_nothing_was_swallowed()
    walk.context.close()


def test_sign_up_to_a_cited_answer_over_the_messy_csvs(browser, live):
    walk = Walk(browser, live, "messy-ask")
    walk.to_map(TWO_CSVS)
    proposal = walk.to_proposal("acme-claims")
    try:
        assert proposal["objects"], proposal["skipped"]
        walk.accept_and_ask()
        walk.assert_nothing_was_swallowed()
    finally:
        walk.context.close()
