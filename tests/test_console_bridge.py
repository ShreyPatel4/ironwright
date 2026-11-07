"""The run board and the approval card, in a real headless browser, with no route stubbed.

Real here: the built console, sessions with the door closed, the gate, the job queue and a
worker on its own thread. Set up through the routes: the upload, the profile job and the
workspace. The one direct write is the workspace's policies.yaml, the customer's own permit
for the silver builder, which no screen owns yet.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import yaml
from test_console_ask import (  # noqa: F401
    browser,
    console_dist,
    open_console,
    serve,
    signup,
    view,
)

from ironwright import workspaces_tenant as wt

UPLOADS = Path(__file__).parent / "fixtures" / "tenant_ws" / "uploads" / "fixture"
PASSWORD = "correct-horse-9"
SLUG = "acme-claims"
PERMIT = {"default": "deny", "rules": [
    {"id": "silver-builder-promotes", "effect": "permit", "principal": "agent:silver-builder",
     "action": "action:bridge.promote.silver"}]}


@pytest.fixture()
def hosted(console_dist, tmp_path, monkeypatch):  # noqa: F811
    yield from serve(monkeypatch, tmp_path, "bridge")


def h(who: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {who['token']}"}


def admin_of(base: str, owner: dict) -> dict:
    made = httpx.post(f"{base}/orgs/{owner['org_slug']}/invites", headers=h(owner), timeout=30,
                      json={"email": "bo@example.com", "role": "admin"})
    assert made.status_code == 201, made.text
    got = httpx.post(f"{base}/auth/accept-invite", timeout=30,
                     json={"token": made.json()["token"], "password": PASSWORD})
    assert got.status_code in (200, 201), got.text
    return got.json()


def workspace(base: str, owner: dict) -> str:
    files = [("files", (f.name, f.read_bytes(), "text/csv")) for f in sorted(UPLOADS.glob("*.csv"))]
    made = httpx.post(f"{base}/uploads", headers=h(owner), files=files, timeout=30)
    assert made.status_code == 201, made.text
    upload = made.json()["id"]
    job = httpx.post(f"{base}/uploads/{upload}/profile", headers=h(owner), timeout=30).json()
    deadline = time.monotonic() + 60
    while httpx.get(f"{base}/jobs/{job['id']}", headers=h(owner), timeout=30).json()["state"] != "succeeded":
        assert time.monotonic() < deadline, "the profile job did not finish"
        time.sleep(0.2)
    proposal = httpx.get(f"{base}/uploads/{upload}/proposal", headers=h(owner), timeout=30).json()
    r = httpx.post(f"{base}/workspaces", headers=h(owner), timeout=30, json={
        "from_upload": upload, "slug": SLUG, "spec": proposal["workspace"],
        "silver": {"declined": [], "renames": {}}})
    assert r.status_code == 201, r.text
    (wt._existing(owner["org_id"], SLUG) / "policies.yaml").write_text(yaml.safe_dump(PERMIT))
    return proposal["workspace"]["title"]


def open_board(page, title: str) -> None:
    page.locator(".card", has_text=title).first.click()
    view(page, "Run board")


def test_a_change_waits_on_the_card_and_a_second_admin_promotes_it(browser, hosted):  # noqa: F811
    owner = signup(hosted, "ann")
    admin = admin_of(hosted, owner)
    title = workspace(hosted, owner)
    models = wt._existing(owner["org_id"], SLUG) / "models" / "silver"
    before = {p.name: p.read_text() for p in models.glob("*.sql")}

    page = open_console(browser, hosted, owner)
    open_board(page, title)
    page.get_by_role("button", name="Draft silver from the silver draft").click()
    card = page.get_by_role("region", name="approval card")
    card.wait_for()
    assert "Promotion into silver" in card.inner_text()
    scores = card.locator("table").first
    compliance = scores.locator("tr", has_text="compliance")
    assert "pass" in compliance.inner_text() and "masked policies.holder" in compliance.inner_text()
    assert "Jos" not in card.inner_text()
    board = page.get_by_role("table", name="run board")
    assert "1 retries" in board.inner_text() and "1 escalations" in board.inner_text()

    card.get_by_role("button", name="Approve").click()
    assert "requester cannot approve" in page.locator(".err").last.inner_text()
    assert {p.name: p.read_text() for p in models.glob("*.sql")} == before
    page.context.close()

    page = open_console(browser, hosted, admin)
    open_board(page, title)
    page.get_by_role("table", name="queue").get_by_role("button", name="Open card").click()
    page.get_by_role("region", name="approval card").get_by_role("button", name="Approve").click()
    page.get_by_text("is observed").wait_for()
    assert page.get_by_text("Queue empty. Nothing is waiting.").is_visible()
    policies = next(models.glob("*policies.sql")).read_text()
    assert "sha256" in policies
    page.context.close()
