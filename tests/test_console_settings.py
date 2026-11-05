"""The settings screens, in a real headless browser, against a real platform app.

Real here: the built console, the sessions, the app gate, the job queue, a worker on its
own thread (the API only queues, as hosted), the download, and the delete. The browser
drives every step a person would. Direct calls only set up what a screen does not own.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import uvicorn
from cryptography.fernet import Fernet

from ironwright.api import harden
from ironwright.config import tenant_dir
from ironwright.jobs.worker import Worker
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import db, usage

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery"
SECRET_KEY = "sk-settings-canary-" + "z" * 20 + "9f3k"


@pytest.fixture(scope="session")
def console_dist() -> None:
    """console/dist is gitignored, so the suite builds what it serves."""
    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm not found: the console cannot be built, so this was not proved")
    console = ROOT / "console"
    if not (console / "node_modules").exists():
        subprocess.run([npm, "ci"], cwd=console, check=True, capture_output=True)
    subprocess.run([npm, "run", "build"], cwd=console, check=True, capture_output=True)


@pytest.fixture(scope="module")
def browser():
    """Module scope, not session: sync_playwright keeps an event loop running on this
    thread, and any later test file's asyncio.run() would refuse to start."""
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except sync_api.Error as e:
            pytest.skip(f"no chromium for playwright, so this was not proved: {e}")
        yield chromium
        chromium.close()


class Stack:
    def __init__(self, base: str) -> None:
        self.base = base
        self.http = httpx.Client(base_url=base, timeout=30)

    def signup(self, email="owner@acme.test", org="Acme Claims") -> dict:
        r = self.http.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
        assert r.status_code == 201, r.text
        return r.json()

    def join(self, owner: dict, email: str, role: str) -> dict:
        r = self.http.post(f"/orgs/{owner['org_slug']}/invites", json={"email": email, "role": role},
                           headers={"Authorization": f"Bearer {owner['token']}"})
        assert r.status_code == 201, r.text
        r = self.http.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD})
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture()
def stack(console_dist, monkeypatch, tmp_path):
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "0")
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    server = uvicorn.Server(uvicorn.Config(harden(create_platform_app()), host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True, name="test-server")
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "the test server did not start"
        time.sleep(0.02)
    stop = threading.Event()
    worker = Worker(idle=0.1)

    def loop() -> None:
        while not stop.is_set():
            if worker.run_once() is None:
                stop.wait(0.1)

    work = threading.Thread(target=loop, daemon=True, name="test-worker")
    work.start()
    yield Stack(f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}")
    stop.set()
    work.join(30)
    server.should_exit = True
    thread.join(10)
    db.dispose_current()


def expect():
    from playwright.sync_api import expect as e
    return e


def open_settings(browser, stack, session):
    """A fresh context with this session in it, past the welcome tour."""
    context = browser.new_context(accept_downloads=True)
    context.set_default_timeout(15_000)
    context.add_init_script(
        f"localStorage.setItem('iw.session', {json.dumps(json.dumps(session))});"
        "localStorage.setItem('iw.onboarded', '1');")
    page = context.new_page()
    page.goto(f"{stack.base}/console/")
    page.get_by_role("button", name="Settings").click()
    expect()(page.get_by_role("heading", name="Settings")).to_be_visible()
    return page


def tab(page, name):
    page.get_by_role("tab", name=name).click()


def row(page, text):
    return page.get_by_role("row").filter(has_text=text)


def role_on_server(user_id: str, org_id: str) -> str | None:
    with usage.engine().connect() as con:
        return con.execute(sa.select(db.memberships.c.role).where(
            db.memberships.c.org_id == org_id, db.memberships.c.user_id == user_id)).scalar()


def test_settings_copy_has_no_em_dashes():
    for f in (ROOT / "console" / "src" / "views" / "settings").glob("*.jsx"):
        assert "—" not in f.read_text(), f.name


def test_an_owner_invites_someone_and_the_screen_hands_over_the_link(browser, stack):
    owner = stack.signup()
    page = open_settings(browser, stack, owner)
    tab(page, "Members")
    expect()(row(page, "owner@acme.test")).to_be_visible()
    page.get_by_label("Email to invite").fill("new@acme.test")
    page.get_by_label("Role for the invite").select_option("member")
    page.get_by_role("button", name="Invite").click()
    link = page.get_by_label("Invite link")
    expect()(link).to_have_value(re.compile(r"/console/#invite=.{20,}"))
    expect()(row(page, "new@acme.test")).to_contain_text("invited")
    token = link.input_value().split("#invite=")[1]
    assert stack.http.post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).status_code == 200
    page.get_by_role("button", name="Refresh").click()
    expect()(row(page, "new@acme.test").get_by_role("combobox")).to_have_value("member")


def test_an_owner_changes_a_role_and_removes_someone_and_the_server_agrees(browser, stack):
    owner = stack.signup()
    who = stack.join(owner, "m@acme.test", "member")
    page = open_settings(browser, stack, owner)
    tab(page, "Members")
    row(page, "m@acme.test").get_by_role("combobox").select_option("admin")
    expect()(page.get_by_role("status")).to_contain_text("m@acme.test is now admin")
    assert role_on_server(who["user_id"], owner["org_id"]) == "admin"
    row(page, "m@acme.test").get_by_role("button", name="Remove").click()
    expect()(row(page, "m@acme.test")).to_have_count(0)
    assert role_on_server(who["user_id"], owner["org_id"]) is None


def test_the_last_owner_is_told_why_and_stays(browser, stack):
    owner = stack.signup()
    page = open_settings(browser, stack, owner)
    tab(page, "Members")
    row(page, "owner@acme.test").get_by_role("button", name="Remove").click()
    expect()(page.locator(".err")).to_contain_text("last owner")
    row(page, "owner@acme.test").get_by_role("combobox").select_option("viewer")
    expect()(page.locator(".err")).to_contain_text("last owner")
    # The menu goes back to what the server still holds.
    expect()(row(page, "owner@acme.test").get_by_role("combobox")).to_have_value("owner")
    assert role_on_server(owner["user_id"], owner["org_id"]) == "owner"


def test_each_role_sees_only_the_tabs_it_can_use(browser, stack):
    owner = stack.signup()
    expected = {"admin": ["Members", "Usage", "Model key", "Audit"],
                "member": ["Usage"], "viewer": ["Usage"]}
    for role, tabs in expected.items():
        session = stack.join(owner, f"{role}@acme.test", role)
        page = open_settings(browser, stack, session)
        expect()(page.get_by_role("tab")).to_have_text(tabs)  # waits: the tabs follow /auth/me
        page.context.close()
    page = open_settings(browser, stack, owner)
    expect()(page.get_by_role("tab")).to_have_text(["Members", "Usage", "Model key", "Audit", "Your data"])


def test_the_usage_screen_shows_the_real_counts(browser, stack):
    owner = stack.signup()
    stack.join(owner, "m@acme.test", "member")
    page = open_settings(browser, stack, owner)
    tab(page, "Usage")
    expect()(page.get_by_text("free", exact=True)).to_be_visible()
    expect()(row(page, "Members")).to_contain_text("2")
    expect()(row(page, "Members")).to_contain_text("5")
    expect()(row(page, "Runs")).to_contain_text("0")
    expect()(row(page, "Storage")).to_contain_text("1.0 GB")


def test_a_saved_model_key_shows_only_its_last_four(browser, stack):
    owner = stack.signup()
    page = open_settings(browser, stack, owner)
    tab(page, "Model key")
    expect()(page.get_by_text("No key saved")).to_be_visible()
    page.get_by_label("Provider").select_option("anthropic")
    page.get_by_label("API key").fill(SECRET_KEY)
    page.get_by_role("button", name="Save key").click()
    expect()(page.get_by_text("ending in 9f3k")).to_be_visible()
    expect()(page.get_by_label("API key")).to_have_value("")
    assert SECRET_KEY not in page.content()
    assert SECRET_KEY[:-4] not in page.content()
    page.reload()
    page.get_by_role("button", name="Settings").click()
    tab(page, "Model key")
    expect()(page.get_by_text("ending in 9f3k")).to_be_visible()
    assert SECRET_KEY[:-4] not in page.content()
    page.get_by_role("button", name="Remove key").click()
    expect()(page.get_by_text("No key saved")).to_be_visible()


def test_a_refused_key_says_why_and_never_repeats_it(browser, stack):
    owner = stack.signup()
    page = open_settings(browser, stack, owner)
    tab(page, "Model key")
    page.get_by_label("API key").fill("too short")
    page.get_by_role("button", name="Save key").click()
    expect()(page.locator(".err")).to_contain_text("does not look like an API key")
    expect()(page.locator(".err")).not_to_contain_text("too short")


def test_the_audit_screen_verifies_filters_and_shows_a_break(browser, stack):
    owner = stack.signup()
    who = stack.join(owner, "m@acme.test", "member")
    headers = {"Authorization": f"Bearer {owner['token']}"}
    assert stack.http.put(f"/orgs/{owner['org_slug']}/members/{who['user_id']}", headers=headers,
                          json={"role": "viewer"}).status_code == 200
    page = open_settings(browser, stack, owner)
    tab(page, "Audit")
    expect()(page.get_by_test_id("chain-status")).to_contain_text("intact")
    expect()(page.get_by_test_id("chain-status")).to_contain_text("3 rows")
    rows = page.locator("tbody tr")
    expect()(rows).to_have_count(3)
    page.get_by_label("Filter by action").select_option("member.role")
    expect()(rows).to_have_count(1)
    expect()(rows.first).to_contain_text("member.role")
    expect()(rows.first).to_contain_text("owner@acme.test")
    page.get_by_label("Filter by action").select_option("")
    page.get_by_label("Filter by actor").select_option(who["principal"])
    expect()(rows).to_have_count(1)
    expect()(rows.first).to_contain_text("member.join")
    # A filter never softens the verdict: break the chain and look through the same filter.
    with usage.engine().begin() as con:
        con.execute(usage.org_audit.update().where(usage.org_audit.c.org_id == owner["org_id"],
                                                   usage.org_audit.c.action == "member.invite").values(detail="{}x"))
    page.get_by_role("button", name="Verify the chain").click()
    expect()(page.get_by_test_id("chain-status")).to_contain_text("BROKEN")


def _tenant_workspace(org_id: str) -> None:
    import yaml

    from ironwright import workspaces_tenant as wt
    from ironwright.config import ensure_tenant

    fixture = ROOT / "tests" / "fixtures" / "tenant_ws"
    shutil.copytree(fixture / "uploads" / "fixture", ensure_tenant(org_id) / "uploads" / "fixture")
    models = {layer: {f.name: f.read_text() for f in (fixture / "models" / layer).glob("*.sql")}
              for layer in ("silver", "gold")}
    wt.create_workspace(org_id, "claims", yaml.safe_load((fixture / "workspace.yaml").read_text()), models=models,
                        ontology=yaml.safe_load((fixture / "ontology.yaml").read_text()),
                        policies=yaml.safe_load((fixture / "policies.yaml").read_text()))
    assert wt.run_workspace(org_id, "claims")["passed"]


def test_an_owner_exports_through_the_worker_and_downloads_the_zip(browser, stack):
    owner = stack.signup()
    _tenant_workspace(owner["org_id"])
    page = open_settings(browser, stack, owner)
    tab(page, "Your data")
    page.get_by_role("button", name="Export my data").click()
    download_button = page.get_by_role("button", name="Download")
    expect()(download_button).to_be_visible(timeout=60_000)
    with page.expect_download() as got:
        download_button.click()
    assert got.value.suggested_filename == f"{owner['org_slug']}-export.zip"
    z = zipfile.ZipFile(io.BytesIO(Path(got.value.path()).read_bytes()))
    assert "workspaces/claims/gold/claims_by_policy.parquet" in z.namelist()
    assert "manifest.json" in z.namelist()


def test_a_delete_asks_for_the_slug_then_ends_at_the_door_with_nothing_left(browser, stack):
    owner = stack.signup()
    _tenant_workspace(owner["org_id"])
    page = open_settings(browser, stack, owner)
    tab(page, "Your data")
    button = page.get_by_role("button", name="Delete this org")
    expect()(button).to_be_disabled()
    page.get_by_label("Type the org slug to confirm").fill("not-the-slug")
    expect()(button).to_be_disabled()
    assert tenant_dir(owner["org_id"]).exists()
    page.get_by_label("Type the org slug to confirm").fill(owner["org_slug"])
    expect()(button).to_be_enabled()
    button.click()
    expect()(page.get_by_text("This org is being deleted")).to_be_visible()
    page.get_by_role("button", name="Back to sign in").click()
    expect()(page.get_by_role("button", name="Sign up")).to_be_visible()
    assert page.evaluate("localStorage.getItem('iw.session')") is None
    deadline = time.monotonic() + 30
    while tenant_dir(owner["org_id"]).exists() or _org_row(owner["org_id"]):
        assert time.monotonic() < deadline, "the worker did not finish the delete"
        time.sleep(0.1)
    assert stack.http.post("/auth/login", json={"email": "owner@acme.test", "password": PASSWORD}).status_code == 401


def _org_row(org_id: str):
    with usage.engine().connect() as con:
        return con.execute(sa.select(db.orgs).where(db.orgs.c.id == org_id)).first()
