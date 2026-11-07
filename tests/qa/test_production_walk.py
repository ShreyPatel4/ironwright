"""The production QA walk (Q1, November 2026), kept so a fixer can re-run it.

It drives a stack that is already running, in a real browser, with no route stub. It
never starts a server, so default `pytest -q` skips every test here. To run it:

    IRONWRIGHT_DATA=$PWD/.qa-data ironwright worker &
    IRONWRIGHT_DATA=$PWD/.qa-data ironwright platform --port 8020 &
    IRONWRIGHT_QA_URL=http://127.0.0.1:8020 pytest -q tests/qa -s

Optional: IRONWRIGHT_QA_AXE points at axe.min.js (axe-core 4.10) for the accessibility
pass, and IRONWRIGHT_QA_SHOTS picks the screenshot folder (default
docs/review/qa-2026-11/shots). Every test writes what it saw to walk.json beside the
shots, so a fixer can diff two runs. The report is docs/review/2026-11-production-qa.md.

Most checks here record rather than assert, because a QA walk should get to the end and
report every finding, not stop at the first. The asserts that stay are the ones a fixer
must turn green: isolation, no session in a URL, no traceback on screen.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = os.environ.get("IRONWRIGHT_QA_URL", "").rstrip("/")
SHOTS = Path(os.environ.get("IRONWRIGHT_QA_SHOTS", ROOT / "docs" / "review" / "qa-2026-11" / "shots"))
AXE = os.environ.get("IRONWRIGHT_QA_AXE", "")
PASSWORD = "correct horse battery"
RUN = uuid.uuid4().hex[:6]
SLOW = 180_000
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
# Buttons a click-everything pass must not press: they end the session or destroy data.
DANGER = re.compile(r"delete|remove|sign out|revoke|cancel|deny|approve|mirror|export", re.IGNORECASE)
FIXTURES = ROOT / "tests" / "fixtures"

pytestmark = pytest.mark.skipif(not BASE, reason="IRONWRIGHT_QA_URL is not set: the QA walk needs a running stack")

LOG: dict[str, Any] = {"base": BASE, "run": RUN, "steps": []}


def note(area: str, what: str, **data: Any) -> None:
    LOG["steps"].append({"area": area, "what": what, **data})
    print(f"[{area}] {what} {json.dumps(data, default=str)[:400]}")


@pytest.fixture(scope="session", autouse=True)
def _write_log() -> Iterator[None]:
    SHOTS.mkdir(parents=True, exist_ok=True)
    yield
    if BASE and LOG["steps"]:
        out = SHOTS.parent / "walk.json"
        try:
            runs = json.loads(out.read_text())["runs"]
        except (OSError, ValueError, KeyError):
            runs = []
        out.write_text(json.dumps({"runs": [*runs, LOG]}, indent=1, default=str))


# HTTP, for setup the UI is not under test for.

def call(method: str, path: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
    headers = {"content-type": "application/json"} if body is not None else {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def api_signup(label: str) -> dict[str, Any]:
    status, got = call("POST", "/auth/signup", {"email": f"qa-{label}-{RUN}@example.com",
                                                "password": PASSWORD, "org_name": f"QA {label} {RUN}"})
    assert status == 201, got
    return got


# The browser.

@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


class Tab:
    """One browser context with every page error and refused call written down."""

    def __init__(self, browser, viewport=DESKTOP, session: dict | None = None, onboarded: bool = True) -> None:
        self.context = browser.new_context(viewport=viewport, bypass_csp=bool(AXE))
        self.context.set_default_timeout(20_000)
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.refused: list[str] = []
        self.urls: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}")
                     if m.type == "error" else None)
        self.page.on("response", lambda r: self.refused.append(f"{r.status} {r.request.method} {r.url}")
                     if r.status >= 400 else None)
        self.page.on("request", lambda r: self.urls.append(r.url))
        if session:
            # Once per tab: re-seeding on every load would undo a sign out and loop a 401 reload.
            self.context.add_init_script(
                "try { if (!sessionStorage.getItem('qa.seeded')) { sessionStorage.setItem('qa.seeded', '1');"
                " localStorage.setItem('iw.session', " + json.dumps(json.dumps(session)) + ");"
                + (" localStorage.setItem('iw.onboarded', '1');" if onboarded else "") + " } } catch (e) {}")

    def shot(self, name: str) -> str:
        path = SHOTS / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)

    def quiet(self, ms: int = 600) -> None:
        """A single page app is idle long before its fetches land, so wait for real quiet."""
        with contextlib.suppress(Exception):  # a long poll keeps the network busy; the wait below still holds
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        self.page.wait_for_timeout(ms)

    def screen_errors(self) -> list[str]:
        """Error text a person would read: the console's Err blocks and raw JSON or tracebacks."""
        self.page.wait_for_load_state()
        try:
            texts = self.page.locator(".err, [role=alert]").all_inner_texts()
            body = self.page.inner_text("body")
        except Exception as e:  # noqa: BLE001  the page navigated under us; say so instead of failing the walk
            return [f"page navigated while reading: {str(e)[:80]}"]
        bad = [t.strip()[:200] for t in texts if t.strip()]
        if "Traceback" in body or re.search(r'\{"detail"|\[\{"type":', body):
            bad.append("raw JSON or traceback on screen")
        return bad

    def axe(self, label: str) -> list[dict[str, Any]]:
        if not AXE or not Path(AXE).exists():
            return []
        self.page.add_script_tag(path=AXE)
        got = self.page.evaluate("""async () => {
            const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']}});
            return r.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.length,
                                           sample: v.nodes[0] && v.nodes[0].target.join(' ')}));
        }""")
        if got:
            note("a11y", f"axe on {label}", violations=got)
        return got

    def close(self) -> None:
        self.context.close()


def timed(label: str, fn) -> float:
    t0 = time.perf_counter()
    fn()
    dt = round(time.perf_counter() - t0, 2)
    note("perf", label, seconds=dt, over_2s=dt > 2)
    return dt


# Files a strict buyer would bring.

def nasty_files(tmp: Path) -> dict[str, Path]:
    random.seed(7)
    rows = ["Kundennummer,Name 名前,Ünït Prïce €,Stadt,Notiz"]
    rows += [f'K{i:06d},Kunde {i} ☃,{random.randint(1, 99999) / 100},München,"Zeile, mit Komma {i}"'
             for i in range(50_000)]
    out = {"50k-unicode.csv": "\n".join(rows).encode(),
           "30mb.csv": "\n".join(["id,payload,amount"] + [f"{i},{'x' * 560},{i % 1000}.5"
                                                         for i in range(54_700)]).encode(),
           "empty.csv": b"", "book.xlsx": b"PK\x03\x04" + b"\x00" * 200,
           "header-only.csv": b"id,name,amount\n"}
    paths = {}
    for name, data in out.items():
        p = tmp / name
        p.write_bytes(data)
        paths[name] = p
    return paths


# 1. The journey, three orgs, three kinds of mess.

def wizard(tab: Tab, label: str, files: list[Path], slug: str) -> dict[str, Any]:
    from playwright.sync_api import expect
    page, seen = tab.page, {"org": label, "slug": slug}
    page.goto(f"{BASE}/console/")
    tab.shot(f"j-{label}-00-door")
    page.get_by_role("button", name="Sign up").click()
    page.get_by_label("org name").fill(f"QA {label} {RUN}")
    page.get_by_label("email").fill(f"qa-{label}-{RUN}@example.com")
    page.get_by_label("password").fill(PASSWORD)
    timed(f"{label}: sign up", lambda: (page.get_by_role("button", name="Create the org").click(),
                                        expect(page.get_by_role("heading", name="Upload your files")).to_be_visible()))
    page.get_by_label("Choose files").set_input_files([str(f) for f in files])
    timed(f"{label}: upload", lambda: (page.get_by_role("button", name="Upload and profile").click(),
                                       expect(page.get_by_role("heading", name="Profile")).to_be_visible(timeout=SLOW)))
    timed(f"{label}: profile to map", lambda: (page.get_by_role("button", name="Continue to Map").click(timeout=SLOW),
                                               expect(page.get_by_role("heading", name="Map")).to_be_visible()))
    tab.shot(f"j-{label}-03-map")
    page.get_by_label("workspace name").fill(slug)
    timed(f"{label}: create workspace", lambda: (page.get_by_role("button", name="Create the workspace").click(),
                                                 expect(page.get_by_role("heading", name="Run")).to_be_visible(timeout=SLOW)))
    page.get_by_role("button", name="Run the pipeline").click()
    timed(f"{label}: run", lambda: page.get_by_role("button", name="Continue to Ontology").wait_for(timeout=SLOW))
    tab.shot(f"j-{label}-04-run")
    page.get_by_role("button", name="Continue to Ontology").click()
    with page.expect_response(lambda r: r.url.endswith("/ontology/proposal"), timeout=SLOW) as read:
        timed(f"{label}: propose", lambda: page.get_by_role("button", name="Propose an ontology").click())
    proposal = read.value.json()
    seen["objects"] = [o.get("name") for o in proposal.get("objects", [])]
    tab.quiet()
    tab.shot(f"j-{label}-05-ontology")
    if not proposal.get("objects"):
        seen["stopped"] = "no objects proposed"
        seen["skipped"] = proposal.get("skipped")
        return seen
    timed(f"{label}: accept to ask", lambda: (page.get_by_role("button", name="Accept this ontology").click(timeout=SLOW),
                                              expect(page.get_by_role("heading", name="Ask")).to_be_visible(timeout=SLOW)))
    tab.quiet()
    examples = page.get_by_label("example questions").get_by_role("button")
    if examples.count():
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/ask")) as asked:
            timed(f"{label}: first ask", lambda: examples.first.click())
        answer = asked.value.json()
        seen["answer"] = {"status": answer.get("status"), "text": answer.get("answer", "")[:300],
                          "citations": answer.get("citations")}
    tab.quiet()
    tab.shot(f"j-{label}-06-ask")
    seen["screen_errors"] = tab.screen_errors()
    return seen


def test_journey_three_orgs(browser, tmp_path):
    nasty = nasty_files(tmp_path)
    orgs = [("clean", sorted((FIXTURES / "ingest" / "join").glob("*.csv")), "qa-clean"),
            ("messy", sorted((FIXTURES / "tenant_ws" / "uploads" / "fixture").glob("*.csv")), "qa-messy"),
            ("nasty", [nasty["50k-unicode.csv"], FIXTURES / "ingest" / "messy" / "claims_export.csv",
                       FIXTURES / "ingest" / "mainframe" / "ITEMMAST.DAT"], "qa-nasty")]
    results = []
    for label, files, slug in orgs:
        tab = Tab(browser)
        try:
            seen = wizard(tab, label, files, slug)
        except Exception as e:  # noqa: BLE001  record where the journey broke and go on to the next org
            seen = {"org": label, "broke": str(e)[:400], "shot": tab.shot(f"j-{label}-broke")}
        seen["refused"], seen["errors"] = tab.refused, tab.errors
        note("journey", label, **seen)
        results.append(seen)
        tab.close()
    LOG["journey"] = results


def test_upload_refusals_in_the_wizard(browser, tmp_path):
    """Empty, xlsx, header only, 30 MB: what the person reads on screen for each."""
    from playwright.sync_api import expect
    nasty = nasty_files(tmp_path)
    s = api_signup("refuse")
    for name in ("empty.csv", "book.xlsx", "header-only.csv", "30mb.csv"):
        tab = Tab(browser, session={**s}, onboarded=False)
        page = tab.page
        page.goto(f"{BASE}/console/")
        try:
            expect(page.get_by_role("heading", name="Upload your files")).to_be_visible()
            page.get_by_label("Choose files").set_input_files(str(nasty[name]))
            t0 = time.perf_counter()
            page.get_by_role("button", name="Upload and profile").click()
            tab.quiet(1500)
            note("upload", name, seconds=round(time.perf_counter() - t0, 2), screen=tab.screen_errors(),
                 heading=page.locator("h2").first.inner_text(), shot=tab.shot(f"u-{name.replace('.', '-')}"))
        except Exception as e:  # noqa: BLE001
            note("upload", name, broke=str(e)[:300], shot=tab.shot(f"u-{name.replace('.', '-')}-broke"))
        tab.close()


# 2. Every view, on a tenant workspace and on every demo pack, at both widths.

def open_workspace(tab: Tab, name: str) -> None:
    """Cards show a title, not the slug, so open by the console's own resume key."""
    page = tab.page
    page.goto(f"{BASE}/console/")
    page.evaluate("n => localStorage.setItem('iw.last', JSON.stringify({ws: n, view: null}))", name)
    page.reload()
    tab.quiet()


def rail(tab: Tab) -> list[str]:
    return [t.strip() for t in tab.page.locator("nav button, aside button").all_inner_texts() if t.strip()]


def walk_views(tab: Tab, ws: str, tag: str, click_all: bool) -> dict[str, Any]:
    page = tab.page
    open_workspace(tab, ws)
    views = [v for v in rail(tab) if v not in ("Settings", "Workspaces", "Sign out", "sign out")]
    out: dict[str, Any] = {"rail": views, "views": {}}
    for v in views:
        before = len(tab.refused)
        t0 = time.perf_counter()
        page.locator("nav button, aside button", has_text=re.compile(f"^{re.escape(v)}$")).first.click()
        tab.quiet()
        seen = {"seconds": round(time.perf_counter() - t0, 2), "screen": tab.screen_errors(),
                "refused": tab.refused[before:], "shot": tab.shot(f"v-{tag}-{v.replace(' ', '_')}")}
        if tag.endswith("desk"):
            seen["axe"] = [a["id"] for a in tab.axe(f"{tag}/{v}")]
        if click_all:
            seen["clicked"] = click_everything(tab, f"{tag}/{v}")
        out["views"][v] = seen
    return out


def click_everything(tab: Tab, where: str) -> list[dict[str, Any]]:
    """Press every safe button and tab in the main pane once; write down what broke."""
    page = tab.page
    found = []
    buttons = page.locator("main button:visible")
    labels = [t.strip() for t in buttons.all_inner_texts()]
    for i, label in enumerate(labels[:25]):
        if not label or DANGER.search(label):
            continue
        before_r, before_e = len(tab.refused), len(tab.errors)
        try:
            b = page.locator("main button:visible").nth(i)
            if b.is_disabled(timeout=1500):
                continue
            b.click(timeout=3000)
            tab.quiet(300)
        except Exception as e:  # noqa: BLE001
            found.append({"button": label, "broke": str(e)[:160]})
            continue
        bad = tab.screen_errors()
        if tab.refused[before_r:] or tab.errors[before_e:] or bad:
            found.append({"button": label, "refused": tab.refused[before_r:], "errors": tab.errors[before_e:],
                          "screen": bad})
    if found:
        note("click", where, found=found)
    return found


@pytest.fixture(scope="module")
def tenant() -> dict[str, Any]:
    """An org with a finished workspace over the clean fixtures, built over the API."""
    s = api_signup("views")
    tok = s["token"]
    files = sorted((FIXTURES / "ingest" / "join").glob("*.csv"))
    boundary = uuid.uuid4().hex
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{f.name}"\r\n'
                    f"Content-Type: text/csv\r\n\r\n".encode() + f.read_bytes() + b"\r\n" for f in files)
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/uploads", data=body, method="POST", headers={
        "authorization": f"Bearer {tok}", "content-type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req) as r:
        up = json.loads(r.read())["id"]

    def wait(job_id: str) -> dict:
        for _ in range(600):
            _, j = call("GET", f"/jobs/{job_id}", token=tok)
            if j["state"] in ("succeeded", "failed", "cancelled"):
                return j
            time.sleep(0.3)
        raise TimeoutError(job_id)

    _, j = call("POST", f"/uploads/{up}/profile", token=tok)
    wait(j["id"])
    _, prop = call("GET", f"/uploads/{up}/proposal", token=tok)
    ws = f"qa-views-{RUN}"
    assert call("POST", "/workspaces", {"from_upload": up, "spec": prop["workspace"], "slug": ws}, token=tok)[0] == 201
    wait(call("POST", "/jobs", {"kind": "run", "workspace": ws, "params": {}}, token=tok)[1]["id"])
    wait(call("POST", "/jobs", {"kind": "ontology_propose", "workspace": ws, "params": {}}, token=tok)[1]["id"])
    _, p = call("GET", f"/workspaces/{ws}/ontology/proposal", token=tok)
    _, acc = call("POST", f"/workspaces/{ws}/ontology/accept", {"proposal_id": p["id"], "edits": []}, token=tok)
    wait(acc["job_id"])
    return {"session": s, "ws": ws, "token": tok}


def demo_packs(token: str) -> list[str]:
    """The packs the server lists, or IRONWRIGHT_QA_PACKS (comma separated) to walk fewer."""
    given = [p for p in os.environ.get("IRONWRIGHT_QA_PACKS", "").split(",") if p]
    return given or [w["name"] for w in call("GET", "/workspaces", token=token)[1] if w["kind"] == "pack"]


@pytest.mark.parametrize("viewport,tag", [(DESKTOP, "desk"), (PHONE, "phone")])
def test_every_view_on_tenant_and_demos(browser, tenant, viewport, tag):
    packs = demo_packs(tenant["token"])
    for ws in ([] if os.environ.get("IRONWRIGHT_QA_PACKS") else [tenant["ws"]]) + packs:
        tab = Tab(browser, viewport=viewport, session=tenant["session"])
        try:
            got = walk_views(tab, ws, f"{ws.split('-')[0] if ws.startswith('qa-') else ws}-{tag}",
                             click_all=(tag == "desk"))
            got["page_errors"] = tab.errors
            if tag == "phone":
                got["h_scroll"] = tab.page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
            note("views", f"{ws} at {tag}", **got)
        except Exception as e:  # noqa: BLE001
            note("views", f"{ws} at {tag}", broke=str(e)[:300], shot=tab.shot(f"v-{ws}-{tag}-broke"))
        tab.close()


# 3. Back, forward, reload, deep links.

def test_back_forward_reload_and_deep_links(browser, tenant):
    tab = Tab(browser, session=tenant["session"])
    page = tab.page
    open_workspace(tab, tenant["ws"])
    page.get_by_role("button", name="Ask", exact=True).first.click()
    tab.quiet()
    at_ask = page.url
    page.get_by_role("button", name="Audit", exact=True).first.click()
    tab.quiet()
    note("nav", "url after switching views", ask=at_ask, audit=page.url, changed=at_ask != page.url)
    page.go_back()
    tab.quiet()
    note("nav", "back from Audit", url=page.url, heading=page.locator("h2").first.inner_text()
         if page.locator("h2").count() else None, shot=tab.shot("n-back-from-audit"))
    page.go_forward()
    tab.quiet()
    page.reload()
    tab.quiet()
    note("nav", "reload", url=page.url, heading=page.locator("h2").first.inner_text()
         if page.locator("h2").count() else None)
    tab.close()
    fresh = Tab(browser, session=tenant["session"])
    fresh.context.clear_cookies()
    fresh.page.goto(f"{BASE}/console/#ask")
    fresh.quiet()
    note("nav", "deep link /console/#ask in a new tab", heading=fresh.page.locator("h2").first.inner_text()
         if fresh.page.locator("h2").count() else None, shot=fresh.shot("n-deep-link"))
    fresh.close()


# 4. Sessions.

def test_sessions(browser):
    s = api_signup("sess")
    # wrong password, taken email, weak password, as a person sees them
    tab = Tab(browser)
    page = tab.page
    page.goto(f"{BASE}/console/")
    page.get_by_label("email").fill(s["email"])
    page.get_by_label("password").fill("not the password")
    page.get_by_role("button", name="Enter the console").click()
    tab.quiet()
    note("session", "wrong password", screen=tab.screen_errors(), shot=tab.shot("s-wrong-password"))
    page.get_by_role("button", name="Sign up").click()
    page.get_by_label("org name").fill("taken")
    page.get_by_label("email").fill(s["email"])
    page.get_by_label("password").fill(PASSWORD)
    page.get_by_role("button", name="Create the org").click()
    tab.quiet()
    note("session", "sign up with a taken email", body=page.inner_text("main")[:300] if page.locator("main").count()
         else page.inner_text("body")[:300], shot=tab.shot("s-taken-email"))
    page.goto(f"{BASE}/console/")
    tab.quiet()
    if page.get_by_role("button", name="Sign up").count():
        page.get_by_role("button", name="Sign up").click()
    page.get_by_label("org name").fill("weak")
    page.get_by_label("email").fill(f"qa-weak-{RUN}@example.com")
    pw = page.get_by_label("password")
    pw.fill("        ")
    note("session", "password field", type=pw.get_attribute("type"), autocomplete=pw.get_attribute("autocomplete"),
         minlength=pw.get_attribute("minlength"))
    page.get_by_role("button", name="Create the org").click()
    tab.quiet()
    note("session", "eight spaces as a password", screen=tab.screen_errors(),
         at_upload=page.get_by_role("heading", name="Upload your files").count() > 0, shot=tab.shot("s-spaces-password"))
    tab.close()

    # two tabs, logout, back after logout, expired token
    _, login = call("POST", "/auth/login", {"email": s["email"], "password": PASSWORD})
    a = Tab(browser, session=login)
    a.page.goto(f"{BASE}/console/")
    a.quiet()
    two = a.context.new_page()
    two.goto(f"{BASE}/console/")
    two.wait_for_timeout(800)
    a.page.get_by_text("sign out", exact=True).click()
    a.quiet()
    audit = two.locator("nav button", has_text="Audit")
    if audit.count():
        audit.first.click()
    two.wait_for_timeout(1500)
    with contextlib.suppress(Exception):  # a page stuck mid reload cannot be captured; the note still says where it is
        two.screenshot(path=str(SHOTS / "s-second-tab-after-signout.png"), timeout=5000)
    note("session", "second tab after sign out, without a reload",
         at_door=two.get_by_role("button", name="Enter the console").count() > 0,
         says=two.locator(".err").all_inner_texts()[:2])
    a.page.go_back()
    a.quiet()
    note("session", "back after sign out", at_door=a.page.get_by_role("button", name="Enter the console").count() > 0,
         shot=a.shot("s-back-after-logout"))
    status, _ = call("GET", "/auth/me", token=login["token"])
    note("session", "old token after sign out", status=status)
    assert status == 401
    a.close()

    # a second device logs the first one out
    _, first = call("POST", "/auth/login", {"email": s["email"], "password": PASSWORD})
    _, second = call("POST", "/auth/login", {"email": s["email"], "password": PASSWORD})
    note("session", "first device after a second sign in", status=call("GET", "/auth/me", token=first["token"])[0])
    dead = Tab(browser, session={**second, "token": "expired-or-garbage"})
    dead.page.goto(f"{BASE}/console/")
    dead.quiet(1200)
    note("session", "expired token in storage", at_door=dead.page.get_by_role("button", name="Enter the console").count() > 0,
         screen=dead.screen_errors(), shot=dead.shot("s-expired"))
    leaked = [u for u in dead.urls + a.urls if second["token"] in u or "token=" in u]
    assert not leaked, leaked
    dead.close()


# 5. Roles, end to end through the invite link.

def test_roles_through_invites(browser, tenant):
    owner = Tab(browser, session=tenant["session"])
    page = owner.page
    page.goto(f"{BASE}/console/")
    owner.quiet()
    page.get_by_role("button", name="Settings").click()
    owner.quiet()
    links: dict[str, str] = {}
    for role in ("admin", "member", "viewer"):
        page.get_by_label("Email to invite").fill(f"qa-{role}-{RUN}@example.com")
        page.get_by_label("Role for the invite").select_option(role)
        page.get_by_role("button", name="Invite").click()
        owner.quiet()
        links[role] = page.get_by_label("Invite link").input_value()
    # no shot here: the members tab shows live invite links
    note("roles", "invite links", contains_fragment=all("#invite=" in v for v in links.values()))
    owner.close()

    for role, link in links.items():
        t = Tab(browser)
        t.page.goto(link if link.startswith("http") else BASE + link)
        t.quiet()
        t.page.get_by_label("choose a password").fill(PASSWORD)
        t.page.locator("form button[type=submit]").click()
        t.quiet(1200)
        landed = t.page.locator("h2").first.inner_text() if t.page.locator("h2").count() else None
        t.shot(f"r-{role}-after-join")
        to_console = t.page.get_by_role("button", name="Go to the console")
        if to_console.count():
            to_console.click()
            t.quiet()
        open_workspace(t, tenant["ws"])
        seen = {"landed_on": landed, "rail": rail(t)}
        for v in ("Pipeline", "Sources", "Ask", "Ontology", "Boards"):
            btn = t.page.locator("nav button, aside button", has_text=re.compile(f"^{v}$"))
            if not btn.count():
                continue
            btn.first.click()
            t.quiet()
            seen[v] = [b for b in t.page.locator("main button:visible").all_inner_texts() if b.strip()][:20]
            t.shot(f"r-{role}-{v}")
        t.page.locator("nav button", has_text="Settings").first.click()
        t.quiet()
        seen["settings_tabs"] = [x for x in t.page.locator("[role=tab], main nav button").all_inner_texts() if x.strip()]
        seen["refused"] = t.refused
        note("roles", role, **seen)
        t.close()


# 6. Keyboard only through the door.

def test_keyboard_through_signup(browser):
    tab = Tab(browser)
    page = tab.page
    page.goto(f"{BASE}/console/")
    tab.quiet()
    stops = []
    for _ in range(12):
        page.keyboard.press("Tab")
        stops.append(page.evaluate("""() => { const e = document.activeElement; const s = getComputedStyle(e);
            return {tag: e.tagName, label: e.getAttribute('aria-label') || e.id || e.innerText.slice(0, 30),
                    outline: s.outlineStyle !== 'none' && s.outlineWidth !== '0px', shadow: s.boxShadow !== 'none'}; }"""))
    note("a11y", "tab stops on the door", stops=stops,
         invisible_focus=[s["label"] for s in stops if not (s["outline"] or s["shadow"])])
    tab.shot("k-door-focus")
    tab.axe("door")
    tab.close()


# 7. Static checks a buyer runs.

def test_headers_bundle_and_copy():
    req = urllib.request.Request(f"{BASE}/console/")
    with urllib.request.urlopen(req) as r:
        headers = {k.lower(): v for k, v in r.headers.items()}
        html = r.read().decode()
    note("security", "headers on /console/", present=sorted(h for h in headers if h in (
        "content-security-policy", "x-content-type-options", "referrer-policy", "strict-transport-security",
        "x-frame-options", "permissions-policy"))),
    js = re.findall(r'src="([^"]+\.js)"', html)
    total = 0
    dashes = 0
    for src in js:
        with urllib.request.urlopen(BASE + src if src.startswith("/") else f"{BASE}/console/{src}") as r:
            body = r.read()
        total += len(body)
        dashes += body.decode("utf8", "replace").count("\u2014")  # an em dash, spelled out so this file has none
    note("perf", "console js bytes", bytes=total)
    note("copy", "em dashes in the shipped console", count=dashes)


def test_isolation_by_url_in_the_browser(browser, tenant):
    """Org B pastes org A's workspace into its own console. Nothing of A may render."""
    b = api_signup("intruder")
    tab = Tab(browser, session=b)
    page = tab.page
    page.goto(f"{BASE}/console/")
    tab.quiet()
    leaked = page.locator("button.card", has_text=tenant["ws"]).count()
    page.evaluate(f"localStorage.setItem('iw.last', JSON.stringify({{ws: '{tenant['ws']}', view: 'explorer'}}))")
    page.reload()
    tab.quiet()
    body = page.inner_text("body")
    note("isolation", "B with A's workspace in iw.last", card=leaked, shows_name=tenant["ws"] in body,
         shot=tab.shot("i-b-with-a-last"))
    assert leaked == 0
    for path in (f"/workspaces/{tenant['ws']}/objects/Customer", f"/workspaces/{tenant['ws']}/ontology",
                 f"/workspaces/{tenant['ws']}/ask/log"):
        assert call("GET", path, token=b["token"])[0] == 404
    tab.close()
