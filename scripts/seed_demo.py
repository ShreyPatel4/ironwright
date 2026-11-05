"""Make the SaaS demo's tenants: Northwind Mutual, with its messy exports waiting as uploads,
and Rival Co, which holds nothing and is there to be refused.

Northwind's files are the insurance policy pack's legacy exports, taken the way a customer's
files arrive: as uploads, with no pack behind them and no answer key. The owner logs in and
walks the wizard from Profile on. The film signs up its own tenant with these same bytes, so
this script and the film can both run on one stack, in either order. Run it again and it
changes nothing.

Local:

    IRONWRIGHT_SEED_PASSWORD=... python scripts/seed_demo.py

Compose. The image has no scripts folder, so it goes over stdin where the data volume is mounted:

    docker compose -f deploy/compose.yaml exec -T -e IRONWRIGHT_SEED_PASSWORD=... api \
        python - < scripts/seed_demo.py
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ironwright.ingest import uploads
from ironwright.packs import load_pack
from ironwright.tenancy import service

ORG = "Northwind Mutual"
OWNER = "owner@northwind-mutual.example"
VIEWER = "viewer@northwind-mutual.example"
RIVAL_ORG = "Rival Co"
RIVAL = "owner@rival-co.example"

PACK = "insurance_policy"
# Fixed width with no header, a billing export, and an agency's own spreadsheet. The JSON
# lines claims feed is not a file type uploads take, and the answer key stays behind.
FILES = ("POLADMIN.dat", "BILLING.csv", "AGENCY_BOOK.csv")


def export_files(dest: Path) -> list[Path]:
    """The bytes the seed uploads and the film uploads. The generator writes to a temp folder
    and never to demo/, because generate wipes a pack's folder and every tenant reads that one."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        load_pack(PACK).generator().generate(Path(tmp))
        return [Path(shutil.copy2(Path(tmp) / name, dest / name)) for name in FILES]


def _session(email: str, password: str, org_name: str) -> dict[str, Any]:
    """Only a login proves whose org this is. Signup answers a taken email with a decoy, so
    its answer is never used, and a wrong password stops here."""
    try:
        return service.login(email, password, time.time())
    except service.BadCredentials:
        pass
    service.revoke(service.signup(email, password, org_name, time.time())["token"])
    try:
        return service.login(email, password, time.time())
    except service.BadCredentials as e:
        raise SystemExit(f"{email} exists and IRONWRIGHT_SEED_PASSWORD is not its password") from e


def _viewer(slug: str, password: str, actor: str) -> None:
    try:
        service.revoke(service.login(VIEWER, password, time.time())["token"])
    except service.BadCredentials:
        token = service.invite(slug, VIEWER, "viewer", time.time(), actor=actor)
        try:
            service.revoke(service.accept_invite(token, password, time.time())["token"])
        except service.BadCredentials as e:
            raise SystemExit(f"{VIEWER} exists and IRONWRIGHT_SEED_PASSWORD is not its password") from e


def _upload(org_id: str) -> tuple[str, bool]:
    held = [u for u in uploads.list_uploads(org_id)
            if sorted(f["name"] for f in u["files"]) == sorted(FILES)]
    if held:
        return held[0]["id"], False
    with tempfile.TemporaryDirectory() as tmp:
        session = uploads.UploadSession(org_id)
        try:
            for path in export_files(Path(tmp)):
                session.begin_file(path.name)
                session.write(path.read_bytes())
                session.end_file()
            return session.commit()["id"], True
        except Exception:
            session.abort()
            raise


def seed() -> dict[str, Any]:
    if os.environ.get("IRONWRIGHT_ENV") == "prod":
        raise SystemExit("this makes demo orgs with a known login, so it refuses prod")
    password = os.environ.get("IRONWRIGHT_SEED_PASSWORD")
    if not password:
        raise SystemExit("set IRONWRIGHT_SEED_PASSWORD: the demo logins' password never goes in the repo")
    owner = _session(OWNER, password, ORG)
    service.revoke(owner["token"])  # people log in for themselves, and this session is not kept
    rival = _session(RIVAL, password, RIVAL_ORG)
    service.revoke(rival["token"])
    _viewer(owner["org_slug"], password, f"user:{owner['user_id']}")
    upload_id, created = _upload(owner["org_id"])
    return {"created": created, "owner": OWNER, "viewer": VIEWER, "rival": RIVAL,
            "org_slug": owner["org_slug"], "rival_slug": rival["org_slug"], "upload_id": upload_id}


if __name__ == "__main__":
    print(json.dumps(seed(), indent=1))
