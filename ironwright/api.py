"""REST API over the ontology. Apps use this; agents use the same calls through MCP.

  GET  /                      pack summary and what the ontology contains
  GET  /objects/{type}        list objects
  GET  /objects/{type}/{id}   one object with provenance, crosswalk and neighbors
  POST /functions/{name}      run a governed read          body: {"args": {...}}
  POST /actions/{name}        run a governed write         body: {"params": {...}, "idempotency_key": "..."}
  GET  /audit                 latest audit records
  GET  /reports/{name}        migrate | reconcile | ontology | agents | evals

The caller's identity comes from the X-Principal header (default user:anonymous),
standing in for a real identity provider. Every call passes the policy engine.

harden() is the serving posture both this app and the platform app run behind:
security headers, CORS, a foreign-origin write check, a body size cap, the kill
switch and a per-IP login limit. See docs/threat-model.md.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from .config import setting
from .ontology.runtime import ActionError, Ontology
from .packs import load_pack
from .tracing import Tracer

MAX_BODY_BYTES = 2 * 1024 * 1024
LOGIN_ATTEMPTS = 10           # per IP, per window
LOGIN_WINDOW_S = 60.0

# The console's own needs and nothing more. frame-src is open to http(s) because
# the BI embed is a URL the user chose, rendered in a sandboxed iframe.
CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-src http: https:; frame-ancestors 'self'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# With the kill switch on, every unsafe request is off unless it is listed here,
# so a route added later is off by default. These read and change nothing that matters.
KILL_SWITCH_READS = re.compile(r"/auth/(login|logout)|/functions/[^/]+|/workspaces/[^/]+/"
                               r"(lake/query|boards/[^/]+/run|governance/check)")


def kill_switch_on() -> bool:
    """Read on every call, so flipping the env of a test or a restarted process just works."""
    return (setting("KILL_SWITCH") or "").strip().lower() not in ("", "0", "false", "off")


def _refuse(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


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
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, with_headers)


class OriginGuard:
    """CORS headers only stop a browser reading the answer. A cross-site form post skips
    preflight and still runs, so an unsafe request from a foreign origin is refused here."""

    def __init__(self, app, allowed: list[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if origin and origin not in self.allowed and urlsplit(origin).netloc != headers.get("host"):
                return await _refuse(403, "foreign origin")(scope, receive, send)
        await self.app(scope, receive, send)


class BodyCap:
    """Counts bytes as they arrive, because a chunked upload has no Content-Length to check."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        seen = 0

        async def counted() -> dict:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    # An HTTPException, because FastAPI turns anything else raised
                    # while it reads the body into a 400.
                    raise HTTPException(413, f"body over {self.limit} bytes")
            return message

        await self.app(scope, counted, send)


class KillSwitch:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (scope["type"] == "http" and scope["method"] not in SAFE_METHODS and kill_switch_on()
                and not KILL_SWITCH_READS.fullmatch(scope["path"])):
            return await _refuse(503, "kill switch on: Ask and actions are off, reads stay up")(scope, receive, send)
        await self.app(scope, receive, send)


class LoginLimit:
    """Keys on the socket's address. X-Forwarded-For is client text, so it never counts."""

    def __init__(self, app, attempts: int = LOGIN_ATTEMPTS, window: float = LOGIN_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.attempts = attempts
        self.window = window
        self.clock = clock
        self.hits: dict[str, deque[float]] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/auth/login":
            now = self.clock()
            for ip in [ip for ip, hits in self.hits.items() if now - hits[-1] >= self.window]:
                del self.hits[ip]     # keeps the table as small as the live attackers
            ip = scope["client"][0] if scope.get("client") else "unknown"
            hits = self.hits.setdefault(ip, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.attempts:
                wait = math.ceil(self.window - (now - hits[0]))
                response = _refuse(429, "too many sign-in attempts", {"retry-after": str(wait)})
                return await response(scope, receive, send)
            hits.append(now)
        await self.app(scope, receive, send)


def harden(app: FastAPI, clock: Callable[[], float] = time.monotonic) -> FastAPI:
    """Install the serving posture. Refusals still carry the security headers, which is
    why those go on last: the last middleware added is the outermost."""
    origin = setting("CONSOLE_ORIGIN")
    if origin == "*":
        raise ValueError("IRONWRIGHT_CONSOLE_ORIGIN must name one origin, not a wildcard")
    allowed = [origin] if origin else []   # the console is same-origin unless told otherwise
    app.add_middleware(LoginLimit, clock=clock)
    app.add_middleware(KillSwitch)
    app.add_middleware(BodyCap)
    app.add_middleware(OriginGuard, allowed=allowed)
    app.add_middleware(CORSMiddleware, allow_origins=allowed, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["authorization", "content-type"])
    app.add_middleware(SecurityHeaders)
    return app


class FnBody(BaseModel):
    args: dict[str, Any] = {}


class ActBody(BaseModel):
    params: dict[str, Any] = {}
    idempotency_key: str | None = None
    on_behalf_of: str | None = None
    approved_by: str | None = None


def create_app(pack_name: str) -> FastAPI:
    pack = load_pack(pack_name)
    tracer = Tracer(pack.paths.traces)
    ont = Ontology(pack, tracer)
    lock = threading.Lock()   # one DuckDB writer
    app = FastAPI(title=f"ironwright: {pack.title}", description=pack.spec.get("story", ""))

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"pack": pack.name, "title": pack.title, "decision": pack.spec.get("decision"),
                "ontology": ont.describe(), "stats": ont.store.stats()}

    @app.get("/objects/{type_}")
    def objects(type_: str, limit: int = 50, offset: int = 0,
                x_principal: str = Header("user:anonymous")) -> list[dict[str, Any]]:
        with lock:
            return [ont.masked({**o, "_type": type_}, x_principal) for o in ont.store.list(type_, limit, offset)]

    @app.get("/objects/{type_}/{id_}")
    def obj(type_: str, id_: str, x_principal: str = Header("user:anonymous")) -> dict[str, Any]:
        with lock:
            o = ont.masked(ont.store.get(type_, id_), x_principal)
            if not o:
                raise HTTPException(404, f"no {type_} {id_}")
            xw = ont.store.con.execute("select system, source_key, method, confidence from crosswalk where id = ?",
                                       [id_]).fetchall()
            hist = ont.store.con.execute("select version, changed_by, action_run, ts from object_history "
                                         "where id = ? order by version", [id_]).fetchall()
            return {**o, "_crosswalk": [dict(zip(("system", "source_key", "method", "confidence"), r)) for r in xw],
                    "_neighbors": ont.store.neighbors(id_),
                    "_history": [dict(zip(("version", "changed_by", "action_run", "ts"), r)) for r in hist]}

    @app.post("/functions/{name}")
    def fn(name: str, body: FnBody, x_principal: str = Header("user:anonymous")) -> dict[str, Any]:
        with lock:
            try:
                return ont.call(name, body.args, principal=x_principal)
            except PermissionError as e:
                raise HTTPException(403, str(e))
            except ActionError as e:
                raise HTTPException(400, str(e))

    @app.post("/actions/{name}")
    def act(name: str, body: ActBody, x_principal: str = Header("user:anonymous")) -> dict[str, Any]:
        with lock:
            try:
                res = ont.act(name, body.params, actor=x_principal, on_behalf_of=body.on_behalf_of,
                              idempotency_key=body.idempotency_key, approved_by=body.approved_by)
            except ActionError as e:
                raise HTTPException(400, str(e))
            if res["status"] in ("applied_pending_sync",):
                res["outbox"] = ont.deliver_outbox()
            return res

    @app.get("/audit")
    def audit(limit: int = 50) -> list[dict[str, Any]]:
        with lock:
            df = ont.store.con.execute("select * from audit_log order by seq desc limit ?", [limit]).fetchdf()
        return json.loads(df.to_json(orient="records"))

    @app.get("/reports/{name}")
    def report(name: str) -> Any:
        p = pack.paths.reports / f"{name}.json"
        if not p.exists():
            raise HTTPException(404, "run the pipeline first")
        return json.loads(p.read_text())

    return app
