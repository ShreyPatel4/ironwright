"""Walk a running hosted stack from the outside. Standard library only, so it
runs anywhere. It prints one line per step and exits non-zero at the first break.

    python deploy/hosted_smoke.py --base http://127.0.0.1:8000 --wait 180

The stack must allow the calls: IRONWRIGHT_DEV_OPEN=1 until T1's sign-up lands.
Runs go through /jobs when that route exists and through the synchronous run
route when it does not.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PACKS = ("retail", "supply_chain", "banking_aml", "insurance_claims")
RUN_TIMEOUT = 600


def call(base: str, path: str, body: Any = None, timeout: float = 10) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
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


def run_with_jobs(base: str, pack: str) -> None:
    status, raw = call(base, "/jobs", {"kind": "run", "workspace": pack, "params": {}})
    step(f"enqueue {pack}", status in (200, 201), raw[:200])
    job = json.loads(raw)["id"]
    deadline = time.monotonic() + RUN_TIMEOUT
    state = "queued"
    while time.monotonic() < deadline:
        state = json.loads(call(base, f"/jobs/{job}")[1])["state"]
        if state in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(2)
    step(f"job {pack}", state == "succeeded", state)


def run_sync(base: str, pack: str) -> None:
    status, raw = call(base, f"/workspaces/{pack}/runs", {}, timeout=RUN_TIMEOUT)
    step(f"run {pack}", status == 200, raw[:200] if status != 200 else "")
    step(f"evals {pack}", json.loads(raw)["evals"]["passed"] is True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--wait", type=float, default=180, help="seconds to wait for /readyz")
    ap.add_argument("--packs", nargs="+", default=list(PACKS))
    args = ap.parse_args()
    base = args.base.rstrip("/")

    wait_ready(base, args.wait)
    step("/healthz", call(base, "/healthz")[0] == 200)
    status, raw = call(base, "/console/")
    step("console is built in", status == 200 and b"<html" in raw.lower(), status)

    has_jobs = call(base, "/jobs?workspace=retail")[0] != 404
    print(f"runs go through {'/jobs' if has_jobs else 'the synchronous route'}", flush=True)
    for pack in args.packs:
        (run_with_jobs if has_jobs else run_sync)(base, pack)

    pack = args.packs[0]
    status, raw = call(base, f"/workspaces/{pack}/lake/tables", timeout=60)
    tables = json.loads(raw) if status == 200 else []
    gold = next((t["name"] for t in tables if t["layer"] == "gold"), None)
    step("lake lists tables", gold is not None, f"{len(tables)} tables")
    status, raw = call(base, f"/workspaces/{pack}/lake/query",
                       {"sql": f"select count(*) as n from gold_{gold}"}, timeout=60)
    step("lake answers a query", status == 200 and int(json.loads(raw)["rows"][0][0]) > 0, raw[:120])
    print("smoke passed", flush=True)


if __name__ == "__main__":
    main()
