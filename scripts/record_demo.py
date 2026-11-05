"""Record the SaaS demo film by driving the real console on a running stack.

Nothing here stubs a route. Every beat checks what the server really answered, and a beat
that cannot happen is a gap, never a fake. A film with a gap cannot reach media/: a final take
stops at the first one. A rehearsal carries on past them, writes to the folder you name, and
lists them.

    docker compose -f deploy/compose.yaml up --build -d
    docker compose -f deploy/compose.yaml exec -T -e IRONWRIGHT_SEED_PASSWORD=... api \
        python - < scripts/seed_demo.py
    IRONWRIGHT_SEED_PASSWORD=... python scripts/record_demo.py --base http://127.0.0.1:8000
    IRONWRIGHT_SEED_PASSWORD=... python scripts/record_demo.py --rehearse /some/folder

Captions come from scripts/demo_film.json. docs/DEMO-SCRIPT-SAAS.md is the narrative.
Job waits are real and get cut out afterwards, down to a short lead and tail. The sped up
part is only ever a wait, and the film never shows a number the server did not send.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CAPTIONS = ROOT / "scripts" / "demo_film.json"
FILM = ROOT / "media" / "demo" / "ironwright-saas-demo.mp4"
POSTER = ROOT / "media" / "demo" / "ironwright-saas-demo.jpg"
BEATS = ("signup", "upload", "profile", "silver", "run", "ontology",
         "ask", "action", "audit", "isolation", "usage")
WIDTH, HEIGHT = 1440, 900
TARGET_SECONDS = 150
TOLERANCE_SECONDS = 25
SLOW_MS = 180_000  # a real pipeline run sits behind these waits
WORKSPACE = "northwind-policy"
REFUSED_QUESTION = "Which of our customers will leave us next year?"
ACTION_REASON = "Asked for in the demo film"
POSTER_BEAT = "ask"
# The rules take client_key as the agency book's key. policy_nbr is the one the billing file
# points at, and only someone who knows the business can say so. That edit is the beat.
CORRECT_KEY = ("agency_book", "policy_nbr", "client_key")

# One calm easing curve, one panel, pointer events off so it never takes a click from the
# console. It only adds to the page: it hides and changes nothing that is already there.
OVERLAY_JS = """
(() => {
  if (window.__film) return;
  const ease = 'cubic-bezier(.4,0,.2,1)';
  let box, dot;
  function mount() {
    if (box || !document.documentElement) return;
    box = document.createElement('div');
    box.setAttribute('data-film', 'caption');
    box.style.cssText = 'position:fixed;left:50%;bottom:36px;transform:translateX(-50%);z-index:2147483647;'
      + 'max-width:880px;padding:14px 20px;border:1px solid #2F343C;border-radius:6px;background:#1C2127;'
      + 'color:#F6F7F9;font:500 19px/1.4 Geist,"Segoe UI",system-ui,sans-serif;text-align:center;'
      + 'pointer-events:none;opacity:0;transition:opacity 600ms ' + ease;
    dot = document.createElement('div');
    dot.setAttribute('data-film', 'pointer');
    dot.style.cssText = 'position:fixed;left:-20px;top:-20px;width:16px;height:16px;margin:-8px 0 0 -8px;'
      + 'border-radius:50%;background:#2D72D2;border:2px solid #fff;z-index:2147483647;pointer-events:none';
    document.documentElement.append(box, dot);
    addEventListener('mousemove', (e) => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }, true);
  }
  window.__film = { caption(text) { mount(); box.textContent = text; box.style.opacity = text ? '1' : '0'; } };
  if (document.documentElement) mount(); else addEventListener('DOMContentLoaded', mount);
})();
"""


class NotPublishable(RuntimeError):
    """This take may not reach media/."""


class BeatFailed(RuntimeError):
    """What the server or the console really did is not what the beat needs."""


@dataclass(frozen=True)
class Gap:
    beat: str
    why: str


@dataclass(frozen=True)
class Caption:
    text: str
    hold: float


@dataclass(frozen=True)
class Beat:
    id: str
    captions: tuple[Caption, ...]


def load_beats(path: Path = CAPTIONS) -> list[Beat]:
    raw = json.loads(path.read_text())["beats"]
    beats = [Beat(b["id"], tuple(Caption(c["text"], float(c["hold"])) for c in b["captions"])) for b in raw]
    if tuple(b.id for b in beats) != BEATS:
        raise ValueError(f"the beats must run in this order: {', '.join(BEATS)}")
    return beats


def keep_segments(duration: float, waits: list[tuple[float, float]], *, lead: float = 1.5,
                  tail: float = 1.0) -> list[tuple[float, float]]:
    """What stays of the recording. A wait keeps its lead and its tail, and the rest goes."""
    kept: list[tuple[float, float]] = []
    start = 0.0
    for begin, end in sorted(waits):
        if end - begin <= lead + tail:
            continue
        kept.append((start, begin + lead))
        start = end - tail
    kept.append((start, duration))
    return [(a, b) for a, b in kept if a < b]


def film_time(moment: float, kept: list[tuple[float, float]]) -> float:
    """Where a moment of the recording lands in the film. One inside a cut lands on the join."""
    out = 0.0
    for a, b in kept:
        if moment < a:
            return out
        if moment <= b:
            return out + moment - a
        out += b - a
    return out


def check_length(seconds: float) -> None:
    if abs(seconds - TARGET_SECONDS) > TOLERANCE_SECONDS:
        raise NotPublishable(f"the film runs {seconds:.0f} s and the target is 2:30, give or take {TOLERANCE_SECONDS} s")


def check_publishable(gaps: list[Gap], seconds: float) -> None:
    if gaps:
        raise NotPublishable("gaps: " + "; ".join(f"{g.beat}: {g.why}" for g in gaps))
    check_length(seconds)


def rehearsal_dir(path: Path) -> Path:
    path = path.resolve()
    media = (ROOT / "media").resolve()
    if path == media or media in path.parents:
        raise SystemExit("a rehearsal never writes into media/: only a clean final take goes there")
    return path


def gated_action(describe: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    for name, action in describe.get("actions", {}).items():
        if action.get("requires_approval"):
            return name, action.get("params", {})
    raise BeatFailed("this workspace has no approval gated action")


def action_params(params: dict[str, Any], first_id: Callable[[str], str], is_object: Callable[[str], bool]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, ptype in params.items():
        t = ptype["type"] if isinstance(ptype, dict) else ptype
        if is_object(t):
            out[name] = first_id(t)
        elif t == "int":
            out[name] = 1
        elif t in ("str", "string"):
            out[name] = ACTION_REASON
        else:
            raise BeatFailed(f"I do not know how to fill a {t} param for {name}")
    return out


def cut(raw: Path, kept: list[tuple[float, float]], out: Path) -> None:
    """H.264 at 1440x900 that a browser plays straight away."""
    parts = "".join(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[p{i}];" for i, (a, b) in enumerate(kept))
    joined = "".join(f"[p{i}]" for i in range(len(kept)))
    graph = f"{parts}{joined}concat=n={len(kept)}:v=1:a=0,scale={WIDTH}:{HEIGHT},format=yuv420p[v]"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-filter_complex", graph, "-map", "[v]",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", str(out)], check=True)


def cut_poster(film: Path, at: float, out: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(film), "-frames:v", "1",
                    "-q:v", "3", str(out)], check=True)


def duration_of(video: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                                check=True, capture_output=True, text=True).stdout.strip())


class Film:
    """One tenant's walk, in one browser page, on a running stack."""

    def __init__(self, browser: Any, base: str, files: list[Path], rival: tuple[str, str], beats: list[Beat],
                 video_dir: Path) -> None:
        self.base, self.files, self.rival, self.beats = base.rstrip("/"), files, rival, beats
        self.context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, record_video_dir=str(video_dir),
                                           record_video_size={"width": WIDTH, "height": HEIGHT})
        self.context.set_default_timeout(20_000)
        self.context.add_init_script(OVERLAY_JS)
        self.page = self.context.new_page()
        self.started = time.monotonic()
        self.email = f"film-{secrets.token_hex(4)}@northwind-mutual.example"
        self.password = secrets.token_urlsafe(18)
        self.workspace = WORKSPACE
        self.upload_id: str | None = None
        self.waits: list[tuple[float, float]] = []
        self.marks: dict[str, float] = {}
        self.refused: list[str] = []
        self.errors: list[str] = []
        self.refusals_expected = False
        self._captions: list[Caption] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("response", self._saw)

    # -------------------------------------------------------------- how it moves

    def _saw(self, response: Any) -> None:
        if response.status >= 400 and not self.refusals_expected:
            self.refused.append(f"{response.status} {response.request.method} {response.url}")
        if response.request.method == "POST" and response.url.endswith("/uploads") and response.status == 201:
            self.upload_id = response.json()["id"]

    def now(self) -> float:
        return time.monotonic() - self.started

    def _hold(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)

    def say(self) -> None:
        """The next caption of this beat, held as long as the script says."""
        caption = self._captions.pop(0)
        self.page.evaluate("t => window.__film.caption(t)", caption.text)
        self._hold(caption.hold)

    def _quiet(self) -> None:
        self.page.evaluate("() => window.__film.caption('')")
        self._hold(0.6)

    def click(self, target: Any, *, timeout: float | None = None) -> None:
        """The pointer travels there first, at one calm speed, so a viewer can follow it."""
        target.wait_for(state="visible", timeout=timeout)
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=24)
        self._hold(0.3)
        target.click()

    def waiting(self, what: Callable[[], Any]) -> Any:
        """A real wait, which the cut takes out later."""
        begin = self.now()
        out = what()
        self.waits.append((begin, self.now()))
        return out

    def button(self, name: str, **kw: Any) -> Any:
        return self.page.get_by_role("button", name=name, **kw)

    def heading(self, name: str) -> Any:
        return self.page.get_by_role("heading", name=name)

    def _see(self, target: Any, timeout: float | None = None) -> None:
        target.wait_for(state="visible", timeout=timeout)

    def scroll_to(self, target: Any) -> None:
        target.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
        self._hold(0.9)

    def api(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """Through the browser's own session, the same door the console uses."""
        return tuple(self.page.evaluate(
            """async ([method, path, body]) => {
              const token = JSON.parse(localStorage.getItem('iw.session')).token;
              const r = await fetch(path, {method, headers: {Authorization: 'Bearer ' + token,
                ...(body ? {'Content-Type': 'application/json'} : {})}, body: body ? JSON.stringify(body) : undefined});
              return [r.status, await r.json().catch(() => null)];
            }""", [method, path, body]))

    def sign_in(self, email: str, password: str) -> None:
        self.page.get_by_label("email").fill(email)
        self.page.get_by_label("password").fill(password)
        self.click(self.button("Enter the console"))

    def sign_out(self) -> None:
        wizard = self.button("Sign out")
        if wizard.count():
            self.click(wizard)
        else:
            self.click(self.page.get_by_role("link", name="sign out"))
        self._see(self.button("Enter the console"))

    def open_workspace(self) -> None:
        """From wherever the walk is to this workspace's views in the rail."""
        leave = self.button("Go to the console")
        if leave.count():
            self.click(leave)
        elif self.page.get_by_role("button", name="Workspaces", exact=True).count():
            self.click(self.page.get_by_role("button", name="Workspaces", exact=True))
        card = self.page.locator("button.card").filter(has_text=self.workspace_title())
        self.click(card.first)

    def workspace_title(self) -> str:
        status, got = self.api("GET", "/workspaces")
        if status != 200:
            raise BeatFailed(f"GET /workspaces answered {status}")
        for w in got:
            if w["name"] == self.workspace:
                return w["title"]
        raise BeatFailed(f"{self.workspace} is not in this org's workspace list")

    def rail(self, label: str) -> None:
        self.click(self.page.locator("nav.rail").get_by_role("button", name=label, exact=True))
        self._hold(0.4)
        self._no_error_shown(label)

    def _no_error_shown(self, where: str) -> None:
        shown = self.page.locator(".err")
        if shown.count():
            raise BeatFailed(f"{where} shows an error: {shown.first.inner_text()[:160]}")

    # -------------------------------------------------------------- the beats

    def beat_signup(self) -> None:
        self.page.goto(f"{self.base}/console/")
        self.click(self.button("Sign up"))
        self.page.get_by_label("org name").press_sequentially("Northwind Mutual", delay=60)
        self.page.get_by_label("email").press_sequentially(self.email, delay=25)
        self.page.get_by_label("password").fill(self.password)
        self.say()
        self.click(self.button("Create the org"))
        self._see(self.heading("Upload your files"))

    def beat_upload(self) -> None:
        self.page.get_by_label("Choose files").set_input_files([str(f) for f in self.files])
        self.say()
        with self.page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/profile")) as started:
            self.click(self.button("Upload and profile"))
        if started.value.status != 202:
            raise BeatFailed(f"the profile answered {started.value.status}")
        self._see(self.heading("Profile"))

    def beat_profile(self) -> None:
        go_on = self.button("Continue to Map")
        self.waiting(lambda: go_on.wait_for(state="visible", timeout=SLOW_MS))
        self.say()
        joins = self.heading("Candidate joins")
        self.scroll_to(joins)
        if not self.page.get_by_text(CORRECT_KEY[1]).count():  # the caption says I found a join
            raise BeatFailed(f"the profile lists no join on {CORRECT_KEY[1]}")
        self.say()
        self.click(go_on)
        self._see(self.heading("Map"))

    def beat_silver(self) -> None:
        decided = self.page.get_by_text("rows come out of").first
        self.waiting(lambda: decided.wait_for(state="visible", timeout=SLOW_MS))
        self.scroll_to(decided)
        self.say()
        source, column, guessed = CORRECT_KEY
        fix = self.page.get_by_label(f"key {source}.{column}")
        self.scroll_to(fix)
        self.click(self.page.get_by_label(f"key {source}.{guessed}"))
        self.click(fix)
        self.say()
        name = self.page.get_by_label("workspace name")
        self.scroll_to(name)
        name.press_sequentially(self.workspace, delay=50)
        self.click(self.button("Create the workspace"))
        self._see(self.heading("Run"))

    def beat_run(self) -> None:
        self.click(self.button("Run the pipeline"))
        self.say()
        go_on = self.button("Continue to Ontology")
        self.waiting(lambda: go_on.wait_for(state="visible", timeout=SLOW_MS))
        self.say()
        self.click(go_on)
        self._see(self.heading("Ontology"))

    def beat_ontology(self) -> None:
        with self.page.expect_response(lambda r: r.url.endswith("/ontology/proposal"), timeout=SLOW_MS) as read:
            self.click(self.button("Propose an ontology"))
            begin = self.now()
        self.waits.append((begin, self.now()))
        proposal = read.value.json()
        if not proposal.get("objects"):
            raise BeatFailed(f"the proposal holds no objects: {json.dumps(proposal.get('skipped'))[:300]}")
        if not proposal.get("links"):  # the caption says objects and links
            raise BeatFailed(f"the proposal holds no links: {json.dumps(proposal.get('skipped'))[:300]}")
        accept = self.button("Accept this ontology")
        accept.wait_for(state="visible", timeout=SLOW_MS)
        self.scroll_to(self.page.get_by_role("heading", name="Links"))
        self.say()
        self.click(accept)
        self._quiet()
        ask = self.heading("Ask")
        self.waiting(lambda: ask.wait_for(state="visible", timeout=SLOW_MS))

    def _asked(self, send: Callable[[], None]) -> dict[str, Any]:
        with self.page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/ask"),
                                       timeout=SLOW_MS) as asked:
            send()
        if asked.value.status != 200:
            raise BeatFailed(f"Ask answered {asked.value.status}")
        return asked.value.json()

    def beat_ask(self) -> None:
        examples = self.page.get_by_label("example questions").get_by_role("button")
        self.waiting(lambda: examples.first.wait_for(state="visible", timeout=SLOW_MS))
        if examples.count() < 2:
            raise BeatFailed(f"I need two example questions and got {examples.count()}")
        for i in range(2):
            answer = self._asked(lambda i=i: self.click(examples.nth(i)))
            gathered = {c["evidence_id"] for c in answer.get("calls", [])}
            if answer["status"] != "answered" or not answer.get("citations") or not set(answer["citations"]) <= gathered:
                raise BeatFailed(f"example {i} was not a cited answer: {json.dumps(answer)[:300]}")
            self._see(self.page.get_by_label("answer"))
            if f"[ev:{answer['citations'][0]}]" not in self.page.get_by_label("answer").inner_text():
                raise BeatFailed("the answer on screen does not show its citation")
            if i == 0:
                self.marks[POSTER_BEAT] = self.now() + 2
            self.say()

        def typed() -> None:
            box = self.page.get_by_label("your question")
            box.fill("")
            box.press_sequentially(REFUSED_QUESTION, delay=35)
            self.click(self.button("Ask", exact=True))
        refused = self._asked(typed)
        if refused["status"] != "refused":
            raise BeatFailed(f"I expected a refusal: {json.dumps(refused)[:300]}")
        self._see(self.page.get_by_text(f"refused · {refused['reason_code']}"))
        self.say()

    def beat_action(self) -> None:
        self.open_workspace()
        self.rail("Approvals")
        status, got = self.api("GET", f"/workspaces/{self.workspace}/ontology")
        if status != 200:
            raise BeatFailed(f"reading the ontology answered {status}")
        describe = got["describe"]
        name, params = gated_action(describe)

        def first_id(type_: str) -> str:
            code, rows = self.api("GET", f"/workspaces/{self.workspace}/objects/{type_}")
            if code != 200 or not rows:
                raise BeatFailed(f"no {type_} to act on: {code}")
            return rows[0]["_id"]
        filled = action_params(params, first_id, lambda t: t in describe["object_types"])
        status, result = self.api("POST", f"/workspaces/{self.workspace}/actions/{name}",
                                  {"params": filled, "idempotency_key": f"film-{secrets.token_hex(4)}"})
        if status != 200 or result.get("status") != "needs_approval":
            raise BeatFailed(f"{name} answered {status}: {json.dumps(result)[:300]}")
        self.click(self.button("Refresh"))
        row = self.page.locator("tbody tr").filter(has_text=name).first
        self._see(row)
        self.say()
        status, me = self.api("GET", "/auth/me")
        self.page.get_by_label("decide as").press_sequentially(me["principal"], delay=20)
        with self.page.expect_response(lambda r: r.url.endswith("/decide")) as decided:
            self.click(row.get_by_role("button", name="Approve"))
        outcome = decided.value.json() if decided.value.status == 200 else {}
        if outcome.get("status") not in ("applied", "applied_pending_sync"):
            raise BeatFailed(f"approve answered {decided.value.status}: {json.dumps(outcome)[:300]}")
        self._see(self.page.get_by_text("last decision:"))
        self.say()

    def beat_audit(self) -> None:
        self.rail("Audit")
        with self.page.expect_response(lambda r: r.url.endswith("/audit/verify")) as verified:
            self.click(self.button("Verify the chain"))
        got = verified.value.json() if verified.value.status == 200 else {}
        if got.get("intact") is not True or not got.get("rows"):
            raise BeatFailed(f"verify answered {verified.value.status}: {json.dumps(got)[:300]}")
        self._see(self.page.get_by_text(f"intact · {got['rows']} rows"))
        self.say()

    def beat_isolation(self) -> None:
        self.sign_out()
        self._quiet()
        self.sign_in(*self.rival)
        self._see(self.button("Go to the console"))
        self.click(self.button("Go to the console"))
        self._see(self.heading("Workspaces"))
        if self.page.get_by_text(self.workspace).count():
            raise BeatFailed("Rival Co can see Northwind's workspace")
        self.say()
        token = self.page.evaluate("() => JSON.parse(localStorage.getItem('iw.session')).token")
        self.refusals_expected = True
        self.context.set_extra_http_headers({"Authorization": f"Bearer {token}"})
        try:
            seen = self.page.goto(f"{self.base}/uploads/{self.upload_id}")
            if seen.status != 404:
                raise BeatFailed(f"Rival Co read Northwind's upload: {seen.status}")
            # The server's CSP refuses a style tag, and it is right to. Setting a property is allowed.
            self.page.evaluate("""line => {
              document.body.style.cssText = 'font:500 22px Menlo,monospace;padding:48px';
              const asked = document.createElement('div');
              asked.textContent = line;
              asked.style.cssText = 'color:#5F6B7C;font-size:16px;margin-bottom:12px';
              document.body.prepend(asked);
            }""", f"Signed in as Rival Co: GET /uploads/{self.upload_id} answered {seen.status}")
            self.say()
        finally:
            self.context.set_extra_http_headers({})
            self.refusals_expected = False

    def beat_usage(self) -> None:
        self.page.goto(f"{self.base}/console/")
        self.page.locator("a, button").filter(has_text="ign out").first.wait_for(state="visible")
        self.sign_out()
        self.sign_in(self.email, self.password)
        self._see(self.button("Go to the console"))
        self.click(self.button("Go to the console"))
        self.click(self.button("Settings"))
        self.click(self.page.get_by_role("tab", name="Usage"))
        self._see(self.page.get_by_text("Your plan is"))
        self.say()
        self.say()
        self._quiet()

    # -------------------------------------------------------------- the take

    def take(self, *, keep_going: bool, shots: Path | None = None) -> list[Gap]:
        gaps: list[Gap] = []
        from playwright.sync_api import Error as PlaywrightError
        for beat in self.beats:
            self.marks[beat.id] = self.now()
            self._captions = list(beat.captions)
            self.refused.clear()
            self.errors.clear()
            try:
                getattr(self, f"beat_{beat.id}")()
                if self.refused or self.errors:
                    raise BeatFailed(f"refused: {self.refused[:3]} page errors: {self.errors[:2]}")
                if self._captions:
                    raise BeatFailed(f"{len(self._captions)} captions were never shown")
            except (BeatFailed, PlaywrightError, KeyError, TypeError) as e:
                gap = Gap(beat.id, " ".join(str(e).split())[:400])
                print(f"GAP {gap.beat}: {gap.why}", flush=True)
                if shots:
                    self.page.screenshot(path=str(shots / f"gap-{beat.id}.png"))
                gaps.append(gap)
                if not keep_going:
                    break
            else:
                print(f"ok  {beat.id} at {self.marks[beat.id]:.0f} s", flush=True)
        return gaps

    def finish(self) -> tuple[Path, float]:
        video = self.page.video
        total = self.now()
        self.context.close()
        return Path(video.path()), total


def _seed_module() -> Any:
    spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "scripts" / "seed_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--rehearse", type=Path, help="carry on past gaps and write here, never into media/")
    a = ap.parse_args(argv)
    password = os.environ.get("IRONWRIGHT_SEED_PASSWORD")
    if not password:
        raise SystemExit("set IRONWRIGHT_SEED_PASSWORD to what scripts/seed_demo.py ran with: Rival Co has to log in")
    out_dir = rehearsal_dir(a.rehearse) if a.rehearse else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    seed = _seed_module()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        files = seed.export_files(Path(tmp) / "exports")
        browser = p.chromium.launch()
        film = Film(browser, a.base, files, (seed.RIVAL, password), load_beats(), Path(tmp) / "video")
        gaps = film.take(keep_going=bool(out_dir), shots=out_dir)
        raw, total = film.finish()
        browser.close()
        kept = keep_segments(total, film.waits)
        seconds = sum(b - a_ for a_, b in kept)
        print(f"recorded {total:.0f} s, film {seconds:.0f} s, gaps {len(gaps)}", flush=True)
        if out_dir:
            cut(raw, kept, out_dir / FILM.name)
            cut_poster(out_dir / FILM.name, film_time(film.marks.get(POSTER_BEAT, 0.0), kept), out_dir / POSTER.name)
            (out_dir / "gaps.json").write_text(json.dumps([g.__dict__ for g in gaps], indent=1))
            print(f"rehearsal in {out_dir}. This take is not the film.", flush=True)
            return 0 if not gaps else 3
        check_publishable(gaps, seconds)
        cut(raw, kept, FILM)
        cut_poster(FILM, film_time(film.marks[POSTER_BEAT], kept), POSTER)
        print(f"{FILM} {duration_of(FILM):.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
