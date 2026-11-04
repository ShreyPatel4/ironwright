"""Make the staging demo org: one synthetic pack, as a real tenant workspace.

It goes the way a customer's files go: upload, profile, the proposed workspace, a run.
The pack's generator writes to a temp folder and never to demo/, because generate wipes
a pack's folder and every tenant reads that one. Run it again and it changes nothing.

Only the supply chain ERP exports go in. They are plain CSVs with a clear key and a clear
join, which is what the ontology step needs. The answer key stays behind.

The image has no scripts folder, so run it over stdin where the data volume is mounted:

    docker compose -f deploy/compose.yaml exec -T -e IRONWRIGHT_SEED_PASSWORD=... api \
        python - < scripts/seed_demo_org.py
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ironwright import workspaces_tenant as wt
from ironwright.ingest import profiler, uploads
from ironwright.packs import load_pack
from ironwright.tenancy import service

EMAIL = "demo-owner@example.com"
ORG_NAME = "Ironwright Demo"
WORKSPACE = "demo"
PACK = "supply_chain"
FILES = "erp/*.csv"


def _session(password: str) -> dict[str, Any]:
    now = time.time()
    try:
        return service.signup(EMAIL, password, ORG_NAME, now)
    except service.EmailTaken:
        pass
    try:
        return service.login(EMAIL, password, now)
    except service.BadCredentials as e:
        raise SystemExit(f"{EMAIL} exists and IRONWRIGHT_SEED_PASSWORD is not its password") from e


def _upload(org_id: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        load_pack(PACK).generator().generate(folder)
        session = uploads.UploadSession(org_id)
        try:
            for path in sorted(folder.glob(FILES)):
                session.begin_file(path.name)
                session.write(path.read_bytes())
                session.end_file()
            return session.commit()["id"]
        except Exception:
            session.abort()
            raise


def seed() -> dict[str, Any]:
    if os.environ.get("IRONWRIGHT_ENV") == "prod":
        raise SystemExit("this makes a demo org with a known login, so it refuses prod")
    password = os.environ.get("IRONWRIGHT_SEED_PASSWORD")
    if not password:
        raise SystemExit("set IRONWRIGHT_SEED_PASSWORD: the demo owner's password never goes in the repo")
    session = _session(password)
    service.revoke(session["token"])  # the owner logs in for themselves, and this one is not kept
    org_id = session["org_id"]
    created = False
    try:
        spec = wt.read_workspace(org_id, WORKSPACE)["spec"]
    except FileNotFoundError:
        upload_id = _upload(org_id)
        profiler.profile(org_id, upload_id, lambda line: print(f"  {line}", flush=True))
        proposal = json.loads((uploads.upload_dir(org_id, upload_id) / "proposal.json").read_text())
        spec = proposal["workspace"]
        wt.create_workspace(org_id, WORKSPACE, spec)
        created = True
    report = wt.load_workspace(org_id, WORKSPACE).paths.reports / "evals.json"
    passed = wt.run_workspace(org_id, WORKSPACE)["passed"] if created or not report.exists() \
        else json.loads(report.read_text())["passed"]
    if not passed:
        raise SystemExit(f"the {WORKSPACE} workspace ran, and its evals did not pass")
    return {"created": created, "email": EMAIL, "org_id": org_id, "org_slug": session["org_slug"],
            "workspace": WORKSPACE, "upload_id": spec["sources"][0]["upload"].split("/")[0], "evals_passed": passed}


if __name__ == "__main__":
    print(json.dumps(seed(), indent=1))
