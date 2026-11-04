"""Walk a running hosted stack from the outside, as signed-up orgs, with the door closed.
Standard library only, so it runs anywhere. It prints one line per step and exits
non-zero at the first break.

    python deploy/hosted_smoke.py --base http://127.0.0.1:8000 --wait 180

The demo packs must be built first (`ironwright run --pack all` in the container),
because a tenant can only read them.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

PACKS = ("retail", "supply_chain", "banking_aml", "insurance_claims")


def call(base: str, path: str, body: Any = None, token: str | None = None, timeout: float = 60) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
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

    status, raw = call(base, "/workspaces", {"name": "smoke", "size": 4, "seed": 3}, token=a["token"], timeout=120)
    step("org a makes a workspace", status == 201, raw[:200] if status != 201 else "")
    ws = json.loads(raw)["name"]
    step("org a sees it", call(base, f"/workspaces/{ws}/organization", token=a["token"])[0] == 200)
    step("org b gets a 404", call(base, f"/workspaces/{ws}/organization", token=b["token"])[0] == 404)
    print("smoke passed", flush=True)


if __name__ == "__main__":
    main()
