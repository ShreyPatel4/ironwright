"""The onboarding wizard, in a real headless browser, against a real platform app.

Everything behind the page is real: the built console, signup, login, invites, roles,
the app gate, uploads, the profiler and its proposal, the job queue, the worker, the log
tail, the run, the ontology proposer and Ask. This suite used to stand adapters in for
routes that did not exist yet, and they outlived those routes and hid every mismatch a
live walk then found. A test here may fake a failure with page.route. Nothing else.

Each test states the break it catches in its name.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import cookie_session
import pytest
import uvicorn

from ironwright import workspaces_tenant as wt
from ironwright.api import harden
from ironwright.jobs.worker import Worker
from ironwright.lake import Lakehouse
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import db

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
    """One platform app on a real socket, and its worker."""

    def __init__(self) -> None:
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


@pytest.fixture()
def stack(console_dist, monkeypatch, tmp_path):
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "0")
    s = Stack()
    app = create_platform_app()
    server = uvicorn.Server(uvicorn.Config(harden(app), host="127.0.0.1", port=0, log_level="error"))
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
    return cookie_session.session_of(page)


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
    page.get_by_label("keep claims.status").uncheck()
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
    """T3's two CSVs: they have the keys and the join an ontology needs."""
    sign_up(page, stack)
    upload(page, JOIN_FILES)
    to_map(page)
    page.get_by_label("workspace name").fill("acme-sales")
    page.get_by_role("button", name="Create the workspace").click()
    expect()(page.get_by_role("heading", name="Run")).to_be_visible()
    run_pipeline(page)
    to_ontology(page)
    with page.expect_response(lambda r: r.url.endswith("/ontology/proposal")) as read:
        page.get_by_role("button", name="Propose an ontology").click()
    proposal = read.value.json()
    first = proposal["objects"][0]["name"]
    page.get_by_label(f"object name {first}").fill("Renamed", timeout=SLOW)
    with page.expect_request(lambda r: r.url.endswith("/ontology/accept")) as accepted:
        page.get_by_role("button", name="Accept this ontology").click()
    # Accept queues the run that builds the objects, so Ask comes after that run.
    expect()(page.get_by_role("heading", name="Ask")).to_be_visible(timeout=SLOW)
    return proposal, json.loads(accepted.value.post_data)


def test_the_whole_wizard_ends_on_an_answer_with_evidence(browser, stack):
    page = new_page(browser, stack)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    proposal, accepted = walk_to_ask(page, stack)

    # The accept carries the id of what was reviewed and only what changed.
    assert accepted["proposal_id"] == proposal["id"]
    assert accepted["edits"] == [{"op": "rename_object", "object": proposal["objects"][0]["name"], "to": "Renamed"}]

    # A refusal is shown as a refusal, with its reason.
    page.get_by_label("your question").fill("what is the weather")
    page.get_by_role("button", name="Ask", exact=True).click()
    expect()(page.get_by_text("No registered question matches this.")).to_be_visible()

    with page.expect_response(lambda r: r.url.endswith("/ask")) as asked:
        page.get_by_label("example questions").get_by_role("button").first.click()
    answer = asked.value.json()
    assert answer["status"] == "answered", answer
    assert answer["citations"], answer
    expect()(page.get_by_label("answer")).to_contain_text(f"[ev:{answer['citations'][0]}]")
    expect()(page.get_by_label(f"evidence {answer['citations'][0]}")).to_be_visible()
    assert errors == []


def test_ontology_edits_go_out_as_w2s_ops_in_an_order_it_can_apply(browser, stack):
    """W2 applies edits in order, by the current name, and a dropped object takes its
    links with it. So a link drop goes first, and set_key goes before a rename."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page, JOIN_FILES)
    to_map(page)
    page.get_by_label("workspace name").fill("acme-sales")
    page.get_by_role("button", name="Create the workspace").click()
    run_pipeline(page)
    to_ontology(page)
    with page.expect_response(lambda r: r.url.endswith("/ontology/proposal")) as read:
        page.get_by_role("button", name="Propose an ontology").click()
    proposal = read.value.json()
    customer = next(o for o in proposal["objects"] if o["name"] == "Customer")
    other_key = next(p["name"] for p in customer["properties"] if p["unique"] and p["name"] != customer["key"])
    link = proposal["links"][0]["name"]
    page.get_by_label("object name Customer").fill("Buyer", timeout=SLOW)
    page.get_by_label("key of Customer").select_option(other_key)
    page.get_by_label(f"keep link {link}").uncheck()
    with page.expect_request(lambda r: r.url.endswith("/ontology/accept")) as accepted:
        page.get_by_role("button", name="Accept this ontology").click()
    assert json.loads(accepted.value.post_data)["edits"] == [
        {"op": "drop_link", "link": link},
        {"op": "set_key", "object": "Customer", "key": other_key},
        {"op": "rename_object", "object": "Customer", "to": "Buyer"},
    ]


def test_ontology_says_why_a_table_became_no_object(browser, stack):
    """Untick every dedupe in the draft and T2's two CSVs keep their duplicates, so
    no key and no object. Accept would only be refused, so there is no button to press."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    for source in ("claims", "policies"):
        page.get_by_label(f"apply {source}.collapse_on_key").uncheck()
        page.get_by_label(f"apply {source}.drop_exact_duplicates").uncheck()
    map_and_create(page)
    run_pipeline(page)
    to_ontology(page)
    page.get_by_role("button", name="Propose an ontology").click()
    expect()(page.get_by_text("no key to build an object on").first).to_be_visible(timeout=SLOW)
    expect()(page.get_by_role("button", name="Accept this ontology")).to_have_count(0)


def test_messy_files_with_the_draft_as_proposed_reach_objects_and_a_link(browser, stack):
    """The walk that dead ended here: two exports with duplicates and no silver SQL."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    page.get_by_label("workspace name").fill("acme-claims")
    page.get_by_role("button", name="Create the workspace").click()
    run_pipeline(page)
    to_ontology(page)
    page.get_by_role("button", name="Propose an ontology").click()
    expect()(page.get_by_role("button", name="Accept this ontology")).to_be_visible(timeout=SLOW)
    expect()(page.get_by_text("silver.claims").first).to_be_visible()
    expect()(page.get_by_text("silver.policies").first).to_be_visible()
    expect()(page.get_by_text("Claim to Policy via policy_ref").first).to_be_visible()


def _trailer_file(tmp_path) -> Path:
    rows = [f"C-{100 + i},P{i},{10 + i}.50" for i in range(9)]
    f = tmp_path / "claims.csv"
    f.write_text("\n".join(["claim_id,policy,amount", *rows, "TOTAL ROWS: 9,,"]) + "\n")
    return f


def test_map_proposes_the_trailer_skip_and_the_row_stays_out_of_the_lake(browser, stack, tmp_path):
    """R1 F-04 in the browser, on the real profiler and the real reader."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page, [_trailer_file(tmp_path)])
    to_map(page)
    box = page.get_by_label("skip claims footer")
    expect()(box).to_be_checked()
    expect()(page.get_by_text("1 record after the data")).to_be_visible()
    page.get_by_label("workspace name").fill("acme-trailer")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/workspaces")) as made:
        page.get_by_role("button", name="Create the workspace").click()
    assert json.loads(made.value.post_data)["records"]["claims"]["skip_footer"] == 1
    expect()(page.get_by_role("heading", name="Run")).to_be_visible()
    run_pipeline(page)
    lake = Lakehouse(wt.load_workspace(session_of(page)["org_id"], "acme-trailer"))
    assert lake.read("silver.claims").num_rows == 9


def test_map_lets_the_human_keep_the_trailer(browser, stack, tmp_path):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page, [_trailer_file(tmp_path)])
    to_map(page)
    page.get_by_label("skip claims footer").uncheck()
    page.get_by_label("workspace name").fill("acme-keeps")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/workspaces")) as made:
        page.get_by_role("button", name="Create the workspace").click()
    assert json.loads(made.value.post_data)["records"]["claims"]["skip_footer"] == 0


def test_map_edits_reach_the_real_lake(browser, stack):
    """Rename and drop have to change the silver table, not just the form."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    body = map_and_create(page)

    by_name = {s["name"]: s for s in body["spec"]["sources"]}
    assert "status" not in [c["name"] for c in by_name["claims"]["columns"]]
    assert "models" not in body  # the server writes the SQL, from what the human chose
    assert body["silver"]["renames"] == {"policies": {"holder": "policy_holder"}}
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
    assert "status" not in [d[0] for d in con.execute("select * from silver_claims limit 0").description]


def test_map_shows_the_silver_draft_with_counts_and_an_untick_reaches_the_lake(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    expect()(page.get_by_text("5 rows come out of 6")).to_be_visible()
    collapse = page.get_by_label("apply claims.collapse_on_key")
    expect()(collapse).to_be_checked()
    expect()(page.get_by_test_id("decision-claims.collapse_on_key")).to_contain_text("1 of 6 rows")
    collapse.uncheck()
    page.get_by_label("workspace name").fill("acme-claims")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/workspaces")) as made:
        page.get_by_role("button", name="Create the workspace").click()
    expect()(page.get_by_role("heading", name="Run")).to_be_visible()
    assert json.loads(made.value.post_data)["silver"]["declined"] == ["claims.collapse_on_key"]

    run_pipeline(page)
    pack = wt.load_workspace(session_of(page)["org_id"], "acme-claims")
    con = Lakehouse(pack).connect()
    assert con.execute("select count(*) from silver_claims where claim_id = 'C-1002'").fetchone()[0] == 2


def test_map_can_regenerate_silver_after_the_workspace_exists(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    page.get_by_label("apply claims.collapse_on_key").uncheck()
    map_and_create(page)
    run_pipeline(page)
    org_id = session_of(page)["org_id"]

    page.get_by_role("button", name="Map", exact=False).first.click()
    expect()(page.get_by_role("heading", name="Map")).to_be_visible()
    again = page.get_by_label("apply claims.collapse_on_key")
    expect()(again).not_to_be_checked()
    again.check()
    with page.expect_response(lambda r: r.url.endswith("/silver/draft") and r.request.method == "POST") as done:
        page.get_by_role("button", name="Rewrite the silver models").click()
    assert done.value.status == 200
    assert json.loads(done.value.request.post_data)["declined"] == []
    expect()(page.get_by_text("The next run builds them.")).to_be_visible()
    sql = (wt.load_workspace(org_id, "acme-claims").dir / "models" / "silver" / "01_claims.sql").read_text()
    assert "row_number()" in sql


def test_a_silver_refusal_reads_as_words_in_the_map_step(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    page.get_by_label("workspace name").fill("acme-claims")
    page.route("**/workspaces", lambda route: route.fulfill(status=422, json={"detail": "no decision named x"})
               if route.request.method == "POST" else route.continue_())
    page.get_by_role("button", name="Create the workspace").click()
    expect()(page.get_by_text("no decision named x")).to_be_visible()


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


def test_your_uploads_lists_what_the_server_holds(browser, stack):
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    page.get_by_role("button", name="Upload", exact=False).first.click()
    expect()(page.get_by_text("newest, and the one the next steps use")).to_be_visible()
    for f in T2_FILES:
        expect()(page.get_by_text(f.name, exact=True)).to_be_visible()


def test_a_new_browser_resumes_at_profile_and_follows_the_same_job(browser, idle_stack):
    page = new_page(browser, idle_stack)
    sign_up(page, idle_stack)
    upload(page)
    expect()(page.get_by_text("waiting for a worker")).to_be_visible()

    again = new_page(browser, idle_stack)
    log_in(again, idle_stack)
    expect()(again.get_by_role("heading", name="Profile")).to_be_visible()
    expect()(again.get_by_text("waiting for a worker")).to_be_visible()
    expect()(again.get_by_role("button", name="Profile these files")).to_have_count(0)


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


def test_a_draft_taken_as_proposed_runs(browser, stack):
    """It used to name a column the reader cannot find, so the run failed unless the
    user dropped it on Map. The profile and the reader spell an empty header differently."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)
    page.get_by_label("workspace name").fill("as-proposed")
    page.get_by_role("button", name="Create the workspace").click()
    page.get_by_role("button", name="Run the pipeline").click()
    expect()(page.get_by_role("button", name="Continue to Ontology")).to_be_visible(timeout=SLOW)


def test_a_failed_run_shows_why_and_cannot_continue(browser, stack):
    """The spec names a column the file does not have, so the run really fails. Map cannot
    write that spec, so this changes the one request, and the server answers for real."""
    page = new_page(browser, stack)
    sign_up(page, stack)
    upload(page)
    to_map(page)

    def declare_a_column_the_file_lacks(route):
        body = json.loads(route.request.post_data)
        body["spec"]["sources"][0]["columns"].append({"name": "never_there", "type": "string"})
        route.continue_(post_data=json.dumps(body))

    page.route("**/workspaces", lambda route: declare_a_column_the_file_lacks(route)
               if route.request.method == "POST" else route.continue_())
    page.get_by_label("workspace name").fill("as-proposed")
    page.get_by_role("button", name="Create the workspace").click()
    page.get_by_role("button", name="Run the pipeline").click()
    expect()(page.get_by_text("the file has no column never_there").first).to_be_visible(timeout=SLOW)
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
    expect()(page.get_by_label("example questions").get_by_role("button").first).to_be_disabled()
    page.get_by_role("button", name="Ontology", exact=False).first.click()
    expect()(page.get_by_text("is unique and never empty").first).to_be_visible()
    expect()(page.get_by_role("button", name="Accept this ontology")).to_have_count(0)
    page.get_by_role("button", name="Run", exact=False).first.click()
    expect()(page.get_by_label("job log")).to_contain_text("evals passed: True")
    expect()(page.get_by_role("button", name="Run again")).to_have_count(0)
    page.get_by_role("button", name="Map", exact=False).first.click()
    expect()(page.get_by_label("rename customers.region")).to_be_disabled()
    expect()(page.get_by_role("button", name="Create the workspace")).to_have_count(0)
    page.get_by_role("button", name="Profile", exact=False).first.click()
    expect()(page.get_by_text("customer_id").first).to_be_visible()
    page.get_by_role("button", name="Upload", exact=False).first.click()
    expect()(page.get_by_text("customers.csv").first).to_be_visible()
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


def test_a_returning_customer_who_signs_up_is_told_to_sign_in(browser, stack):
    first = new_page(browser, stack)
    sign_up(first, stack)
    expect()(first.get_by_role("heading", name="Upload your files")).to_be_visible()
    page = new_page(browser, stack)
    sign_up(page, stack)
    expect()(page.get_by_text("If this email already has an account, sign in with its password.")).to_be_visible()
    expect()(page.get_by_role("button", name="Enter the console")).to_be_visible()
    assert cookie_session.token_of(page.context) is None
