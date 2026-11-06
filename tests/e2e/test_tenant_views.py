"""Every console view, in a real browser, on a workspace an org made for itself.

`ironwright platform` and `ironwright worker` run as their own processes on a throwaway
data root. There is no route stub here, and a test below holds the file to that. The
workspace comes from the wizard's own screens, walked with F1's helpers.

No error state means three things, because a view can look fine while it is broken: no
error on screen, no exception in the page, and no refused request. Approvals swallowed its
404 that way, so the check on the screen alone would have passed it.

CI runs this in the demo job, where a skip fails the job.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import test_wizard_real_backend as wizard
import yaml
from test_wizard_real_backend import CLEAN_CSVS, PASSWORD, SLOW, Walk

ROOT = Path(__file__).resolve().parents[2]
# One real server and one browser for this file, the way the wizard's walk starts them.
browser, console_dist, live = wizard.browser, wizard.console_dist, wizard.live
HIDDEN = {"Agents", "Training", "Approvals"}

expect = pytest.importorskip("playwright.sync_api").expect


@dataclass
class Person:
    """Someone who signed up through the wizard, and the one workspace they made."""

    email: str

    @property
    def workspace(self) -> str:
        return self.email


def _made_by_the_wizard(browser, live, label: str, *, finish: bool) -> Person:
    """The wizard's screens, in a browser, on the real server. Finish goes on to a cited
    answer. Otherwise it stops at Run: the workspace exists, and nothing has run."""
    walk = Walk(browser, live, f"views-{label}-{uuid.uuid4().hex[:8]}")
    # The walk owns the email it signed up with, so sign in later with exactly that one.
    person = Person(walk.email)
    walk.to_map(CLEAN_CSVS)
    if finish:
        assert walk.to_proposal(person.workspace)["objects"]
        walk.accept_and_ask()
    else:
        walk.page.get_by_label("workspace name").fill(person.workspace)
        walk.page.get_by_role("button", name="Create the workspace").click()
        expect(walk.page.get_by_role("heading", name="Run")).to_be_visible()
    walk.context.close()
    return person


@pytest.fixture(scope="module")
def built(browser, live) -> Person:
    return _made_by_the_wizard(browser, live, "built", finish=True)


@pytest.fixture(scope="module")
def new_workspace(browser, live) -> Person:
    return _made_by_the_wizard(browser, live, "new", finish=False)


class Console:
    """One person in one fresh browser, with every refusal the page saw written down."""

    def __init__(self, browser, base: str, person: Person) -> None:
        self.context = browser.new_context()
        self.context.set_default_timeout(20_000)
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.refused: list[str] = []
        self.in_flight = 0
        self.page.on("request", lambda _r: self._count(1))
        self.page.on("requestfinished", lambda _r: self._count(-1))
        self.page.on("requestfailed", lambda _r: self._count(-1))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("response", lambda r: self.refused.append(f"{r.status} {r.request.method} {r.url}")
                     if r.status >= 400 else None)
        self.base, self.person = base, person
        self._sign_in()

    def _count(self, change: int) -> None:
        self.in_flight += change

    def settle(self) -> None:
        """Quiet for half a second. Playwright's networkidle fires once per page load, so on
        a single page app it returns before a view's fetches land, and every check passes."""
        quiet_since = time.monotonic()
        deadline = quiet_since + 60
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
            if self.in_flight > 0:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= 0.5:
                return
        raise AssertionError("the view never stopped fetching")

    def _sign_in(self) -> None:
        page = self.page
        page.goto(f"{self.base}/console/")
        page.get_by_label("email").fill(f"{self.person.email}@acme.test")
        page.get_by_label("password").fill(PASSWORD)
        page.get_by_role("button", name="Enter the console").click()
        page.get_by_role("button", name="Go to the console").click(timeout=SLOW)
        expect(page.get_by_role("heading", name="Workspaces")).to_be_visible()
        page.locator("button.card", has_text="your workspace").click()
        expect(self.rail().first).to_be_visible()
        # The wizard screens on the way in are F1's walk and hold their own record.
        self.settle()
        self.refused.clear()

    def rail(self):
        return self.page.locator("nav.rail .railgroup ~ button")

    def rail_labels(self) -> list[str]:
        return [b.strip() for b in self.rail().all_inner_texts()]

    def open(self, label: str) -> None:
        self.page.locator("nav.rail button", has_text=label).first.click()
        self.settle()

    def assert_no_error_state(self, label: str) -> None:
        assert self.page.locator("main .err").all_inner_texts() == [], label
        assert self.refused == [], label
        assert self.errors == [], label


def _sources(page, _state) -> None:
    expect(page.get_by_text("customers.csv", exact=True)).to_be_visible()
    expect(page.get_by_text("orders.csv", exact=True)).to_be_visible()


def _pipeline(page, _state) -> None:
    expect(page.get_by_text("evals green")).to_be_visible()
    expect(page.get_by_text("Nothing has run")).to_have_count(0)
    expect(page.get_by_text("generate", exact=True)).to_have_count(0)


def _loads(page, _state) -> None:
    expect(page.get_by_text("published to main").first).to_be_visible()


def _explorer(page, _state) -> None:
    expect(page.locator(".list button", has_text="customers").first).to_be_visible()
    expect(page.get_by_label("SQL query, a single SELECT")).not_to_have_value("")
    page.get_by_role("button", name="Run query").click()
    expect(page.locator("main table tbody tr").first).to_be_visible()
    expect(page.get_by_role("button", name="Run models on postgres and compare")).to_have_count(0)
    expect(page.get_by_text("stays in your org's storage")).to_be_visible()


def _ontology(page, _state) -> None:
    """All the way to one object: the view opens on a list, and its reads come after."""
    page.locator("main .list button").first.click()
    page.locator("main tbody button.linklike").first.click()
    expect(page.get_by_role("button", name="back to")).to_be_visible()


def _discovery(page, _state) -> None:
    expect(page.get_by_text("identities resolved cleanly")).to_be_visible()


def _ask(page, _state) -> None:
    expect(page.locator("main textarea, main input").first).to_be_visible()


def _boards(page, _state) -> None:
    page.get_by_label("new board").fill("weekly")
    page.get_by_role("button", name="Create with starter tiles").click()
    page.get_by_role("button", name="Run all tiles").click()
    expect(page.locator("main table tbody tr").first).to_be_visible()


def _bi(page, _state) -> None:
    expect(page.get_by_text("No dashboard URL saved yet")).to_be_visible()
    expect(page.get_by_role("button", name="Mirror lake to postgres")).to_have_count(0)
    expect(page.get_by_text("stays in your org's storage")).to_be_visible()


def _audit(page, _state) -> None:
    page.get_by_role("button", name="Verify the chain").click()
    expect(page.get_by_text("intact")).to_be_visible()


def _governance(page, _state) -> None:
    expect(page.get_by_text("org-default-members-read")).to_be_visible()
    expect(page.get_by_text("local build")).to_have_count(0)


def _principals(page, _state) -> None:
    expect(page.get_by_role("button", name="user:*")).to_be_visible()


def _platform(page, _state) -> None:
    expect(page.get_by_role("heading", name="Platform")).to_be_visible()


# Every view holds real content, or a green walk would prove only that nothing threw.
CONTENT: dict[str, Callable] = {
    "Platform": _platform, "Sources": _sources, "Pipeline": _pipeline, "Loads": _loads,
    "Data explorer": _explorer, "Ontology": _ontology, "Discovery": _discovery, "Ask": _ask, "Boards": _boards,
    "BI embed": _bi, "Audit": _audit, "Governance": _governance, "Principals": _principals,
}


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


def test_every_view_in_the_rail_opens_with_no_error_state(browser, live, built):
    """The rail is read off the page, so a view added later is red here until it has content."""
    console = Console(browser, live, built)
    labels = console.rail_labels()
    assert not (set(labels) - set(CONTENT)), "a new view needs a line in CONTENT"
    assert set(CONTENT) <= set(labels), "a view this file expects is gone from the rail"
    for label in labels:
        console.open(label)
        CONTENT[label](console.page, built)
        console.settle()
        console.assert_no_error_state(label)
    console.context.close()


def test_views_that_cannot_apply_are_hidden_with_a_reason(browser, live, built):
    console = Console(browser, live, built)
    assert not (HIDDEN & set(console.rail_labels()))
    reason = console.page.locator("nav.rail").get_by_label("views hidden here")
    for label in HIDDEN:
        expect(reason).to_contain_text(label)
    console.context.close()


def test_a_stored_view_that_cannot_apply_shows_the_reason(browser, live, built):
    """A browser remembers the last view, and that can be one this workspace hides."""
    console = Console(browser, live, built)
    console.page.evaluate(
        "ws => localStorage.setItem('iw.last', JSON.stringify({ ws, view: 'training' }))", built.workspace)
    console.page.reload()
    console.settle()
    expect(console.page.locator("main")).to_contain_text("hidden")
    console.assert_no_error_state("stored training view")
    console.context.close()


def test_a_workspace_that_has_not_run_shows_no_error_state(browser, live, new_workspace):
    """New is not broken. Each of these says it is empty, in its own words."""
    console = Console(browser, live, new_workspace)
    for label in console.rail_labels():
        console.open(label)
        console.assert_no_error_state(label)
        if label == "Pipeline":
            expect(console.page.get_by_text("Nothing has run")).to_be_visible()
        if label == "Loads":
            expect(console.page.get_by_text("Nothing has loaded")).to_be_visible()
    console.context.close()
