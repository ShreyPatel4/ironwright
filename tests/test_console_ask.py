"""Ask in a real headless browser, against a real platform app, with no route stubbed.

Real here: the built console, the sessions, the gate, the job queue and a worker on its own
thread. The retail run happens once on a dev open server, because a tenant cannot run a demo
pack. That is the only step the browser does not take or the routes do not serve.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import cookie_session
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from ironwright.api import harden
from ironwright.jobs.worker import Worker
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import db, service

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
JOIN = HERE / "fixtures" / "ingest" / "join"
PASSWORD = "correct-horse-9"
DEMO = "retail"
# The view, and a button a tenant would only get a 403 from on a demo pack.
WRITES = [("Pipeline", "Run all stages"), ("Boards", "Create with starter tiles"),
          ("BI embed", "Mirror lake to postgres"), ("BI embed", "Save"), ("Agents", "Export"),
          ("Training", "Evaluate"), ("Data explorer", "Run models on postgres and compare")]


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except sync_api.Error as e:
            pytest.skip(f"no chromium for playwright, so this was not proved: {e}")
        yield chromium
        chromium.close()


@pytest.fixture(scope="module")
def console_dist() -> None:
    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm not found: the console cannot be built, so this was not proved")
    console = ROOT / "console"
    if not (console / "node_modules").exists():
        subprocess.run([npm, "ci"], cwd=console, check=True, capture_output=True)
    subprocess.run([npm, "run", "build"], cwd=console, check=True, capture_output=True)


@pytest.fixture(scope="module")
def demo_root(tmp_path_factory) -> Path:
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("console-ask")
    mp.setenv("IRONWRIGHT_DATA", str(root))
    mp.setenv("IRONWRIGHT_DEV_OPEN", "1")
    mp.delenv("IRONWRIGHT_DB_URL", raising=False)
    with TestClient(create_platform_app()) as c:
        assert c.post(f"/workspaces/{DEMO}/runs", json={"seed": 7}).status_code == 200
    mp.undo()
    db.dispose_current()
    return root


def serve(monkeypatch, root: Path, label: str) -> Iterator[str]:
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(root))
    monkeypatch.setenv("IRONWRIGHT_DB_URL", f"sqlite:///{root}/meta-{label}.sqlite")
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "0")
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
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    stop.set()
    work.join(30)
    server.should_exit = True
    thread.join(10)
    db.dispose_current()


@pytest.fixture()
def hosted(console_dist, demo_root, monkeypatch, request):
    yield from serve(monkeypatch, demo_root, f"hosted-{abs(hash(request.node.name))}")


def signup(base: str, label: str) -> dict:
    r = httpx.post(f"{base}/auth/signup", json={"email": f"{label}@example.com", "password": PASSWORD,
                                                "org_name": f"Org {label}"}, timeout=30)
    assert r.status_code == 201, r.text
    return r.json()


def open_console(browser, base: str, session: dict):
    context = browser.new_context()
    context.set_default_timeout(20_000)
    cookie_session.place(context, base, session)
    page = context.new_page()
    page.goto(f"{base}/console/")
    return page


def open_workspace(page, title: str) -> None:
    page.locator(".card", has_text=title).first.click()


def view(page, name: str) -> None:
    page.locator("nav.rail").get_by_role("button", name=name, exact=True).click()
    page.get_by_role("heading", name=name, exact=False).first.wait_for()


def first_example(page):
    return page.get_by_text("try one").locator("..").get_by_role("button").first


def expect():
    from playwright.sync_api import expect as e
    return e


def own_workspace(base: str, who: dict, slug: str = "acme") -> str:
    """Upload, profile, create, run, propose, accept: the routes the wizard calls, with
    the worker doing the jobs. Returns the workspace's title on its card."""
    head = {"Authorization": f"Bearer {who['token']}"}

    def done(job_id: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            got = httpx.get(f"{base}/jobs/{job_id}", headers=head, timeout=30).json()
            if got["state"] in ("succeeded", "failed"):
                assert got["state"] == "succeeded", got
                return
            time.sleep(0.1)
        raise AssertionError(f"job {job_id} did not finish")

    files = [("files", (f.name, f.read_bytes(), "text/csv")) for f in sorted(JOIN.glob("*.csv"))]
    upload_id = httpx.post(f"{base}/uploads", headers=head, files=files, timeout=30).json()["id"]
    done(httpx.post(f"{base}/uploads/{upload_id}/profile", headers=head, timeout=30).json()["id"])
    spec = httpx.get(f"{base}/uploads/{upload_id}/proposal", headers=head, timeout=30).json()["workspace"]
    made = httpx.post(f"{base}/workspaces", headers=head, timeout=30,
                      json={"from_upload": upload_id, "spec": spec, "slug": slug})
    assert made.status_code == 201, made.text
    for kind in ("run", "ontology_propose"):
        done(httpx.post(f"{base}/jobs", headers=head, timeout=30,
                        json={"kind": kind, "workspace": slug, "params": {}}).json()["id"])
    proposal = httpx.get(f"{base}/workspaces/{slug}/ontology/proposal", headers=head, timeout=30).json()
    accepted = httpx.post(f"{base}/workspaces/{slug}/ontology/accept", headers=head, timeout=30,
                          json={"proposal_id": proposal["id"], "edits": []})
    assert accepted.status_code == 200, accepted.text
    done(accepted.json()["job_id"])
    return spec["title"]


def test_a_signed_up_org_asks_a_demo_in_the_console(browser, hosted):
    page = open_console(browser, hosted, signup(hosted, "alpha"))
    open_workspace(page, load_pack(DEMO).title)
    view(page, "Ask")
    first_example(page).click()
    expect()(page.locator(".badge.ok", has_text="answered")).to_be_visible()
    expect()(page.locator("pre.mono")).to_contain_text("[ev:")
    page.get_by_role("button", name="Replay this answer").click()
    expect()(page.locator(".badge", has_text="replay matches")).to_be_visible()


def test_a_second_org_sees_none_of_it_in_the_console(browser, hosted):
    a, b = signup(hosted, "alpha"), signup(hosted, "beta")
    page = open_console(browser, hosted, a)
    open_workspace(page, load_pack(DEMO).title)
    view(page, "Ask")
    first_example(page).click()
    expect()(page.get_by_text("This workspace so far: 1 asked")).to_be_visible()
    other = open_console(browser, hosted, b)
    open_workspace(other, load_pack(DEMO).title)
    view(other, "Ask")
    expect()(first_example(other)).to_be_visible()
    expect()(other.get_by_text("This workspace so far")).to_have_count(0)


@pytest.mark.parametrize(("name", "button"), WRITES)
def test_a_demo_offers_no_write_it_would_refuse(browser, hosted, name, button):
    page = open_console(browser, hosted, signup(hosted, "alpha"))
    open_workspace(page, load_pack(DEMO).title)
    view(page, name)
    expect()(page.get_by_text("This demo is read only")).to_be_visible()
    expect()(page.get_by_role("button", name=button, exact=True)).to_have_count(0)


def test_the_operator_of_the_dev_org_still_gets_run(browser, hosted):
    """auth-set seeds this account into the dev org, which owns the packs on its own server."""
    service.seed_operator("operator@example.com", PASSWORD, time.time())
    r = httpx.post(f"{hosted}/auth/login", json={"email": "operator@example.com", "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    page = open_console(browser, hosted, r.json())
    open_workspace(page, load_pack(DEMO).title)
    view(page, "Pipeline")
    expect()(page.get_by_role("button", name="Run all stages")).to_be_visible()
    expect()(page.get_by_text("This demo is read only")).to_have_count(0)


def test_a_customer_asks_with_their_own_id_and_reads_a_plain_answer(browser, hosted):
    who = signup(hosted, "alpha")
    title = own_workspace(hosted, who)
    page = open_console(browser, hosted, who)
    open_workspace(page, title)
    view(page, "Ask")
    page.get_by_label("your question").fill("show me customer 1001")
    page.locator("main").get_by_role("button", name="Ask", exact=True).click()
    answer = page.locator("pre.mono")
    expect()(answer).to_contain_text("Ann Lee")
    expect()(answer).to_contain_text("Signup date: 2021-03-04")
    expect()(answer).to_contain_text("[ev:")
    for internal in ("_id", "_title", "00:00:00", "CUS-"):
        expect()(answer).not_to_contain_text(internal)


def test_a_workspace_of_your_own_does_not_offer_the_demo_run_button(browser, hosted):
    """POST /runs is for packs. A tenant workspace runs through the job queue."""
    who = signup(hosted, "alpha")
    title = own_workspace(hosted, who)
    page = open_console(browser, hosted, who)
    open_workspace(page, title)
    view(page, "Pipeline")
    expect()(page.get_by_role("button", name="Run all stages")).to_have_count(0)
