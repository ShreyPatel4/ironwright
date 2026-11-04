"""The platform app as a server: bind from the environment, health routes outside the gate.

    python -m ironwright.hosted

The health routes answer before the app sees the request, in an ASGI wrapper,
because a load balancer has no session and the app's own gate is default deny.
/healthz says the process is up. /readyz says the database and the object store
answer. A failed check is named and nothing else is said: no URL, no bucket, no
exception text, because those routes are public.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import sys
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import auth, config

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
        except Exception as e:  # the type only: messages carry URLs and credentials
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
    from .platform_api import create_platform_app

    check_posture()
    config.secure_data_root()
    return HealthGate(create_platform_app())


def _is_loopback(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    import uvicorn

    host = config.bind_host()
    if not _is_loopback(host):
        print(f"exposed on {host} because IRONWRIGHT_HOST says so. Read docs/deploy-hosted.md.", file=sys.stderr)
    # proxy_headers off for the same reason as the local servers: a peer must not pick its own address.
    uvicorn.run(create_hosted_app(), host=host, port=config.bind_port(), proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
