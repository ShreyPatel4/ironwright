"""Serving under a path prefix, such as coconutlabs.org/projects/ironwright/app.

The rewrite in front keeps the path, so a request arrives as <prefix>/workspaces. The same
server also answers at the root of its own host. So the prefix is optional on every request:
when it is there, this strips it, and the app below sees exactly the paths it sees at home.
root_path stays empty on purpose. Several middlewares compare scope["path"] as it is.

Two things leave the app and have to learn the prefix again:
  - a redirect, whose Location is a path, or an absolute URL on the Host this server saw
    (behind the rewrite that host is not the browser's, so it goes back to a path);
  - the console page, which names its base in a <base href>, set where it is served.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import setting

_PLAIN = re.compile(r"(/[A-Za-z0-9._~-]+)+")
STATE_KEY = "base_path"


def prefix() -> str:
    """IRONWRIGHT_BASE_PATH as /a/b, or "" for none."""
    raw = (setting("BASE_PATH") or "").strip()
    if raw in ("", "/"):
        return ""
    if raw.startswith("//"):
        raise ValueError(f"IRONWRIGHT_BASE_PATH must be a plain path such as /projects/ironwright/app, not {raw!r}")
    value = "/" + raw.strip("/")
    if not _PLAIN.fullmatch(value) or any(part in (".", "..") for part in value.split("/")):
        raise ValueError(f"IRONWRIGHT_BASE_PATH must be a plain path such as /projects/ironwright/app, not {raw!r}")
    return value


def of(scope: Scope) -> str:
    """The prefix this request came in under, or "" at the root."""
    return (scope.get("state") or {}).get(STATE_KEY, "")


class BasePath:
    def __init__(self, app: ASGIApp, base: str) -> None:
        self.app = app
        self.base = base

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if not self.base or scope["type"] not in ("http", "websocket") or \
                not (path == self.base or path.startswith(self.base + "/")):
            await self.app(scope, receive, send)
            return
        raw = scope.get("raw_path") or path.encode()
        base_raw = self.base.encode()
        scope = {**scope, "path": path[len(self.base):] or "/",
                 "raw_path": (raw.removeprefix(base_raw)) or b"/",
                 "state": {**(scope.get("state") or {}), STATE_KEY: self.base}}
        host = Headers(scope=scope).get("host", "")

        async def relocated(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "location" in headers:
                    headers["location"] = self._under(headers["location"], host)
            await send(message)

        await self.app(scope, receive, relocated)

    def _under(self, location: str, host: str) -> str:
        if location.startswith("/") and not location.startswith("//"):
            return self.base + location
        parts = urlsplit(location)
        if parts.scheme in ("http", "https") and parts.netloc == host:
            return self.base + (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return location


def mount(app: ASGIApp) -> ASGIApp:
    base = prefix()
    return BasePath(app, base) if base else app
