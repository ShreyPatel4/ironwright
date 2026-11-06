"""The Actions step, an Ask offer and the Approvals view, in a real browser on a real server.

The owner writes a permit in the wizard. A member asks a question, gets the action
offered as a button, and asks for it. The owner sees before and after in Approvals and
approves. No route is stubbed.
"""
from __future__ import annotations

import json
import re
import urllib.request

from test_console_onboarding import (  # noqa: F401  pytest finds the fixtures by name
    PASSWORD,
    browser,
    console_dist,
    expect,
    log_in,
    new_page,
    session_of,
    stack,
    walk_to_ask,
)

WS = "acme-sales"


def call(on, method: str, path: str, token: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(on.base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_a_permit_written_in_the_wizard_becomes_an_offer_a_member_asks_for_and_an_owner_approves(browser, stack):  # noqa: F811
    owner = new_page(browser, stack)
    errors: list[str] = []
    owner.on("pageerror", lambda e: errors.append(str(e)))
    walk_to_ask(owner, stack)
    me = session_of(owner)
    catalog = call(stack, "GET", f"/workspaces/{WS}/actions", me["token"])
    otype = next(t["name"] for t in catalog["object_types"] if "status" in t["properties"])

    owner.get_by_role("button", name=re.compile(r"^\d+ Actions$")).click()
    expect()(owner.get_by_role("heading", name="Actions")).to_be_visible()
    owner.get_by_label("template").select_option("set_status")
    owner.get_by_label("object type").select_option(otype)
    owner.get_by_label("statuses it may set, comma separated").fill("CLOSED, A")
    owner.get_by_role("button", name="Preview the rule").click()
    expect()(owner.get_by_label("the rule in plain words")).to_contain_text("Nothing changes until an owner or admin")
    owner.get_by_role("button", name="Accept this permit").click()
    action = f"set_status_{otype.lower()}"
    expect()(owner.get_by_text(f"Accepted {action}")).to_be_visible()

    invite = call(stack, "POST", f"/orgs/{me['org_slug']}/invites", me["token"],
                  {"email": "member@acme.test", "role": "member"})
    req = urllib.request.Request(stack.base + "/auth/accept-invite", method="POST",
                                 data=json.dumps({"token": invite["token"], "password": PASSWORD}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).close()
    oid = call(stack, "GET", f"/workspaces/{WS}/objects/{otype}", me["token"])[0]["_id"]

    member = new_page(browser, stack)
    member.on("pageerror", lambda e: errors.append(str(e)))
    log_in(member, stack, "member@acme.test")
    expect()(member.get_by_role("heading", name="Ask")).to_be_visible(timeout=60_000)
    member.get_by_label("your question").fill(f"Get {otype.lower()} {oid}: what is its status?")
    with member.expect_response(lambda r: r.url.endswith("/ask")) as asked:
        member.get_by_role("button", name="Ask", exact=True).click()
    answer = asked.value.json()
    assert answer["status"] == "answered", answer
    assert [o["action"] for o in answer["offers"]] == [action]
    offer = member.get_by_label(f"offer {action}")
    with member.expect_response(lambda r: r.url.endswith(f"/actions/{action}")) as requested:
        offer.get_by_role("button", name=f"Ask to {action}").click()
    assert requested.value.json()["status"] == "needs_approval"
    expect()(offer.get_by_text("waiting for an approver")).to_be_visible()

    owner.get_by_role("button", name="Go to the console").click()
    title = next(w["title"] for w in call(stack, "GET", "/workspaces", me["token"]) if w["name"] == WS)
    owner.locator("button.card").filter(has_text=title).first.click()
    owner.get_by_role("button", name="Approvals", exact=True).click()
    row = owner.get_by_role("row").filter(has_text=action)
    expect()(row).to_contain_text("CLOSED")
    with owner.expect_response(lambda r: r.url.endswith("/decide")) as decided:
        row.get_by_role("button", name="Approve").click()
    assert decided.value.json()["result"]["status"] == "applied", decided.value.json()
    expect()(owner.get_by_text("Inbox empty.")).to_be_visible()
    assert call(stack, "GET", f"/workspaces/{WS}/objects/{otype}/{oid}", me["token"])["status"] == "CLOSED"
    assert call(stack, "GET", f"/workspaces/{WS}/audit/verify", me["token"])["intact"] is True

    # The object page shows the permitted action as a button, and a request from it parks.
    owner.get_by_role("button", name="Ontology", exact=True).click()
    owner.get_by_role("button", name=re.compile(f"^{otype}")).click()
    owner.get_by_role("button", name=oid, exact=True).click()
    buttons = owner.get_by_label("actions on this object")
    buttons.get_by_label(f"{action} status").select_option("A")
    with owner.expect_response(lambda r: r.url.endswith(f"/actions/{action}")) as again:
        buttons.get_by_role("button", name=f"Ask to {action}").click()
    assert again.value.json()["status"] == "needs_approval"
    expect()(buttons.get_by_text("waiting for an approver")).to_be_visible()
    assert errors == []
