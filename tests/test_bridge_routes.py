"""The promotion gate over HTTP, with the door closed and real sessions.

A real customer journey up to a workspace with a silver draft (upload, profile job,
create), then the bridge routes only. The approver is a second admin, because the owner
who starts the run is the requester and can never approve it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ironwright import workspaces_tenant as wt
from ironwright.config import tenant_dir
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.platform_api import create_platform_app

UPLOADS = Path(__file__).parent / "fixtures" / "tenant_ws" / "uploads" / "fixture"
PASSWORD = "correct-horse-9"
SLUG = "acme-claims"
PERMIT = {"default": "deny", "rules": [
    {"id": "silver-builder-promotes", "effect": "permit", "principal": "agent:silver-builder",
     "action": "action:bridge.promote.silver"}]}


def bearer(who: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {who['token']}"}


@pytest.fixture()
def client(meta_db):
    with TestClient(create_platform_app()) as c:
        yield c


def signup(c: TestClient, email: str, org: str) -> dict:
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    assert r.status_code == 201, r.text
    return r.json()


def join_as(c: TestClient, owner: dict, label: str, role: str) -> dict:
    made = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                  json={"email": f"{label}@example.com", "role": role})
    assert made.status_code == 201, made.text
    got = c.post("/auth/accept-invite", json={"token": made.json()["token"], "password": PASSWORD})
    assert got.status_code in (200, 201), got.text
    return got.json()


def workspace(c: TestClient, who: dict, permit: bool = True) -> Path:
    files = [("files", (f.name, f.read_bytes(), "text/csv")) for f in sorted(UPLOADS.glob("*.csv"))]
    made = c.post("/uploads", headers=bearer(who), files=files)
    assert made.status_code == 201, made.text
    job = c.post(f"/uploads/{made.json()['id']}/profile", headers=bearer(who))
    assert job.status_code == 202, job.text
    Worker(JobStore()).run_once(job.json()["id"])
    proposal = c.get(f"/uploads/{made.json()['id']}/proposal", headers=bearer(who)).json()
    r = c.post("/workspaces", headers=bearer(who), json={
        "from_upload": made.json()["id"], "slug": SLUG, "spec": proposal["workspace"],
        "silver": {"declined": [], "renames": {}}})
    assert r.status_code == 201, r.text
    d = wt._existing(who["org_id"], SLUG)
    if permit:  # the customer's own policy file; no screen owns it yet
        (d / "policies.yaml").write_text(yaml.safe_dump(PERMIT))
    return d


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(folder)).encode() + p.read_bytes())
    return h.hexdigest()


def base(name: str = SLUG) -> str:
    return f"/workspaces/{name}/bridge"


def start_run(c: TestClient, who: dict) -> dict:
    r = c.post(f"{base()}/runs", headers=bearer(who), json={})
    assert r.status_code == 201, r.text
    return r.json()


def decide(c: TestClient, who: dict, card: dict, decision: str = "approve", reason: str = ""):
    return c.post(f"{base()}/cards/{card['card_id']}/decide", headers=bearer(who),
                  json={"decision": decision, "content_hash": card["content_hash"], "reason": reason})


def test_a_silver_run_promotes_only_after_a_second_admin_approves(client):
    owner = signup(client, "ann@example.com", "Org Ann")
    admin = join_as(client, owner, "bo", "admin")
    member = join_as(client, owner, "cy", "member")
    d = workspace(client, owner)
    models_before = digest(d / "models")

    out = start_run(client, owner)
    assert out["state"] == "escalated" and out["interrupted"]
    assert digest(d / "models") == models_before   # nothing live before interrupt()

    card = client.get(f"{base()}/cards/{out['card_id']}", headers=bearer(owner)).json()
    assert card["kind"] == "promotion"
    assert "policies.holder" in card["compliance"]["masked"]
    assert "Jos" not in str(card["samples"])        # the holder never reaches the inbox

    board = client.get(f"{base()}/board", headers=bearer(member)).json()
    assert board["queue"][0]["card_id"] == out["card_id"]
    cell = next(x for x in board["cells"] if x["layer"] == "silver" and x["agent"] == "agent:silver-builder")
    assert cell["retries"] == 1 and cell["escalations"] == 1

    assert decide(client, member, card).status_code == 403
    refused = decide(client, owner, card)
    assert refused.status_code == 422 and "requester" in refused.json()["detail"]
    assert digest(d / "models") == models_before

    done = decide(client, admin, card)
    assert done.status_code == 200, done.text
    assert done.json()["run"]["state"] == "observed"
    policies = next((d / "models" / "silver").glob("*policies.sql")).read_text()
    assert "sha256" in policies
    assert client.get(f"{base()}/board", headers=bearer(owner)).json()["queue"] == []


def test_without_a_permit_an_approved_promotion_is_denied(client):
    owner = signup(client, "ann@example.com", "Org Ann")
    admin = join_as(client, owner, "bo", "admin")
    d = workspace(client, owner, permit=False)
    before = digest(d / "models")
    out = start_run(client, owner)
    card = client.get(f"{base()}/cards/{out['card_id']}", headers=bearer(owner)).json()
    done = decide(client, admin, card).json()
    assert done["run"]["state"] == "approved"
    assert "policy denied" in done["run"]["refused"]
    assert digest(d / "models") == before


def test_a_rejection_goes_back_to_the_agent_and_comes_back_as_a_new_card(client):
    owner = signup(client, "ann@example.com", "Org Ann")
    admin = join_as(client, owner, "bo", "admin")
    workspace(client, owner)
    out = start_run(client, owner)
    card = client.get(f"{base()}/cards/{out['card_id']}", headers=bearer(owner)).json()
    assert decide(client, admin, card, "reject").status_code == 422     # a reason is required
    again = decide(client, admin, card, "reject", reason="not this week").json()
    assert again["run"]["state"] == "escalated"
    assert again["run"]["card_id"] != out["card_id"]
    fresh = client.get(f"{base()}/cards/{again['run']['card_id']}", headers=bearer(owner)).json()
    assert "policies.holder" in fresh["compliance"]["masked"]           # the mask survived the restart


def test_another_org_cannot_see_or_decide_anything(client):
    a = signup(client, "ann@example.com", "Org Ann")
    d = workspace(client, a)
    out = start_run(client, a)
    card = client.get(f"{base()}/cards/{out['card_id']}", headers=bearer(a)).json()
    assert (d / "bridge").resolve().is_relative_to(tenant_dir(a["org_id"]).resolve())

    b = signup(client, "bo@example.com", "Org Bo")
    for path in (f"{base()}/board", f"{base()}/cards", f"{base()}/cards/{out['card_id']}"):
        r = client.get(path, headers=bearer(b))
        assert r.status_code == 404, path
        assert out["card_id"] not in r.text
    assert decide(client, b, card).status_code == 404
    assert client.post(f"{base()}/runs", headers=bearer(b), json={}).status_code == 404

    # B's own workspace under the same name answers for B alone.
    workspace(client, b)
    mine = client.get(f"{base()}/board", headers=bearer(b)).json()
    assert mine["runs"] == [] and mine["queue"] == []
    assert client.get(f"{base()}/cards/{out['card_id']}", headers=bearer(b)).status_code == 404
    assert decide(client, b, card).status_code == 404
    assert client.get(f"{base()}/cards/{out['card_id']}", headers=bearer(a)).json()["decision"] is None


def test_a_demo_pack_shows_an_empty_board_and_refuses_a_run(client):
    a = signup(client, "ann@example.com", "Org Ann")
    assert client.get("/workspaces/retail/bridge/board", headers=bearer(a)).json()["runs"] == []
    assert client.post("/workspaces/retail/bridge/runs", headers=bearer(a), json={}).status_code == 403
