"""Walk a running hosted stack from the outside, as signed-up orgs, with the door closed.
Standard library only, so it runs anywhere. It prints one line per step and exits
non-zero at the first break.

    python deploy/hosted_smoke.py --base http://127.0.0.1:8000 --wait 180
    python deploy/hosted_smoke.py --base https://coconutlabs.org --base-path /projects/ironwright/app

--base-path is the prefix the server answers under (IRONWRIGHT_BASE_PATH). Every call
goes under it, so a link the app builds without the prefix breaks here, not for a customer.

Where signups are open, the walk is signup, upload, profile job, create workspace from
the proposal, run job. POST /jobs only takes a workspace the org can already see, and an
org has none before its first profile, so the walk makes a scenario workspace first.

Where signups are by invite or closed (prod), nobody can sign up, so the walk is what a
visitor can do: the door refuses a new org, the demo door opens a viewer seat, the seat
reads the demo packs and cannot write.

Ask comes after wave 2 lands. The demo packs must be built first (`ironwright run
--pack all` in the container), because a tenant can only read them. The last line
is `tenant <org_id> <slug>`, for CI to find the tenant's tables.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

PACKS = ("retail", "supply_chain", "banking_aml", "insurance_claims")
FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ingest" / "join"
TENANT_SLUG = "smoke-sales"


def call(base: str, path: str, body: Any = None, token: str | None = None, timeout: float = 60,
         raw: bytes | None = None, content_type: str = "application/json") -> tuple[int, bytes]:
    data = raw if raw is not None else None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def step(name: str, ok: bool, detail: Any = "") -> None:
    print(f"{'ok' if ok else 'BROKEN'}  {name}  {detail}", flush=True)
    if not ok:
        sys.exit(1)


def wait_ready(base: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    last: Any = "no answer"
    while time.monotonic() < deadline:
        try:
            status, raw = call(base, "/readyz", timeout=6)
            last = raw.decode(errors="replace")
            if status == 200:
                step("/readyz", json.loads(raw)["checks"] == {"db": "ok", "object_store": "ok"}, last)
                return
        except OSError as e:
            last = type(e).__name__
        time.sleep(2)
    step("/readyz", False, f"not ready after {seconds}s: {last}")


def signup(base: str, label: str) -> dict[str, Any]:
    tag = uuid.uuid4().hex[:10]
    status, raw = call(base, "/auth/signup", {"email": f"smoke-{tag}@example.com", "password": uuid.uuid4().hex,
                                              "org_name": f"Smoke {label} {tag}"})
    step(f"signup org {label}", status == 201, raw[:200] if status != 201 else "")
    return json.loads(raw)


def upload(base: str, token: str, folder: pathlib.Path) -> tuple[int, bytes]:
    boundary = uuid.uuid4().hex
    parts = []
    for f in sorted(folder.iterdir()):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{f.name}"\r\n'
                     "Content-Type: text/csv\r\n\r\n".encode() + f.read_bytes() + b"\r\n")
    return call(base, "/uploads", token=token, raw=b"".join(parts) + f"--{boundary}--\r\n".encode(),
                content_type=f"multipart/form-data; boundary={boundary}")


def run_job(base: str, token: str, kind: str, workspace: str, params: dict[str, Any], seconds: float) -> dict[str, Any]:
    """Queue it, and poll until the worker is done. Nothing in the API runs it."""
    status, raw = call(base, "/jobs", {"kind": kind, "workspace": workspace, "params": params}, token=token)
    step(f"{kind} job queued", status == 200, raw[:200] if status != 200 else "")
    job_id = json.loads(raw)["id"]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = json.loads(call(base, f"/jobs/{job_id}", token=token)[1])
        if job["state"] in ("succeeded", "failed", "cancelled"):
            step(f"{kind} job succeeded", job["state"] == "succeeded", job["error"] or "")
            return job
        time.sleep(1)
    step(f"{kind} job finished", False, f"still {job['state']} after {seconds}s. Is the worker up?")
    raise AssertionError("unreachable")


def walk_a_tenant(base: str, a: dict[str, Any], b: dict[str, Any], scenario: str, job_seconds: float) -> None:
    status, raw = upload(base, a["token"], FIXTURE)
    step("org a uploads two files", status == 201, raw[:200] if status != 201 else "")
    upload_id = json.loads(raw)["id"]

    job = run_job(base, a["token"], "profile", scenario, {"upload_id": upload_id}, job_seconds)
    status, raw = call(base, f"/uploads/{upload_id}/proposal", token=a["token"])
    spec = json.loads(raw)["workspace"] if status == 200 else {}
    step("the profile proposes both files", len(spec.get("sources", [])) == 2 == job["result"]["sources"], raw[:200] if not spec else "")

    # The tenant approves the proposal as it stands.
    status, raw = call(base, "/workspaces", {"from_upload": upload_id, "spec": spec, "slug": TENANT_SLUG},
                       token=a["token"], timeout=120)
    step("org a creates the workspace from the proposal", status == 201, raw[:300] if status != 201 else "")

    job = run_job(base, a["token"], "run", TENANT_SLUG, {}, job_seconds)
    passed = job["result"]["evals"]["passed"] is True
    step("the run passed its evals", passed, "" if passed else job["result"]["layers"])

    nobody = uuid.uuid4().hex
    for path in (f"/uploads/{upload_id}", f"/uploads/{upload_id}/proposal", f"/jobs/{job['id']}", f"/jobs/{job['id']}/log"):
        status, raw = call(base, path, token=b["token"])
        same = call(base, path.replace(upload_id, nobody).replace(job["id"], nobody), token=b["token"])[1]
        step(f"org b gets the same 404 as an unknown id on {path.replace(upload_id, '<id>').replace(job['id'], '<id>')}",
             status == 404 and raw == same, status)
    status = call(base, "/jobs", {"kind": "run", "workspace": TENANT_SLUG, "params": {}}, token=b["token"])[0]
    step("org b cannot run it", status == 404, status)
    print(f"tenant {a['org_id']} {TENANT_SLUG}", flush=True)


def evals_of(base: str, pack: str, token: str, seconds: float) -> tuple[int, bytes]:
    """A fresh container builds the demo packs after it is up, so a pack can be missing for
    a while. Wait for it as long as we were told to, then say what we got."""
    deadline = time.monotonic() + seconds
    while True:
        status, raw = call(base, f"/workspaces/{pack}/reports/evals", token=token)
        if status == 200 or time.monotonic() >= deadline:
            return status, raw
        time.sleep(5)


def walk_a_visitor(base: str, door: dict[str, Any], packs: list[str], packs_wait: float) -> None:
    status = call(base, "/auth/signup", {"email": f"smoke-{uuid.uuid4().hex[:10]}@example.com",
                                         "password": uuid.uuid4().hex, "org_name": "Smoke"})[0]
    step("a new org is refused", status == 403, status)
    step("the server has a demo door", door.get("demo") is True, door)
    status, raw = call(base, "/auth/demo", {})
    seat = json.loads(raw) if status == 200 else {}
    step("the demo door opens a viewer seat", seat.get("role") == "viewer", raw[:200])
    token = seat["token"]
    status, raw = call(base, "/workspaces", token=token)
    names = {w["name"] for w in json.loads(raw)} if status == 200 else set()
    step("a visitor sees the demo packs", set(packs) <= names, sorted(names))
    for pack in packs:
        status, raw = evals_of(base, pack, token, packs_wait)
        step(f"a visitor reads evals {pack}", status == 200 and json.loads(raw).get("passed") is True,
             raw[:200] if status != 200 else "")
    refused = [call(base, "/workspaces", {"name": "smoke", "size": 4, "seed": 3}, token=token)[0],
               call(base, f"/workspaces/{packs[0]}/runs", {}, token=token)[0]]
    step("a visitor cannot write", refused == [403, 403], refused)
    call(base, "/auth/logout", {}, token=token)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--base-path", default="", help="the prefix the server answers under, such as /projects/ironwright/app")
    ap.add_argument("--wait", type=float, default=180, help="seconds to wait for /readyz")
    ap.add_argument("--packs", nargs="+", default=list(PACKS))
    ap.add_argument("--packs-wait", type=float, default=0, help="seconds to wait for the demo packs to be built")
    ap.add_argument("--job-seconds", type=float, default=300, help="seconds to wait for each job")
    args = ap.parse_args()
    prefix = "/" + args.base_path.strip("/") if args.base_path.strip("/") else ""
    base = args.base.rstrip("/") + prefix

    wait_ready(base, args.wait)
    step("/healthz", call(base, "/healthz")[0] == 200)
    for page in ("/console/", "/console"):  # Next.js strips the slash before it rewrites
        status, raw = call(base, page)
        step(f"console is built in at {page}", status == 200 and b"<html" in raw.lower(), status)
        step(f"the console page knows its prefix at {page}", f'<base href="{prefix}/console/">'.encode() in raw)

    door = json.loads(call(base, "/auth/status")[1])
    step("the door is closed", door["dev_open"] is False)
    step("no token, no workspaces", call(base, "/workspaces")[0] == 401)
    step(f"signup is {door.get('signup', 'open')}", True)
    if door.get("signup", "open") != "open":
        walk_a_visitor(base, door, args.packs, args.packs_wait)
        print("smoke passed", flush=True)
        return
    a, b = signup(base, "a"), signup(base, "b")
    status, raw = call(base, "/auth/me", token=a["token"])
    step("a token is one org", status == 200 and json.loads(raw)["org_id"] == a["org_id"] != b["org_id"])

    for pack in args.packs:
        status, raw = evals_of(base, pack, a["token"], args.packs_wait)
        step(f"evals {pack}", status == 200 and json.loads(raw).get("passed") is True, raw[:200] if status != 200 else "")

    pack = args.packs[0]
    status, raw = call(base, f"/workspaces/{pack}/lake/tables", token=a["token"])
    tables = json.loads(raw) if status == 200 else []
    gold = next((t["name"] for t in tables if t["layer"] == "gold"), None)
    step("a tenant reads the demo lake", gold is not None, f"{len(tables)} tables")
    status, raw = call(base, f"/workspaces/{pack}/lake/query",
                       {"sql": f"select count(*) as n from gold_{gold}"}, token=a["token"])
    step("the lake answers a query", status == 200 and int(json.loads(raw)["rows"][0][0]) > 0, raw[:120])
    step("a tenant cannot run a demo pack", call(base, f"/workspaces/{pack}/runs", {}, token=a["token"])[0] == 403)

    # POST /jobs needs a workspace this org can see. A first profile has no other one.
    status, raw = call(base, "/workspaces", {"name": "smoke", "size": 4, "seed": 3}, token=a["token"], timeout=120)
    step("org a makes a workspace", status == 201, raw[:200] if status != 201 else "")
    ws = json.loads(raw)["name"]
    step("org a sees it", call(base, f"/workspaces/{ws}/organization", token=a["token"])[0] == 200)
    step("org b gets a 404", call(base, f"/workspaces/{ws}/organization", token=b["token"])[0] == 404)

    walk_a_tenant(base, a, b, ws, args.job_seconds)
    print("smoke passed", flush=True)


if __name__ == "__main__":
    main()
