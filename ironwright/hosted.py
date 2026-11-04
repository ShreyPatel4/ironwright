"""The platform app as a hosted server, behind `ironwright serve-platform`.

It carries S5's posture with one difference: the bind comes from the environment,
since a container has no one to type flags. The rule is the same two keys. An
address that is not loopback needs IRONWRIGHT_HOST and also
IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED=1.

The health routes answer in an ASGI middleware, before the routes run, because a
load balancer has no session and the app's own gate is default deny. S5's hardening
sits outside them, so they still carry the security headers.
/healthz says the process is up. /readyz says the database and the object store
answer. A failed check is named and nothing else is said: no URL, no bucket, no
exception text, because those routes are public.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import auth, config
from .cli import _is_loopback

log = logging.getLogger("ironwright.hosted")
CHECK_TIMEOUT_SECONDS = 2
SERVER_ENVS = ("staging", "prod")


def check_posture() -> None:
    """DEV_OPEN turns the door off. That is for tests, never for a real deployment."""
    if auth.dev_open() and config.setting("ENV", "local") in SERVER_ENVS:
        raise SystemExit(f"refusing to start: IRONWRIGHT_DEV_OPEN=1 with IRONWRIGHT_ENV={config.setting('ENV')}")


def _check_db() -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    url = config.db_url()
    args: dict[str, Any] = {"connect_timeout": CHECK_TIMEOUT_SECONDS} if url.startswith("postgresql") else {}
    engine = create_engine(url, poolclass=NullPool, connect_args=args)
    try:
        with engine.connect() as con:
            con.execute(text("select 1"))
    finally:
        engine.dispose()


def _check_object_store() -> None:
    uri = config.lake_uri()
    if uri is None:
        if not os.access(config.data_root(), os.W_OK):
            raise OSError("data root is not writable")
        return
    from pyarrow import fs

    props = config.s3_properties()
    store = fs.S3FileSystem(access_key=props.get("s3.access-key-id"), secret_key=props.get("s3.secret-access-key"),
                            region=props["s3.region"], endpoint_override=props.get("s3.endpoint"),
                            connect_timeout=CHECK_TIMEOUT_SECONDS, request_timeout=CHECK_TIMEOUT_SECONDS,
                            retry_strategy=fs.AwsStandardS3RetryStrategy(max_attempts=1))
    bucket = uri.removeprefix("s3://").split("/")[0]
    if store.get_file_info(bucket).type != fs.FileType.Directory:
        raise OSError("bucket not found")


def readiness() -> dict[str, str]:
    checks = {}
    for name, check in (("db", _check_db), ("object_store", _check_object_store)):
        try:
            check()
            checks[name] = "ok"
        except Exception as e:  # noqa: BLE001  the type only: messages carry URLs and credentials
            log.warning("readiness check %s failed: %s", name, type(e).__name__)
            checks[name] = "failed"
    return checks


class HealthGate:
    """Answers /healthz and /readyz, passes everything else to the app untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path") if scope["type"] == "http" else None
        if path not in ("/healthz", "/readyz"):
            await self.app(scope, receive, send)
            return
        response = await self._answer(path, scope["method"])
        await response(scope, receive, send)

    @staticmethod
    async def _answer(path: str, method: str) -> JSONResponse:
        if method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "method not allowed"}, status_code=405, headers={"Allow": "GET, HEAD"})
        if path == "/healthz":
            return JSONResponse({"status": "ok"})
        checks = await run_in_threadpool(readiness)
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks},
                            status_code=200 if ready else 503)


def create_hosted_app() -> ASGIApp:
    from .api import harden
    from .platform_api import create_platform_app

    check_posture()
    config.secure_data_root()
    app = create_platform_app()
    app.add_middleware(HealthGate)
    return harden(app)  # added last, so outermost


def serve() -> None:
    import uvicorn

    host = config.bind_host()
    if not _is_loopback(host):
        if config.setting("I_KNOW_THIS_IS_EXPOSED") != "1":
            raise SystemExit(f"refusing to start: IRONWRIGHT_HOST={host!r} is not loopback; "
                             "this also needs IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED=1")
        print(f"WARNING: exposed on {host}. Read docs/deploy-hosted.md first.", file=sys.stderr)
    # proxy_headers off, as in cli._serve: a peer must not pick its own address.
    uvicorn.run(create_hosted_app(), host=host, port=config.bind_port(), proxy_headers=False, server_header=False)
