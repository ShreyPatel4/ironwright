"""Walk a running hosted stack from the outside, as signed-up orgs, with the door closed.
Standard library only, so it runs anywhere. It prints one line per step and exits
non-zero at the first break.

    python deploy/hosted_smoke.py --base http://127.0.0.1:8000 --wait 180

The walk is signup, upload, profile job, create workspace, run job. Two steps are
not what a tenant will finally do, and each says so as it runs:

  - No route creates a tenant workspace yet, so an operator creates it inside the
    API container (--operator), from the proposal the profile job wrote.
  - POST /jobs only takes a workspace the org can already see, and an org has none
    before its first profile. So the walk makes a scenario workspace first.

After the run the walk proposes and accepts an ontology, then asks a question. The demo
packs must be built first (`ironwright run --pack all` in the container), because a
tenant can only read them. The last line is `tenant <org_id> <slug>`, for CI to find
the tenant's tables.

--state FILE writes what org a can see to FILE. After the disk is wiped and the api and
worker restart, `--verify FILE` reads it all again and fails on any difference. That is
the durable state proof (docs/deploy-hosted.md).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

PACKS = ("retail", "supply_chain", "banking_aml", "insurance_claims")
FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ingest" / "join"
OPERATOR = "docker compose -f deploy/compose.yaml exec -T api"
TENANT_SLUG = "smoke-sales"
CREATE_WORKSPACE = """
import json, sys
from ironwright.workspaces_tenant import create_workspace
create_workspace(sys.argv[1], sys.argv[2], json.load(sys.stdin))
"""


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
    return wait_job(base, token, kind, json.loads(raw)["id"], seconds)


def wait_job(base: str, token: str, kind: str, job_id: str, seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = json.loads(call(base, f"/jobs/{job_id}", token=token)[1])
        if job["state"] in ("succeeded", "failed", "cancelled"):
            step(f"{kind} job succeeded", job["state"] == "succeeded", job["error"] or "")
            return job
        time.sleep(1)
    step(f"{kind} job finished", False, f"still {job['state']} after {seconds}s. Is the worker up?")
    raise AssertionError("unreachable")


def walk_a_tenant(base: str, a: dict[str, Any], b: dict[str, Any], scenario: str, operator: str,
                  job_seconds: float) -> dict[str, str]:
    status, raw = upload(base, a["token"], FIXTURE)
    step("org a uploads two files", status == 201, raw[:200] if status != 201 else "")
    upload_id = json.loads(raw)["id"]

    job = run_job(base, a["token"], "profile", scenario, {"upload_id": upload_id}, job_seconds)
    status, raw = call(base, f"/uploads/{upload_id}/proposal", token=a["token"])
    spec = json.loads(raw)["workspace"] if status == 200 else {}
    step("the profile proposes both files", len(spec.get("sources", [])) == 2 == job["result"]["sources"], raw[:200] if not spec else "")

    # No route does this yet. An operator approves the proposal as it stands.
    made = subprocess.run([*shlex.split(operator), "python", "-c", CREATE_WORKSPACE, a["org_id"], TENANT_SLUG],
                          input=json.dumps(spec), capture_output=True, text=True, timeout=120, check=False)
    step("an operator creates the workspace (no tenant route yet)", made.returncode == 0, made.stderr[-400:])

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

    run_job(base, a["token"], "ontology_propose", TENANT_SLUG, {}, job_seconds)
    status, raw = call(base, f"/workspaces/{TENANT_SLUG}/ontology/proposal", token=a["token"])
    step("an ontology is proposed", status == 200, raw[:200] if status != 200 else "")
    status, raw = call(base, f"/workspaces/{TENANT_SLUG}/ontology/accept",
                       {"proposal_id": json.loads(raw)["id"], "edits": []}, token=a["token"])
    step("org a accepts it", status == 200, raw[:200] if status != 200 else "")
    wait_job(base, a["token"], "accepted ontology run", json.loads(raw)["job_id"], job_seconds)
    print(f"tenant {a['org_id']} {TENANT_SLUG}", flush=True)
    return {"upload_id": upload_id, "job_id": job["id"]}


def what_org_a_sees(base: str, a: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """Everything the restart must bring back, read through the routes."""
    def get(path: str) -> Any:
        status, raw = call(base, path, token=a["token"])
        step(f"read {path}", status == 200, raw[:200] if status != 200 else "")
        return json.loads(raw) if path != f"/jobs/{ids['job_id']}/log" else raw.decode(errors="replace")

    examples = get(f"/workspaces/{TENANT_SLUG}/ask/examples")
    status, raw = call(base, f"/workspaces/{TENANT_SLUG}/ask", {"question": examples[0]["question"]}, token=a["token"])
    answer = json.loads(raw) if status == 200 else {}
    step("Ask answers from org a's own uploads", answer.get("status") == "answered" and "[ev:" in answer.get("answer", ""),
         raw[:200])
    listed = sorted(w["name"] for w in get("/workspaces") if w.get("kind") != "organization")
    return {"workspaces": listed,
            "upload": get(f"/uploads/{ids['upload_id']}"),
            "proposal": get(f"/uploads/{ids['upload_id']}/proposal"),
            "ontology": get(f"/workspaces/{TENANT_SLUG}/ontology/proposal")["id"],
            "job_log": get(f"/jobs/{ids['job_id']}/log"),
            "answer": [answer["answer"], answer["citations"]],
            "audit_chain": get(f"/orgs/{a['org_slug']}/audit/chain")}


def verify(base: str, path: pathlib.Path) -> None:
    saved = json.loads(path.read_text())
    a, ids = saved["org_a"], saved["ids"]
    status, raw = call(base, "/auth/me", token=a["token"])
    step("org a's session outlived the restart", status == 200 and json.loads(raw)["org_id"] == a["org_id"], status)
    now = what_org_a_sees(base, a, ids)
    for key, was in saved["seen"].items():
        step(f"{key} came back intact", now[key] == was, "" if now[key] == was else f"was {str(was)[:160]} now {str(now[key])[:160]}")
    print("durable state verified", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--wait", type=float, default=180, help="seconds to wait for /readyz")
    ap.add_argument("--packs", nargs="+", default=list(PACKS))
    ap.add_argument("--operator", default=OPERATOR, help="command prefix that runs python inside the API's environment")
    ap.add_argument("--job-seconds", type=float, default=300, help="seconds to wait for each job")
    ap.add_argument("--state", type=pathlib.Path, help="write what org a sees here, for --verify after a restart")
    ap.add_argument("--verify", type=pathlib.Path, help="only check that what --state saved came back")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    wait_ready(base, args.wait)
    if args.verify:
        verify(base, args.verify)
        return
    step("/healthz", call(base, "/healthz")[0] == 200)
    status, raw = call(base, "/console/")
    step("console is built in", status == 200 and b"<html" in raw.lower(), status)

    step("the door is closed", json.loads(call(base, "/auth/status")[1])["dev_open"] is False)
    step("no token, no workspaces", call(base, "/workspaces")[0] == 401)
    a, b = signup(base, "a"), signup(base, "b")
    status, raw = call(base, "/auth/me", token=a["token"])
    step("a token is one org", status == 200 and json.loads(raw)["org_id"] == a["org_id"] != b["org_id"])

    for pack in args.packs:
        status, raw = call(base, f"/workspaces/{pack}/reports/evals", token=a["token"])
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

    ids = walk_a_tenant(base, a, b, ws, args.operator, args.job_seconds)
    if args.state:
        seen = what_org_a_sees(base, a, ids)
        args.state.write_text(json.dumps({"org_a": a, "ids": ids, "seen": seen}))
    print("smoke passed", flush=True)


if __name__ == "__main__":
    main()
