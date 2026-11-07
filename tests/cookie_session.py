"""The console's session is an httpOnly cookie, so browser tests place it and read it
through the browser context, never through page script. Shared by the console tests."""
from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

COOKIE = "iw_session"


def place(context, base: str, session: dict[str, Any]) -> None:
    """Give a fresh context a session the way a sign in would have."""
    context.add_cookies([{"name": COOKIE, "value": session["token"], "url": base + "/",
                          "httpOnly": True, "secure": True, "sameSite": "Lax"}])
    context.add_init_script("localStorage.setItem('iw.onboarded', '1');")


def token_of(context) -> str | None:
    return next((c["value"] for c in context.cookies() if c["name"] == COOKIE), None)


def session_of(page) -> dict[str, Any]:
    """What the session answer held: /auth/me over the cookie's token, plus the token."""
    token = token_of(page.context)
    assert token, "no session cookie in this browser"
    r = page.context.request.get(urljoin(page.url, "/auth/me"), headers={"authorization": f"Bearer {token}"})
    assert r.ok, r.text()
    return {**r.json(), "token": token}


def page_cannot_read_it(page) -> bool:
    """The point of the cookie: no script on the page sees the session."""
    token = token_of(page.context)
    return bool(token) and token not in page.evaluate("document.cookie") and \
        token not in page.evaluate("JSON.stringify(Object.assign({}, localStorage))")
