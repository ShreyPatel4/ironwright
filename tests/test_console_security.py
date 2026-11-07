"""The tier 1 console flows in a real headless browser against a real served platform:
sign in on the cookie, two-step enrolment and challenge, a reset from the mailbox,
sessions, tokens, the auth audit, and an org that requires two-step sign in.

Nothing is stubbed. The mail is the outbox's local file, which is what a local server uses.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import cookie_session
import pytest
from test_console_settings import PASSWORD, browser, console_dist, expect, stack  # noqa: F401  fixtures

from ironwright.security import mfa, outbox

NEW_PASSWORD = "a-brand-new-secret-7"


@pytest.fixture()
def site(stack, monkeypatch):  # noqa: F811
    monkeypatch.setenv("IRONWRIGHT_PUBLIC_URL", stack.base)
    return stack


def fresh(browser):  # noqa: F811
    context = browser.new_context(accept_downloads=True)
    context.set_default_timeout(15_000)
    context.add_init_script("localStorage.setItem('iw.onboarded', '1');")
    return context.new_page()


def sign_in(page, base, email, password=PASSWORD):
    page.goto(f"{base}/console/")
    page.get_by_label("email").fill(email)
    page.get_by_label("password").fill(password)
    page.get_by_role("button", name="Enter the console").click()


def settings_security(page):
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Security").click()


def code_now(secret: str, skew: int = 0) -> str:
    return mfa.totp(base64.b32decode(secret.replace(" ", "")), int(time.time() // 30) + skew)


def mail_to(email: str) -> list[dict]:
    path = outbox.file_path()
    return [m for m in (json.loads(x) for x in path.read_text().splitlines()) if m["to"] == email]


def test_sign_in_rides_an_httponly_cookie_the_page_cannot_read(browser, site):  # noqa: F811
    site.signup()
    page = fresh(browser)
    sign_in(page, site.base, "owner@acme.test")
    expect()(page.get_by_role("button", name="Settings")).to_be_visible()
    cookie = next(c for c in page.context.cookies() if c["name"] == cookie_session.COOKIE)
    assert cookie["httpOnly"] and cookie["secure"] and cookie["sameSite"] == "Lax"
    assert cookie_session.page_cannot_read_it(page)
    assert page.evaluate("localStorage.getItem('iw.session')") is None
    page.reload()
    expect()(page.get_by_role("button", name="Settings")).to_be_visible()  # the cookie brought it back


def test_enrol_then_sign_in_with_a_code(browser, site):  # noqa: F811
    site.signup()
    page = fresh(browser)
    sign_in(page, site.base, "owner@acme.test")
    settings_security(page)
    box = page.get_by_test_id("security-mfa")
    box.get_by_role("button", name="Set it up").click()
    secret = page.get_by_test_id("mfa-secret").inner_text()
    box.get_by_label("code from the app").fill(code_now(secret))
    box.get_by_role("button", name="Turn it on").click()
    expect()(page.get_by_test_id("recovery-codes")).to_be_visible()
    codes = page.get_by_test_id("recovery-codes").locator("pre").inner_text().split()
    assert len(codes) == 10
    expect()(page.get_by_test_id("mfa-on")).to_contain_text("10 recovery codes left")
    # The rotated session still works in this page: it is the new cookie, not the old one.
    page.get_by_role("button", name="Workspaces").click()
    expect()(page.get_by_role("heading", name="Workspaces")).to_be_visible()

    other = fresh(browser)
    sign_in(other, site.base, "owner@acme.test")
    expect()(other.get_by_text("Type the 6 digit code from your authenticator app.")).to_be_visible()
    assert cookie_session.token_of(other.context) is None  # a password alone set no cookie
    other.get_by_label("code").fill(code_now(secret, skew=1))
    other.get_by_role("button", name="Sign in").click()
    expect()(other.get_by_role("button", name="Settings")).to_be_visible()

    third = fresh(browser)
    sign_in(third, site.base, "owner@acme.test")
    third.get_by_role("button", name="Use a recovery code").click()
    third.get_by_label("recovery code").fill(codes[0])
    third.get_by_role("button", name="Sign in").click()
    expect()(third.get_by_role("button", name="Settings")).to_be_visible()


def test_a_reset_from_the_mailbox(browser, site):  # noqa: F811
    site.signup()
    page = fresh(browser)
    page.goto(f"{site.base}/console/")
    page.get_by_role("link", name="Forgot your password?").click()
    page.get_by_label("email").fill("owner@acme.test")
    page.get_by_role("button", name="Send the link").click()
    expect()(page.get_by_test_id("reset-sent")).to_contain_text("if that email has an account")
    link = re.search(r"https?://\S+#reset=\S+", mail_to("owner@acme.test")[-1]["text"]).group(0)
    assert link.startswith(f"{site.base}/console/#reset=")
    page.goto(link)
    page.get_by_label("new password").fill("qwerty123456")
    page.get_by_role("button", name="Save the password").click()
    expect()(page.get_by_text("breached")).to_be_visible()
    page.get_by_label("new password").fill(NEW_PASSWORD)
    page.get_by_role("button", name="Save the password").click()
    expect()(page.get_by_test_id("reset-done")).to_be_visible()
    page.get_by_role("button", name="Go to sign in").click()
    sign_in(page, site.base, "owner@acme.test", NEW_PASSWORD)
    expect()(page.get_by_role("button", name="Settings")).to_be_visible()


def test_sessions_can_be_ended_one_by_one_and_everywhere(browser, site):  # noqa: F811
    site.signup()
    other = fresh(browser)
    sign_in(other, site.base, "owner@acme.test")
    expect()(other.get_by_role("button", name="Settings")).to_be_visible()
    page = fresh(browser)
    sign_in(page, site.base, "owner@acme.test")
    settings_security(page)
    rows = page.get_by_test_id("security-sessions").locator("tbody tr")
    expect()(rows).to_have_count(3)  # the signup over the API, the other browser, this one
    theirs = rows.filter(has_text="Mozilla").filter(has=page.get_by_role("button", name="Sign out"))
    theirs.get_by_role("button", name="Sign out").click()
    expect()(rows).to_have_count(2)
    assert site.http.get("/auth/me", headers={"Authorization": f"Bearer {cookie_session.token_of(other.context)}"}
                         ).status_code == 401
    page.get_by_role("button", name="Sign out everywhere").click()
    expect()(page.get_by_role("button", name="Enter the console")).to_be_visible()


def test_an_admin_makes_a_token_sees_it_once_and_revokes_it(browser, site):  # noqa: F811
    owner = site.signup()
    page = fresh(browser)
    cookie_session.place(page.context, site.base, owner)
    page.goto(f"{site.base}/console/")
    settings_security(page)
    box = page.get_by_test_id("security-tokens")
    box.get_by_label("token name").fill("nightly-ci")
    box.get_by_role("button", name="Make a token").click()
    token = page.get_by_test_id("token-once").locator("pre").inner_text()
    assert token.startswith("iwt_")
    assert site.http.get("/workspaces", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    page.get_by_test_id("token-once").get_by_role("button", name="Done").click()
    expect()(page.get_by_test_id("token-once")).to_have_count(0)
    box.get_by_role("button", name="Revoke").click()
    expect()(box.get_by_text("No tokens yet.")).to_be_visible()
    assert site.http.get("/workspaces", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_the_auth_audit_lists_sign_ins_and_exports_the_chain(browser, site):  # noqa: F811
    site.signup()
    site.http.post("/auth/login", json={"email": "owner@acme.test", "password": "wrong-password-1"})
    page = fresh(browser)
    sign_in(page, site.base, "owner@acme.test")
    settings_security(page)
    box = page.get_by_test_id("security-audit")
    expect()(page.get_by_test_id("auth-chain")).to_contain_text("chain intact")
    expect()(box.locator("tbody tr", has_text="auth.fail")).to_have_count(1)
    expect()(box.locator("tbody tr", has_text="auth.login")).to_have_count(1)
    with page.expect_download() as got:
        box.get_by_role("button", name="Export the chain").click()
    lines = [json.loads(x) for x in Path(got.value.path()).read_text().splitlines()]
    assert lines[-1]["verified"] is True and any(x.get("action") == "auth.fail" for x in lines)


def test_an_org_that_requires_two_step_sends_a_member_to_set_it_up(browser, site):  # noqa: F811
    owner_api = site.signup()
    page = fresh(browser)
    sign_in(page, site.base, "owner@acme.test")
    settings_security(page)
    box = page.get_by_test_id("security-mfa")
    box.get_by_role("button", name="Set it up").click()
    secret = page.get_by_test_id("mfa-secret").inner_text()
    box.get_by_label("code from the app").fill(code_now(secret))
    box.get_by_role("button", name="Turn it on").click()
    page.get_by_role("button", name="I saved them").click()
    page.get_by_test_id("security-org").get_by_role("button", name="Require it").click()
    expect()(page.get_by_test_id("security-org")).to_contain_text("Required.")

    owner = cookie_session.session_of(page)
    member = site.join({**owner_api, **owner}, "m@acme.test", "member")
    assert member["org_slug"] == owner["org_slug"]
    them = fresh(browser)
    sign_in(them, site.base, "m@acme.test")
    expect()(them.get_by_role("heading", name="Set up two-step sign in")).to_be_visible()
    expect()(them.get_by_role("button", name="Settings")).to_have_count(0)
    them.get_by_role("button", name="Set it up").click()
    theirs = them.get_by_test_id("mfa-secret").inner_text()
    them.get_by_label("code from the app").fill(code_now(theirs))
    them.get_by_role("button", name="Turn it on").click()
    expect()(them.get_by_role("button", name="Settings")).to_be_visible()


def test_a_lost_phone_moves_to_a_new_one_with_a_recovery_code(browser, site):  # noqa: F811
    site.signup()
    page = fresh(browser)
    sign_in(page, site.base, "owner@acme.test")
    settings_security(page)
    box = page.get_by_test_id("security-mfa")
    box.get_by_role("button", name="Set it up").click()
    old = page.get_by_test_id("mfa-secret").inner_text()
    box.get_by_label("code from the app").fill(code_now(old))
    box.get_by_role("button", name="Turn it on").click()
    codes = page.get_by_test_id("recovery-codes").locator("pre").inner_text().split()
    page.get_by_role("button", name="I saved them").click()
    box.get_by_label("current code").fill(codes[0])
    box.get_by_role("button", name="Move to a new phone").click()
    new = page.get_by_test_id("mfa-secret").inner_text()
    assert new != old
    box.get_by_label("code from the app").fill(code_now(new))
    box.get_by_role("button", name="Turn it on").click()
    expect()(page.get_by_test_id("recovery-codes")).to_be_visible()
