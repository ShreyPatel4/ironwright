"""The whole journey on a running stack, then org B trying to reach all of it.

    python tests/e2e/journey.py --base http://127.0.0.1:8000 --wait 180 --budget 600

Standard library only, so it runs anywhere. The same steps run in process in pytest.

Isolation always runs, after however far the walk got, and it builds whatever it needs
for itself. It has no flag and no setting, and it cannot go on the blocked list. A run
without the line `ISOLATION ok` is a failed run.

BLOCKED holds steps that cannot pass yet because the piece belongs to another ticket.
It is strict: a listed step that passes fails the run, so the list cannot go stale.
The ticket is done when this list is empty and the walk prints `JOURNEY complete`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

HERE = Path(__file__).resolve().parent
# Two files with a clear key each and a clear join, so the proposal has something to say.
UPLOADS = HERE.parent / "fixtures" / "ingest" / "join"
NOT_FOUND = "no such resource"

# Every route the app scopes by a path param. test_isolation_cannot_be_skipped.py holds
# this equal to the live app, so a new route is red until it is swept here too.
ISOLATION_ROUTES: list[tuple[str, str]] = [tuple(r) for r in json.loads((HERE / "isolation_routes.json").read_text())]
MIN_ISOLATION_ROUTES = 50

BLOCKED: dict[str, str] = {
    "profile_job": "no ticket yet: POST /jobs answers 404 for a workspace that does not exist, and a profile job runs before one can",
    "accept_proposal": "no ticket owns it: no route calls workspaces_tenant.create_workspace, and GET /workspaces never lists one",
    "ontology_accept": "W2: the proposal and accept routes",
    "ask": "W2: _pack() in platform_api answers 404 for anything that is not a demo pack",
}


@dataclass
class Answer:
    status: int
    raw: bytes

    def json(self) -> Any:
        return json.loads(self.raw)


class Stack(Protocol):
    def call(self, method: str, path: str, *, body: Any = None, files: list[tuple[str, bytes]] | None = None,
             token: str | None = None, timeout: float = 60.0) -> Answer: ...

    def pump(self, job_id: str) -> None:
        """Give a queued job a chance to move."""


class Broken(Exception):
    def __init__(self, step: str, detail: Any) -> None:
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


@dataclass
class State:
    tag: str
    password: str
    deadline: float
    workspace: str
    a_token: str | None = None
    a_org_slug: str | None = None
    b_token: str | None = None
    upload_id: str | None = None
    proposal: dict[str, Any] | None = None
    workspace_made: bool = False
    isolation_workspace: str | None = None
    isolation_job: str | None = None
    answer: dict[str, Any] = field(default_factory=dict)


def fresh_state(budget: float = 600.0) -> State:
    tag = uuid.uuid4().hex[:10]
    return State(tag=tag, password=uuid.uuid4().hex, deadline=time.monotonic() + budget, workspace=f"e2e-{tag}")


def expect(stack: Stack, method: str, path: str, status: int, step: str, *, token: str | None = None,
           body: Any = None, files: list[tuple[str, bytes]] | None = None, timeout: float = 60.0) -> Any:
    got = stack.call(method, path, body=body, files=files, token=token, timeout=timeout)
    if got.status != status:
        raise Broken(step, f"{method} {path} answered {got.status}, wanted {status}: {got.raw[:300]!r}")
    return got.json()


def _signup(stack: Stack, state: State, label: str, step: str) -> dict[str, Any]:
    return expect(stack, "POST", "/auth/signup", 201, step, body={
        "email": f"e2e-{label}-{state.tag}@example.com", "password": state.password,
        "org_name": f"E2E {label} {state.tag}"})


def _wait_job(stack: Stack, state: State, job_id: str, step: str) -> dict[str, Any]:
    while time.monotonic() < state.deadline:
        job = expect(stack, "GET", f"/jobs/{job_id}", 200, step, token=state.a_token)
        if job["state"] == "succeeded":
            return job
        if job["state"] in ("failed", "cancelled"):
            raise Broken(step, f"job {job['state']}: {job.get('error')}")
        stack.pump(job_id)
    raise Broken(step, f"job {job_id} did not finish inside the budget")


def _job(stack: Stack, state: State, kind: str, workspace: str, params: dict[str, Any], step: str) -> str:
    return expect(stack, "POST", "/jobs", 200, step, token=state.a_token,
                  body={"kind": kind, "workspace": workspace, "params": params})["id"]


# The steps, in the order a customer takes them.

def signup_a(stack: Stack, state: State) -> None:
    a = _signup(stack, state, "a", "signup_a")
    state.a_token, state.a_org_slug = a["token"], a["org_slug"]


def upload(stack: Stack, state: State) -> None:
    files = [(p.name, p.read_bytes()) for p in sorted(UPLOADS.glob("*.csv"))]
    state.upload_id = expect(stack, "POST", "/uploads", 201, "upload", token=state.a_token, files=files)["id"]


def profile_job(stack: Stack, state: State) -> None:
    _wait_job(stack, state, _job(stack, state, "profile", state.workspace, {"upload_id": state.upload_id}, "profile_job"),
              "profile_job")
    got = expect(stack, "GET", f"/uploads/{state.upload_id}/proposal", 200, "profile_job", token=state.a_token)
    state.proposal = got["workspace"]
    if len(state.proposal["sources"]) != 2:
        raise Broken("profile_job", f"wanted 2 proposed sources, got {state.proposal['sources']}")


def workspace_is_listed(stack: Stack, state: State) -> None:
    names = [w["name"] for w in expect(stack, "GET", "/workspaces", 200, "accept_proposal", token=state.a_token)]
    if state.workspace not in names:
        raise Broken("accept_proposal", f"{state.workspace} is not in the org's own list: {names}")


def accept_proposal(stack: Stack, state: State) -> None:
    """The route is my ask, not a contract: nothing makes a contract 3 workspace over HTTP yet.
    POST /workspaces makes a scenario organization, which is a different thing."""
    expect(stack, "POST", f"/uploads/{state.upload_id}/accept", 201, "accept_proposal", token=state.a_token,
           body={"workspace": state.workspace, "spec": state.proposal})
    state.workspace_made = True
    workspace_is_listed(stack, state)


def run_job(stack: Stack, state: State) -> None:
    job = _wait_job(stack, state, _job(stack, state, "run", state.workspace, {}, "run_job"), "run_job")
    if not (job.get("result") or {}).get("evals", {}).get("passed"):
        raise Broken("run_job", f"the run finished with evals not passed: {job.get('result')}")
    state.workspace_made = True


def ontology_accept(stack: Stack, state: State) -> None:
    base = f"/workspaces/{state.workspace}/ontology"
    proposal = expect(stack, "GET", f"{base}/proposal", 200, "ontology_accept", token=state.a_token)
    if not proposal["objects"]:
        raise Broken("ontology_accept", "the proposal holds no objects")
    expect(stack, "POST", f"{base}/accept", 200, "ontology_accept", token=state.a_token,
           body={"proposal_id": proposal["id"], "edits": {}})


def ask(stack: Stack, state: State) -> None:
    """The question comes from the server's own examples, so this does not pin W2's names."""
    base = f"/workspaces/{state.workspace}/ask"
    examples = expect(stack, "GET", f"{base}/examples", 200, "ask", token=state.a_token)
    if not examples:
        raise Broken("ask", "the accepted ontology offers no question")
    entry = expect(stack, "POST", base, 200, "ask", token=state.a_token, body={"question": examples[0]["question"]})
    gathered = {c["evidence_id"] for c in entry.get("calls", [])}
    if entry.get("status") != "answered" or "[ev:" not in entry.get("answer", ""):
        raise Broken("ask", f"not a grounded answer: {entry}")
    if not entry.get("citations") or not set(entry["citations"]) <= gathered:
        raise Broken("ask", f"citations {entry.get('citations')} are not ids it gathered: {gathered}")
    state.answer = entry


STEPS: list[tuple[str, Callable[[Stack, State], None]]] = [
    ("signup_a", signup_a), ("upload", upload), ("profile_job", profile_job), ("accept_proposal", accept_proposal),
    ("run_job", run_job), ("ontology_accept", ontology_accept), ("ask", ask),
]


# Isolation. Nothing above decides whether this runs.

def _fill(template: str, state: State, workspace: str) -> str:
    values = {"name": workspace, "slug": state.a_org_slug, "upload_id": state.upload_id, "job_id": state.isolation_job}
    # A boards {slug} sits behind {name}, and the gate answers before it matters.
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: values.get(m.group(1)) or "1", template)


def _controls(stack: Stack, state: State) -> None:
    """If org A cannot read its own things, a 404 for org B proves nothing."""
    for path in (f"/uploads/{state.upload_id}", f"/jobs/{state.isolation_job}",
                 f"/workspaces/{state.isolation_workspace}/organization"):
        expect(stack, "GET", path, 200, "isolation", token=state.a_token)


def _sweep(stack: Stack, state: State, workspace: str) -> tuple[int, list[str]]:
    leaks = []
    for method, template in ISOLATION_ROUTES:
        path = _fill(template, state, workspace)
        body = {} if method in ("POST", "PUT", "PATCH") else None
        got = stack.call(method, path, body=body, token=state.b_token)
        try:
            same = got.status == 404 and got.json() == {"detail": NOT_FOUND}
        except ValueError:
            same = False
        if not same:
            leaks.append(f"{method} {path} answered {got.status}: {got.raw[:120]!r}")
    return len(ISOLATION_ROUTES), leaks


def _b_profile_error(stack: Stack, state: State, workspace: str, upload_id: str) -> str | None:
    """Org B profiles an upload by id, on a workspace of its own. None means it ran."""
    step = "isolation"
    job_id = expect(stack, "POST", "/jobs", 200, step, token=state.b_token,
                    body={"kind": "profile", "workspace": workspace, "params": {"upload_id": upload_id}})["id"]
    while time.monotonic() < state.deadline:
        job = expect(stack, "GET", f"/jobs/{job_id}", 200, step, token=state.b_token)
        if job["state"] == "succeeded":
            return None
        if job["state"] in ("failed", "cancelled"):
            return str(job.get("error"))
        stack.pump(job_id)
    raise Broken(step, f"org B's job {job_id} did not finish inside the budget")


def _names_an_upload_of_a(stack: Stack, state: State) -> list[str]:
    """The gate cannot see params. A's upload has to read exactly like an id nobody has."""
    workspace = expect(stack, "POST", "/workspaces", 201, "isolation", token=state.b_token, timeout=120,
                       body={"name": f"isob-{state.tag}", "size": 4, "seed": 3})["name"]
    theirs = _b_profile_error(stack, state, workspace, state.upload_id)
    ghost = _b_profile_error(stack, state, workspace, uuid.uuid4().hex)
    if theirs is None or theirs != ghost:
        return [f"a job naming an upload of org A answered {theirs!r}, and an id nobody has answered {ghost!r}"]
    return []


def isolation(stack: Stack, state: State) -> int:
    """Returns how many requests org B made and had refused. Raises Broken on any leak."""
    step = "isolation"
    if len(ISOLATION_ROUTES) < MIN_ISOLATION_ROUTES:
        raise Broken(step, f"only {len(ISOLATION_ROUTES)} routes to sweep, wanted at least {MIN_ISOLATION_ROUTES}")
    if state.a_token is None:
        signup_a(stack, state)
    if state.upload_id is None:
        upload(stack, state)
    made = expect(stack, "POST", "/workspaces", 201, step, token=state.a_token, timeout=120,
                  body={"name": f"iso-{state.tag}", "size": 4, "seed": 3})
    state.isolation_workspace = made["name"]
    state.isolation_job = _job(stack, state, "profile", state.isolation_workspace,
                               {"upload_id": state.upload_id}, step)
    b = _signup(stack, state, "b", step)
    state.b_token = b["token"]
    if expect(stack, "GET", "/auth/me", 200, step, token=state.b_token)["org_id"] == \
            expect(stack, "GET", "/auth/me", 200, step, token=state.a_token)["org_id"]:
        raise Broken(step, "the two signups landed in one org")
    _controls(stack, state)

    checked, leaks = _sweep(stack, state, state.isolation_workspace)
    if state.workspace_made:
        more, found = _sweep(stack, state, state.workspace)
        checked, leaks = checked + more, leaks + found

    # What a path cannot show: a listing, and a workspace named in a body.
    secrets = [state.isolation_workspace, state.isolation_job, state.upload_id, state.workspace, state.a_org_slug]
    for path in ("/workspaces", "/jobs"):
        raw = stack.call("GET", path, token=state.b_token).raw.decode(errors="replace")
        leaks += [f"GET {path} names {s}" for s in secrets if s in raw]
    for workspace in (state.isolation_workspace, state.workspace if state.workspace_made else None):
        if workspace:
            got = stack.call("POST", "/jobs", token=state.b_token,
                             body={"kind": "run", "workspace": workspace, "params": {}})
            if got.status != 404:
                leaks.append(f"POST /jobs naming {workspace} answered {got.status}")
    leaks += _names_an_upload_of_a(stack, state)
    if leaks:
        raise Broken(step, f"{len(leaks)} leaks: " + "; ".join(leaks[:10]))
    _controls(stack, state)  # a write that answered 404 and landed anyway shows up here
    return checked


def _walk(stack: Stack, state: State, blocked: dict[str, str]) -> int:
    for name, fn in STEPS:
        try:
            fn(stack, state)
        except Broken as e:
            if name in blocked:
                print(f"JOURNEY incomplete, stopped at {name} ({blocked[name]})\n  {e}", flush=True)
                return 0
            print(f"JOURNEY broken at {name}: {e}", flush=True)
            return 1
        except Exception as e:  # noqa: BLE001  a dropped connection or bad JSON still has to reach isolation
            print(f"JOURNEY broken at {name}: {type(e).__name__}: {e}", flush=True)
            return 1
        if name in blocked:
            print(f"JOURNEY broken at {name}: it passes now but is still listed in BLOCKED. Remove it.", flush=True)
            return 1
        print(f"ok  {name}", flush=True)
    print("JOURNEY complete", flush=True)
    return 0


def run(stack: Stack, blocked: dict[str, str] | None = None, budget: float = 600.0) -> int:
    blocked = BLOCKED if blocked is None else blocked
    if "isolation" in blocked:
        raise ValueError("isolation cannot be blocked")
    state = fresh_state(budget)
    rc = _walk(stack, state, blocked)
    try:
        print(f"ISOLATION ok routes={isolation(stack, state)}", flush=True)
    except Exception as e:  # noqa: BLE001  anything short of the proof is a failure
        print(f"ISOLATION broken: {e}", flush=True)
        rc = 1
    return rc


class Http:
    """Stack over a real server. A worker process moves the jobs, so pump only waits."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, *, body: Any = None, files: list[tuple[str, bytes]] | None = None,
             token: str | None = None, timeout: float = 60.0) -> Answer:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif files is not None:
            boundary = uuid.uuid4().hex
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n\r\n'.encode()
                     + raw + b"\r\n" for name, raw in files]
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Answer(r.status, r.read())
        except urllib.error.HTTPError as e:
            return Answer(e.code, e.read())

    def pump(self, job_id: str) -> None:
        time.sleep(1)


def wait_ready(stack: Http, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if stack.call("GET", "/readyz", timeout=6).status == 200:
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--wait", type=float, default=180, help="seconds to wait for /readyz")
    ap.add_argument("--budget", type=float, default=600, help="seconds the journey's jobs get, in all")
    return ap


def main() -> int:
    args = parser().parse_args()
    stack = Http(args.base)
    if not wait_ready(stack, args.wait):
        print(f"JOURNEY broken: {args.base} was not ready after {args.wait}s", flush=True)
        return 1
    return run(stack, budget=args.budget)


if __name__ == "__main__":
    sys.exit(main())
