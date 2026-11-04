"""The onboarding wizard, in a real headless browser, against a real platform app.

Real here: the built console, signup, login, invites, roles, the app gate, uploads,
the profiler and its proposal, the job queue, the worker, the log tail, T2's spec
validation and T2's run, down to the silver tables in the lake.

Not real yet: the routes in the GAP table below. They do not exist on main, so an
adapter stands in for each and names the ticket that owes the real one. An adapter
drops out by itself once a real route of that method and path exists, except those
marked `replaces`, which change a route that already exists. The ontology proposal
and every Ask answer are hand written stubs of W2's contract. So this suite proves
the wizard, and it does not prove a grounded answer over tenant data. W4's journey on
the hosted stack is what proves that.

Each test states the break it catches in its name.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import pytest
import uvicorn
from fastapi import Depends, HTTPException, Request

from ironwright import auth, verification
from ironwright import config as cfg
from ironwright import workspaces_tenant as wt
from ironwright.jobs import handlers
from ironwright.jobs import routes as job_routes
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.lake import Lakehouse
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import Ctx, db

# Module level, because FastAPI resolves these string annotations from here.
CtxDep = Annotated[Ctx, Depends(auth.ctx)]
JobBody = job_routes.JobBody

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
T2_FILES = sorted((FIXTURES / "tenant_ws" / "uploads" / "fixture").glob("*.csv"))
JOIN_FILES = sorted((FIXTURES / "ingest" / "join").glob("*.csv"))
PASSWORD = "correct horse battery"
SLOW = 60_000  # a real pipeline run sits behind some of these waits


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


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except sync_api.Error as e:
            pytest.skip(f"no chromium for playwright, so this was not proved: {e}")
        yield chromium
        chromium.close()


class Stack:
    """One platform app on a real socket, plus what the W2 stubs were sent."""

    def __init__(self) -> None:
        self.accepted: dict[str, dict[str, Any]] = {}
        self.base = ""
        self.stop_worker = threading.Event()
        self.worker_thread: threading.Thread | None = None

    def start_worker(self) -> None:
        worker = Worker(idle=0.1)

        def loop() -> None:
            while not self.stop_worker.is_set():
                if worker.run_once() is None:
                    self.stop_worker.wait(0.1)

        self.worker_thread = threading.Thread(target=loop, daemon=True, name="test-worker")
        self.worker_thread.start()


def _uploads_root(org_id: str) -> Path:
    return cfg.uploads_root(org_id)


def _proposal_stub(workspace: str) -> dict[str, Any]:
    return {
        "id": f"proposal-{workspace}",
        "objects": [
            {"name": "Policy", "table": "silver.policies", "key": "policy_no",
             "properties": ["policy_no", "policy_holder", "premium"], "confidence": 0.9,
             "reason": "This table has one row per policy_no."},
            {"name": "Claim", "table": "silver.claims", "key": "claim_id",
             "properties": ["claim_id", "policy_ref", "amount"], "confidence": 0.7,
             "reason": "Most rows have their own claim_id."},
        ],
        "links": [{"from": "Claim", "to": "Policy", "via": "policy_ref", "cardinality": "many_to_one",
                   "confidence": 0.8, "reason": "policy_ref values match policy_no."}],
    }


def _walk(routes: Any, prefix: str = "") -> Any:
    """Included routers nest in this FastAPI, as tests/test_isolation_routes.py knows."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _walk(included.routes, prefix + route.include_context.prefix)
        elif hasattr(route, "methods") and route.methods:
            yield prefix + route.path, route.methods, route.endpoint


def _install_gaps(app: Any, stack: Stack) -> None:
    """The GAP table: method, path, who owes the real route, and the stand-in."""
    real = {(m, path): endpoint for path, methods, endpoint in _walk(app.routes) for m in methods}
    list_real, create_job_real = real[("GET", "/workspaces")], real[("POST", "/jobs")]

    def missing() -> HTTPException:
        return HTTPException(404, "no such resource")

    def list_uploads(c: CtxDep) -> list[dict[str, Any]]:
        root = _uploads_root(c.org_id)
        found = sorted(root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [json.loads(p.read_text()) for p in found]

    def create_job(body: JobBody, c: CtxDep) -> dict[str, str]:
        # _check_workspace needs a workspace folder, and Profile comes before Map.
        if body.kind == "profile":
            return {"id": JobStore().enqueue(c.org_id, body.workspace, "profile", c.principal, body.params)}
        return create_job_real(body, c)

    def list_workspaces(c: CtxDep) -> list[dict[str, Any]]:
        own = []
        for w in wt.list_workspaces(c.org_id):
            pack = wt.load_workspace(c.org_id, w["slug"])
            own.append({"name": w["slug"], "kind": "workspace", "title": w["title"], "decision": None,
                        "verification": verification.derive(pack)})
        return [*list_real(c), *own]

    async def create_workspace(request: Request, c: CtxDep) -> dict[str, Any]:
        body = await request.json()
        try:
            made = wt.create_workspace(c.org_id, body["name"], body["spec"], models=body.get("models"))
        except wt.WorkspaceError as e:
            raise HTTPException(422, str(e)) from e
        return {"name": made["slug"], "kind": "workspace", "title": made["title"]}

    def ontology_proposal(name: str, c: CtxDep) -> dict[str, Any]:
        done = [j for j in JobStore().list(c.org_id, name)
                if j["kind"] == "ontology_propose" and j["state"] == "succeeded"]
        if not done:
            raise missing()
        return _proposal_stub(name)

    async def ontology_accept(name: str, request: Request, c: CtxDep) -> dict[str, Any]:
        stack.accepted[name] = await request.json()
        return {"ontology_version": 1}

    def ontology_read(name: str, c: CtxDep) -> dict[str, Any]:
        types = {"Policy": {}, "Claim": {}} if name in stack.accepted else {}
        return {"describe": {"object_types": types, "link_types": {}, "functions": {}, "actions": {}},
                "stats": {}}

    def ask_examples(name: str, c: CtxDep) -> list[dict[str, Any]]:
        if name not in stack.accepted:
            return []
        return [{"question": "Get one policy for policy P-100", "function": "get_policy"}]

    async def ask(name: str, request: Request, c: CtxDep) -> dict[str, Any]:
        question = (await request.json())["question"]
        entry = {"ts": 1.0, "question": question, "principal": c.principal}
        if "policy" not in question.lower():
            return {**entry, "status": "refused", "reason_code": "out_of_scope",
                    "reason": "No registered question matches this."}
        eid = "get_policy#1a2b3c4d"
        call = {"call_id": "c1", "function": "get_policy", "args": {"policy": "P-100"},
                "evidence_id": eid, "result_digest": "f" * 64}
        return {**entry, "status": "answered", "answer": f"Policy P-100 has a premium of 1000.\n[ev:{eid}]",
                "narrator": "offline-template", "citations": [eid], "calls": [call],
                "result": {"id": eid, "function": "get_policy", "args": {"policy": "P-100"},
                           "rows": [{"premium": 1000}], "summary": None},
                "verification": {"level": "L1", "meaning": "your files, structural checks only"}}

    gaps = [
        ("GET", "/uploads", "ask: no list route", list_uploads, False),
        ("POST", "/jobs", "ask: profile before a workspace exists", create_job, True),
        ("GET", "/workspaces", "ask: tenant workspaces are not listed", list_workspaces, True),
        ("POST", "/workspaces", "ask: wt.create_workspace has no route", create_workspace, True),
        ("GET", "/workspaces/{name}/ontology/proposal", "W2", ontology_proposal, False),
        ("POST", "/workspaces/{name}/ontology/accept", "W2", ontology_accept, False),
        ("GET", "/workspaces/{name}/ontology", "ask: _pack 404s a tenant workspace", ontology_read, True),
        ("GET", "/workspaces/{name}/ask/examples", "W2 and _pack", ask_examples, True),
        ("POST", "/workspaces/{name}/ask", "W2 and _pack", ask, True),
    ]
    for method, path, _owner, fn, replaces in gaps:
        if (method, path) in real and not replaces:
            continue
        app.add_api_route(path, fn, methods=[method])
        app.router.routes.insert(0, app.router.routes.pop())

    def propose_handler(ctx: Any, workspace: str, params: dict[str, Any], log: Any) -> dict[str, Any]:
        log("proposed 2 objects and 1 link")
        return {"objects": 2, "links": 1}

    handlers.HANDLERS["ontology_propose"] = propose_handler


@pytest.fixture()
def stack(console_dist, monkeypatch, tmp_path):
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "0")
    saved = dict(handlers.HANDLERS)
    s = Stack()
    app = create_platform_app()
    _install_gaps(app, s)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True, name="test-server")
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "the test server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    s.base = f"http://127.0.0.1:{port}"
    s.start_worker()
    yield s
    s.stop_worker.set()
    if s.worker_thread:
        s.worker_thread.join(30)
    server.should_exit = True
    thread.join(10)
    handlers.HANDLERS.clear()
    handlers.HANDLERS.update(saved)
    db.dispose_current()


@pytest.fixture()
def idle_stack(stack):
    """The same stack with no worker running, so a job stays queued."""
    stack.stop_worker.set()
    stack.worker_thread.join(30)
    stack.worker_thread = None
    return stack


def expect():
    from playwright.sync_api import expect as e
    return e


def new_page(browser, stack):
    """A fresh context each time: no localStorage survives, as on a new laptop."""
    context = browser.new_context()
    context.set_default_timeout(15_000)
    return context.new_page()


def sign_up(page, stack, email="owner@acme.test", org="Acme Claims"):
    page.goto(f"{stack.base}/console/")
    page.get_by_role("button", name="Sign up").click()
    page.get_by_label("org name").fill(org)
    page.get_by_label("email").fill(email)
    page.get_by_label("password").fill(PASSWORD)
    page.get_by_role("button", name="Create the org").click()


def log_in(page, stack, email="owner@acme.test"):
    page.goto(f"{stack.base}/console/")
    page.get_by_label("email").fill(email)
    page.get_by_label("password").fill(PASSWORD)
    page.get_by_role("button", name="Enter the console").click()


def session_of(page) -> dict[str, Any]:
    return json.loads(page.evaluate("localStorage.getItem('iw.session')"))


def upload(page, files=T2_FILES):
    expect()(page.get_by_role("heading", name="Upload your files")).to_be_visible()
    page.get_by_label("Choose files").set_input_files([str(f) for f in files])
    page.get_by_role("button", name="Upload and profile").click()
    expect()(page.get_by_role("heading", name="Profile")).to_be_visible()


def to_map(page):
    page.get_by_role("button", name="Continue to Map").click(timeout=SLOW)
    expect()(page.get_by_role("heading", name="Map")).to_be_visible()


def map_and_create(page, slug="acme-claims"):
    """The edits the T2 fixture needs and every kind of edit Map offers."""
    page.get_by_label("workspace name").fill(slug)
    page.get_by_label("keep claims.column_6").uncheck()
    page.get_by_label("rename policies.holder").fill("policy_holder")
    page.get_by_label("type policies.premium").select_option("double")
    page.get_by_label("key policies.policy_no").check()
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/workspaces")) as made:
        page.get_by_role("button", name="Create the workspace").click()
    expect()(page.get_by_role("heading", name="Run")).to_be_visible()
    return json.loads(made.value.post_data)


def run_pipeline(page):
    page.get_by_role("button", name="Run the pipeline").click()
    expect()(page.get_by_role("button", name="Continue to Ontology")).to_be_visible(timeout=SLOW)


def to_ontology(page):
    page.get_by_role("button", name="Continue to Ontology").click(timeout=SLOW)
    expect()(page.get_by_role("heading", name="Ontology")).to_be_visible()


def walk_to_ask(page, stack):
    sign_up(page, stack)
    upload(page)
    to_map(page)
    map_and_create(page)
    run_pipeline(page)
    to_ontology(page)
    page.get_by_role("button", name="Propose an ontology").click()
    page.get_by_label("object name Policy").fill("Insured", timeout=SLOW)
    with page.expect_request(lambda r: r.url.endswith("/ontology/accept")) as accepted:
        page.get_by_role("button", name="Accept this ontology").click()
    expect()(page.get_by_role("heading", name="Ask")).to_be_visible()
    return json.loads(accepted.value.post_data)


def test_the_whole_wizard_ends_on_an_answer_with_evidence(browser, stack):
    page = new_page(browser, stack)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    accepted = walk_to_ask(page, stack)

    # The accept carries the id of what was reviewed and only what changed.
    assert accepted["proposal_id"] == "proposal-acme-claims"
    assert accepted["edits"]["objects"] == [{"name": "Policy", "rename": "Insured"}]
    assert accepted["edits"]["links"] == []

    # A refusal is shown as a refusal, with its reason.
    page.get_by_label("your question").fill("what is the weather")
    page.get_by_role("button", name="Ask", exact=True).click()
    expect()(page.get_by_text("No registered question matches this.")).to_be_visible()

    page.get_by_role("button", name="Get one policy for policy P-100").click()
    expect()(page.get_by_label("answer")).to_contain_text("[ev:get_policy#1a2b3c4d]")
    expect()(page.get_by_label("evidence get_policy#1a2b3c4d")).to_be_visible()
    assert errors == []


def test_map_edits_reach_the_real_lake(browser, stack):
    """Rename and drop have to change the silver table, not just the form."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    body = map_and_create(page)

    by_name = {s["name"]: s for s in body["spec"]["sources"]}
    assert "column_6" not in [c["name"] for c in by_name["claims"]["columns"]]
    policy_cols = {c["name"]: c for c in by_name["policies"]["columns"]}
    assert policy_cols["policy_no"]["key"] is True
    assert policy_cols["premium"]["type"] == "double"
    # Contract 3 requires the file's own name in the spec: the rename lives in silver.
    assert "holder" in policy_cols
    assert "policy_holder" not in policy_cols

    run_pipeline(page)
    pack = wt.load_workspace(session_of(page)["org_id"], "acme-claims")
    con = Lakehouse(pack).connect()
    rows = con.execute("select policy_holder, premium from silver_policies order by premium").fetchall()
    assert rows[0][1] == 99.5  # a real number now, cast from the bronze string
    assert isinstance(rows[0][0], str)
    assert "holder" not in [d[0] for d in con.execute("select * from silver_policies limit 0").description]
    assert "column_6" not in [d[0] for d in con.execute("select * from silver_claims limit 0").description]


def test_profile_shows_types_nulls_keys_and_joins_with_their_reasons(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page, JOIN_FILES)
    expect()(page.get_by_text("Unique and never null in every row read. The name also reads like a key.").first
             ).to_be_visible(timeout=SLOW)
    expect()(page.get_by_text("customers.csv.customer_id")).to_be_visible()
    expect()(page.get_by_text("orders.csv.cust_no")).to_be_visible()
    expect()(page.get_by_text("The names are similar and 4 distinct values match after dropping "
                              "leading zeros. Values on the many side with no match: 1.")).to_be_visible()
    row = page.get_by_role("row", name="signup_date")
    expect()(row).to_contain_text("date")
    expect()(row).to_contain_text("0%")


def test_profile_says_so_when_it_found_no_keys_or_joins(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    expect()(page.get_by_text("No join candidates between these files.")).to_be_visible(timeout=SLOW)
    expect()(page.get_by_text("No candidate key in this file.").first).to_be_visible()


@pytest.mark.parametrize("stage, heading", [("profiled", "Map"), ("created", "Run"), ("ran", "Ontology")])
def test_a_new_browser_resumes_at_the_step_the_server_says(browser, stack, stage, heading):
    """A fresh context has no localStorage, so only server state can place it."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    if stage != "profiled":
        map_and_create(page)
    if stage == "ran":
        run_pipeline(page)

    again = new_page(browser, stack)
    log_in(again, stack)
    expect()(again.get_by_role("heading", name=heading)).to_be_visible()


def test_a_new_browser_after_accept_resumes_at_ask(browser, stack):
    walk_to_ask(new_page(browser, stack), stack)
    again = new_page(browser, stack)
    log_in(again, stack)
    expect()(again.get_by_role("heading", name="Ask")).to_be_visible()


def test_a_job_with_no_worker_says_it_is_queued(browser, idle_stack):
    page = new_page(browser, idle_stack)
    sign_up(page, idle_stack)
    upload(page)
    expect()(page.get_by_text("queued", exact=False).first).to_be_visible()
    expect()(page.get_by_text("waiting for a worker", exact=False)).to_be_visible()
    expect()(page.get_by_role("button", name="Continue to Map")).to_have_count(0)


def test_a_failed_run_shows_why_and_cannot_continue(browser, stack):
    """T3's draft for this fixture names a column the reader cannot find, so the
    run really fails unless the user drops it on Map. Here the user does not."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    page.get_by_label("workspace name").fill("as-proposed")
    page.get_by_role("button", name="Create the workspace").click()
    page.get_by_role("button", name="Run the pipeline").click()
    expect()(page.get_by_text("the file has no column column_6").first).to_be_visible(timeout=SLOW)
    expect()(page.get_by_role("button", name="Run again")).to_be_visible()
    expect()(page.get_by_role("button", name="Continue to Ontology")).to_have_count(0)


def test_a_run_whose_evals_failed_cannot_continue(browser, stack):
    """The job succeeded and an eval did not. Evals gate everything, so the wizard
    has to read the verdict, not just the job state."""
    def failed_evals(route):
        reply = route.fetch()
        job = reply.json()
        if job.get("kind") == "run" and job.get("result"):
            job["result"]["evals"]["passed"] = False
        route.fulfill(response=reply, json=job)

    page = new_page(browser, stack)
    page.route("**/jobs/*", lambda route: failed_evals(route) if route.request.method == "GET" else route.continue_())
    sign_up(page, stack)
    upload(page)
    to_map(page)
    map_and_create(page)
    page.get_by_role("button", name="Run the pipeline").click()
    expect()(page.get_by_text("a check failed, so the next step stays closed")).to_be_visible(timeout=SLOW)
    expect()(page.get_by_role("button", name="Continue to Ontology")).to_have_count(0)
    expect()(page.get_by_role("button", name="Run again")).to_be_visible()


def test_a_queued_run_can_be_cancelled(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    map_and_create(page)
    stack.stop_worker.set()
    stack.worker_thread.join(30)
    page.get_by_role("button", name="Run the pipeline").click()
    expect()(page.get_by_text("waiting for a worker", exact=False)).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect()(page.get_by_text("cancelled", exact=True)).to_be_visible(timeout=SLOW)
    expect()(page.get_by_role("button", name="Run again")).to_be_visible()


def test_the_run_log_tails_real_lines(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    map_and_create(page)
    run_pipeline(page)
    expect()(page.get_by_label("job log")).to_contain_text("[acme-claims] started")
    expect()(page.get_by_label("job log")).to_contain_text("evals passed: True")


def test_map_refuses_a_bad_rename_before_it_asks_the_server(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    page.get_by_label("workspace name").fill("acme-claims")
    button = page.get_by_role("button", name="Create the workspace")
    page.get_by_label("rename policies.holder").fill("premium")
    expect()(page.get_by_text("policies has two columns named premium")).to_be_visible()
    expect()(button).to_be_disabled()
    page.get_by_label("rename policies.holder").fill("Not A Name")
    expect()(page.get_by_text("Not A Name is not a usable column name")).to_be_visible()
    expect()(button).to_be_disabled()


def test_map_says_which_files_the_draft_left_out(browser, stack, tmp_path):
    """The proposal drops a file the run cannot read. Nobody should learn that later."""
    odd = tmp_path / "wide.csv"
    odd.write_text("a,b\n1,2\n3,4\n", encoding="utf-32")
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page, [*JOIN_FILES, odd])
    to_map(page)
    expect()(page.get_by_text("The run cannot read utf-32")).to_be_visible()


def test_map_shows_the_servers_refusal(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    page.get_by_label("workspace name").fill("!!")
    page.get_by_role("button", name="Create the workspace").click()
    expect()(page.get_by_text("bad workspace name")).to_be_visible()
    expect()(page.get_by_role("heading", name="Map")).to_be_visible()


def test_an_api_failure_shows_an_error_and_retry_recovers(browser, stack):
    page = new_page(browser, stack)
    page.route("**/uploads", lambda route: route.fulfill(status=500, json={"detail": "the store is down"}))
    sign_up(page, stack)
    expect()(page.get_by_text("the store is down")).to_be_visible()
    page.unroute("**/uploads")
    page.get_by_role("button", name="Try again").click()
    expect()(page.get_by_role("heading", name="Upload your files")).to_be_visible()


def test_a_viewer_joins_by_invite_and_can_change_nothing(browser, stack):
    owner = new_page(browser, stack)
    walk_to_ask(owner, stack)
    me = session_of(owner)
    made = owner.request.post(f"{stack.base}/orgs/{me['org_slug']}/invites",
                              headers={"authorization": f"Bearer {me['token']}"},
                              data={"email": "viewer@acme.test", "role": "viewer"})
    assert made.ok, made.text()

    page = new_page(browser, stack)
    writes: list[str] = []
    page.on("request", lambda r: writes.append(r.url) if r.method != "GET" else None)
    page.goto(f"{stack.base}/console/#invite={made.json()['token']}")
    page.get_by_label("choose a password").fill(PASSWORD)
    page.get_by_role("button", name="Join the org").click()
    expect()(page.get_by_role("heading", name="Ask")).to_be_visible()
    assert writes == [f"{stack.base}/auth/accept-invite"]
    assert "invite" not in page.url

    # Read only is on every step, and each step still shows the org's real data.
    expect()(page.get_by_text("You have read access here")).to_be_visible()
    expect()(page.get_by_label("your question")).to_be_disabled()
    expect()(page.get_by_role("button", name="Get one policy for policy P-100")).to_be_disabled()
    page.get_by_role("button", name="Ontology", exact=False).first.click()
    expect()(page.get_by_text("This table has one row per policy_no.")).to_be_visible()
    expect()(page.get_by_role("button", name="Accept this ontology")).to_have_count(0)
    page.get_by_role("button", name="Run", exact=False).first.click()
    expect()(page.get_by_label("job log")).to_contain_text("evals passed: True")
    expect()(page.get_by_role("button", name="Run again")).to_have_count(0)
    page.get_by_role("button", name="Map", exact=False).first.click()
    expect()(page.get_by_label("rename policies.holder")).to_be_disabled()
    expect()(page.get_by_role("button", name="Create the workspace")).to_have_count(0)
    page.get_by_role("button", name="Profile", exact=False).first.click()
    expect()(page.get_by_text("policy_no").first).to_be_visible()
    page.get_by_role("button", name="Upload", exact=False).first.click()
    expect()(page.get_by_text("claims.csv").first).to_be_visible()
    expect()(page.get_by_role("button", name="Upload and profile")).to_have_count(0)
    assert writes == [f"{stack.base}/auth/accept-invite"]


def test_a_member_can_upload_but_map_needs_an_admin(browser, stack):
    owner = new_page(browser, stack)
    sign_up(owner, stack)
    upload(owner)
    to_map(owner)
    me = session_of(owner)
    made = owner.request.post(f"{stack.base}/orgs/{me['org_slug']}/invites",
                              headers={"authorization": f"Bearer {me['token']}"},
                              data={"email": "member@acme.test", "role": "member"})
    page = new_page(browser, stack)
    page.goto(f"{stack.base}/console/#invite={made.json()['token']}")
    page.get_by_label("choose a password").fill(PASSWORD)
    page.get_by_role("button", name="Join the org").click()
    expect()(page.get_by_role("heading", name="Map")).to_be_visible()
    expect()(page.get_by_text("Creating a workspace needs an admin")).to_be_visible()
    expect()(page.get_by_role("button", name="Create the workspace")).to_have_count(0)


def test_a_bad_invite_is_refused_on_the_join_form(browser, stack):
    page = new_page(browser, stack)
    page.goto(f"{stack.base}/console/#invite=not-a-real-token")
    page.get_by_label("choose a password").fill(PASSWORD)
    page.get_by_role("button", name="Join the org").click()
    expect()(page.get_by_text("that invite is not valid")).to_be_visible()


def test_a_second_org_starts_empty(browser, stack):
    first = new_page(browser, stack)
    sign_up(first, stack)
    upload(first)
    second = new_page(browser, stack)
    sign_up(second, stack, email="owner@other.test", org="Other Org")
    expect()(second.get_by_role("heading", name="Upload your files")).to_be_visible()
    expect()(second.get_by_text("No files yet.")).to_be_visible()
    expect()(second.get_by_text("claims.csv")).to_have_count(0)


def test_the_wizard_panels_use_six_pixel_corners(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    expect()(page.get_by_role("heading", name="Upload your files")).to_be_visible()
    radius = page.get_by_test_id("wizard-panel").first.evaluate("e => getComputedStyle(e).borderRadius")
    assert radius == "6px"


def test_the_wizard_copy_has_no_em_dashes(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page, JOIN_FILES)
    to_map(page)
    assert "—" not in page.locator("main").inner_text()
