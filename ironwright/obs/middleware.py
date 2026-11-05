"""One line per request, the request counters, and /metrics.

It sits outside the app and just inside the security headers, so it sees what the
hardening refuses and its own answers still carry those headers. /metrics answers
here rather than as a route, like the health routes: the app's gate is default deny
for anything without a session, and this door has its own token.

Two things are worked out by hand, because the scope seen here is not the scope the
app ends up with. BindPrincipalHeader copies it when sessions are on, so scope["route"]
and request.state.ctx never come back out. A test on a dev-open app would not notice.
"""
from __future__ import annotations

import logging
import re
import time
import traceback
import uuid
import weakref
from collections.abc import Iterable, Iterator
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import metrics
from .logging import configure, emit

PROBES = frozenset({"/healthz", "/readyz"})  # a load balancer every few seconds; nothing to learn
METRICS_PATH = "/metrics"


def _flatten(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[re.Pattern[str], Any, str]]:
    """(pattern, methods, label) for every route. FastAPI keeps an included router behind
    one object whose matches() names no template, so this walks into it, the way the
    isolation sweep does. If FastAPI moves those attributes, routes read as unmatched
    and tests/test_obs.py goes red."""
    for route in routes:
        included = getattr(route, "original_router", None)
        path = getattr(route, "path", None)
        if included is not None:
            yield from _flatten(included.routes, prefix + route.include_context.prefix)
        elif isinstance(route, Mount):
            yield compile_path(f"{prefix}{path}/{{path:path}}")[0], None, f"{prefix}{path}/*"
        elif path is not None:
            yield compile_path(prefix + path)[0], getattr(route, "methods", None), prefix + path


_tables: weakref.WeakKeyDictionary[Any, tuple[int, list[Any]]] = weakref.WeakKeyDictionary()


def route_label(scope: Scope) -> str:
    """The template, never the path: a path holds tenant names, and every distinct
    label is a new series. Same order as Starlette: first full match, then first partial."""
    app = scope.get("app")
    routes = getattr(getattr(app, "router", None), "routes", None)
    if routes is None:
        return "unmatched"
    cached = _tables.get(app)
    if cached is None or cached[0] != len(routes):
        cached = (len(routes), list(_flatten(routes)))
        _tables[app] = cached
    path, partial = scope["path"], None
    for pattern, methods, label in cached[1]:
        if pattern.match(path):
            if methods is None or scope["method"] in methods:
                return label
            partial = partial or label
    return partial or "unmatched"


async def _identity(scope: Scope) -> tuple[str | None, str | None]:
    """Before the app runs, because a logout ends the session it is answering for.
    Only with a token, so a probe or a public page never touches the database."""
    from .. import auth

    if auth.dev_open():
        return auth.DEV_ORG_ID, auth.DEV_USER_ID
    token = auth.bearer_token(Headers(scope=scope))
    if not token:
        return None, None
    try:
        found = await run_in_threadpool(auth.session_ctx, token)
    except Exception as e:  # noqa: BLE001  logging who asked must never fail the asking
        emit("obs.identity_failed", level=logging.WARNING, error_type=type(e).__name__)
        return None, None
    return (found.org_id, found.user_id) if found else (None, None)


async def _metrics_response(scope: Scope) -> Response:
    if not metrics.authorized(Headers(scope=scope).get("authorization", "")):
        return JSONResponse({"detail": "this needs the operator token"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    if scope["method"] != "GET":
        return JSONResponse({"detail": "method not allowed"}, status_code=405, headers={"Allow": "GET"})
    return Response(await run_in_threadpool(metrics.render), media_type=metrics.CONTENT_TYPE)


class RequestObs:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        configure()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PROBES:
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        # Always ours. A client's id would be client text in every line.
        request_id = uuid.uuid4().hex
        status: list[int] = []

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        if scope["path"] == METRICS_PATH:
            response = await _metrics_response(scope)
            await response(scope, receive, tagged)
            self._finish(scope, request_id, None, None, status[0], started, None,
                         log=status[0] != 200)  # a scrape every few seconds is not news, a refused one is
            return

        org_id, user_id = await _identity(scope)
        error_type = None
        try:
            await self.app(scope, receive, tagged)
        except Exception as e:  # noqa: BLE001
            # Answered here and not raised, because the server would log the traceback with
            # its message, and a message can quote a cell. The file and line are in our line.
            error_type = type(e).__name__
            emit("request.crashed", level=logging.ERROR, request_id=request_id, org_id=org_id, user_id=user_id,
                 route=route_label(scope), error_type=error_type, stack=traceback.format_tb(e.__traceback__)[-5:])
            if not status:
                await JSONResponse({"detail": "internal error", "request_id": request_id},
                                   status_code=500)(scope, receive, tagged)
        self._finish(scope, request_id, org_id, user_id, status[0] if status else 500, started, error_type)

    @staticmethod
    def _finish(scope: Scope, request_id: str, org_id: str | None, user_id: str | None, status: int,
                started: float, error_type: str | None, log: bool = True) -> None:
        from ..api import DOORS

        seconds = time.perf_counter() - started
        route = route_label(scope)
        metrics.record_request(route, status, seconds)
        if status == 429 and scope["path"] not in DOORS:  # the login limit is not a plan limit
            metrics.record_quota_refusal(org_id, route)
        if log:
            fields: dict[str, Any] = {"request_id": request_id, "org_id": org_id, "user_id": user_id,
                                      "method": scope["method"], "route": route, "status": status,
                                      "duration_ms": round(seconds * 1000, 2)}
            if error_type:
                fields["error_type"] = error_type
            emit("request", **fields)
