"""deploy/hosted_smoke.py's tenant walk, where signups are open: what cf-check and the compose
hosted-smoke job run. Two orgs sign up, org a uploads, profiles, makes the workspace from the
proposal through POST /workspaces and runs it, and org b is refused at every step. A real
hosted server under the prefix, and a real job worker on its own thread.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from cryptography.fernet import Fernet
from test_console_settings import (
    console_dist,  # noqa: F401  the smoke checks the console page
)

from ironwright import hosted
from ironwright.cli import main as cli_main
from ironwright.jobs.worker import Worker
from ironwright.tenancy import db

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "deploy" / "hosted_smoke.py"
PREFIX = "/projects/ironwright/app"


@pytest.fixture(scope="module")
def open_stack(tmp_path_factory, request):
    request.getfixturevalue("console_dist")
    root = tmp_path_factory.mktemp("smoke-open")
    mp = pytest.MonkeyPatch()
    for name in ("IRONWRIGHT_DEV_OPEN", "IRONWRIGHT_DB_URL", "IRONWRIGHT_ENV", "IRONWRIGHT_LAKE_URI",
                 "IRONWRIGHT_DEMO_ORG"):
        mp.delenv(name, raising=False)
    mp.setenv("IRONWRIGHT_DATA", str(root))
    mp.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    mp.setenv("IRONWRIGHT_BASE_PATH", PREFIX)
    mp.setenv("IRONWRIGHT_SIGNUP", "open")
    mp.setenv("IRONWRIGHT_RUNS_WAIT", "0")
    mp.chdir(root)
    cli_main(["run", "--pack", "retail"])
    server = uvicorn.Server(uvicorn.Config(hosted.create_hosted_app(), host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    stop = threading.Event()
    worker = Worker(idle=0.1)

    def loop() -> None:
        while not stop.is_set():
            if worker.run_once() is None:
                stop.wait(0.1)

    work = threading.Thread(target=loop, daemon=True)
    work.start()
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    stop.set()
    work.join(30)
    server.should_exit = True
    thread.join(10)
    db.dispose_current()
    mp.undo()


def test_the_tenant_walk_passes_under_the_prefix(open_stack):
    out = subprocess.run([sys.executable, str(SMOKE), "--base", open_stack, "--base-path", PREFIX, "--wait", "10",
                          "--packs", "retail", "--job-seconds", "240"],
                         capture_output=True, text=True, timeout=600, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    for line in ("org a creates the workspace from the proposal", "the run passed its evals",
                 "org b cannot run it", "smoke passed"):
        assert line in out.stdout, out.stdout
    assert any(line.startswith("tenant ") for line in out.stdout.splitlines())
