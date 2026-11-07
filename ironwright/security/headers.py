"""Response headers on every answer, refusals included (enterprise-security.md item 9).

The CSP has no inline script and nobody may frame us. frame-src stays open to
http(s) because the BI embed is a URL the user chose, in a sandboxed iframe.
HSTS is sent on plain http too: a browser ignores it there, and behind the TLS
edge the app cannot tell which one it is on.
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders

CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-src http: https:; frame-ancestors 'none'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'")
HSTS = "max-age=63072000; includeSubDomains"
PERMISSIONS = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
               "magnetometer=(), gyroscope=(), accelerometer=(), interest-cohort=()")
# CORS is closed unless IRONWRIGHT_CONSOLE_ORIGIN names one origin. These are the
# headers that origin may send. X-Principal is not one of them.
CORS_HEADERS = ["authorization", "content-type", "x-csrf-token"]


class SecurityHeaders:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["content-security-policy"] = CSP
                headers["strict-transport-security"] = HSTS
                headers["permissions-policy"] = PERMISSIONS
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
                headers["x-frame-options"] = "DENY"
                headers["cross-origin-opener-policy"] = "same-origin"
            await send(message)

        await self.app(scope, receive, with_headers)
