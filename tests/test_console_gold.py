"""The Gold step in a real headless browser, against a real platform app, with no route stubbed.

The fixture's three files go in through the upload and profile routes with the worker
doing the job. Then the browser types the contract, saves it, and reads the coverage the
server's rules produced: gaps and partials in the warning color, covered lines in green.
"""
from __future__ import annotations

import time

import httpx
import pytest
from test_console_ask import (  # noqa: F401
    browser,
    console_dist,
    expect,
    open_console,
    serve,
    signup,
    view,
)

from ironwright.bridge import fixture

SLUG = "acme-bridge"


@pytest.fixture()
def hosted(console_dist, tmp_path, monkeypatch):  # noqa: F811
    yield from serve(monkeypatch, tmp_path, "gold")


def _workspace(base: str, who: dict, files_dir) -> None:
    head = {"Authorization": f"Bearer {who['token']}"}

    def done(job_id: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            got = httpx.get(f"{base}/jobs/{job_id}", headers=head, timeout=30).json()
            if got["state"] in ("succeeded", "failed"):
                assert got["state"] == "succeeded", got
                return
            time.sleep(0.1)
        raise AssertionError(f"job {job_id} did not finish")

    files = [("files", (n, (files_dir / n).read_bytes(), "text/plain")) for n in fixture.FILES]
    upload_id = httpx.post(f"{base}/uploads", headers=head, files=files, timeout=30).json()["id"]
    done(httpx.post(f"{base}/uploads/{upload_id}/profile", headers=head, timeout=30).json()["id"])
    spec = httpx.get(f"{base}/uploads/{upload_id}/proposal", headers=head, timeout=30).json()["workspace"]
    spec["title"] = "Bridge fixture"
    made = httpx.post(f"{base}/workspaces", headers=head, timeout=30,
                      json={"from_upload": upload_id, "spec": spec, "slug": SLUG,
                            "silver": {"declined": [], "renames": {}}})
    assert made.status_code == 201, made.text


def test_an_admin_writes_a_contract_and_sees_every_planted_gap(browser, hosted, tmp_path):  # noqa: F811
    truth = fixture.generate(tmp_path / "fx")
    who = signup(hosted, "gold")
    _workspace(hosted, who, tmp_path / "fx")
    page = open_console(browser, hosted, who)
    page.locator(".card", has_text="Bridge fixture").first.click()
    view(page, "Gold")
    expect()(page.get_by_text("nothing saved yet")).to_be_visible()

    page.get_by_label("use cases").fill("Which claims are paid above limit?")
    page.get_by_role("button", name="Draft a contract").click()
    expect()(page.locator(".badge", has_text="the draft needs work")).to_be_visible()

    page.get_by_label("gold contract").fill((tmp_path / "fx" / "gold.yaml").read_text())
    page.get_by_role("button", name="Save contract").click()
    expect()(page.get_by_text("saved as gold.yaml in this workspace")).to_be_visible()
    expect()(page.locator(".badge.ok", has_text="8 covered")).to_be_visible()
    expect()(page.locator(".badge.hold", has_text="2 gaps")).to_be_visible()

    svg = page.get_by_role("img", name="lineage from source columns to gold columns")
    for ref, want in truth["gold"].items():
        node = svg.locator(f'g[data-ref="{ref}"]')
        expect()(node).to_have_attribute("data-status", want["status"])
        colour = node.locator("text").evaluate("e => e.getAttribute('fill')")
        assert colour == ("var(--ink-0)" if want["status"] == "covered" else "var(--warn)"), ref
    assert svg.locator('path[data-status="covered"]').count() == 8
    assert svg.locator('path[data-status="partial"]').count() == 4  # a gap has no source, so no line
    row = page.locator('tr[data-ref="fct_claim_payment.adjuster_region"]')
    expect()(row).to_contain_text("join")
    expect()(row).to_contain_text("adjusters.region")


def test_a_demo_pack_has_no_gold_step(browser, hosted):  # noqa: F811
    page = open_console(browser, hosted, signup(hosted, "demo"))
    page.locator(".card").first.click()
    page.locator("nav.rail").get_by_role("button", name="Pipeline", exact=True).wait_for()
    assert page.locator("nav.rail").get_by_role("button", name="Gold", exact=True).count() == 0
