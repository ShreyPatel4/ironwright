"""deploy/hosted_smoke.py against a real hosted server under the prefix, as prod runs it.

Prod is invite only, so the smoke cannot sign up there. It walks what a visitor can do
instead, and this proves that walk passes on a server set up like prod and fails loudly when
the prefix is wrong. The full tenant walk runs on the compose stack (make cf-check, CI).
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
from ironwright.tenancy import db

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "deploy" / "hosted_smoke.py"
PREFIX = "/projects/ironwright/app"


@pytest.fixture(scope="module")
def prod_like(tmp_path_factory, request):
    request.getfixturevalue("console_dist")
    root = tmp_path_factory.mktemp("smoke")
    mp = pytest.MonkeyPatch()
    for name in ("IRONWRIGHT_DEV_OPEN", "IRONWRIGHT_DB_URL", "IRONWRIGHT_ENV", "IRONWRIGHT_LAKE_URI"):
        mp.delenv(name, raising=False)
    mp.setenv("IRONWRIGHT_DATA", str(root))
    mp.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    mp.setenv("IRONWRIGHT_BASE_PATH", PREFIX)
    mp.setenv("IRONWRIGHT_SIGNUP", "invite")
    mp.setenv("IRONWRIGHT_DEMO_ORG", "ironwright-demo")
    mp.chdir(root)
    cli_main(["run", "--pack", "retail"])
    server = uvicorn.Server(uvicorn.Config(hosted.create_hosted_app(), host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(10)
    db.dispose_current()
    mp.undo()


def _smoke(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SMOKE), *args], capture_output=True, text=True, timeout=120, check=False)


def test_the_visitor_walk_passes_under_the_prefix(prod_like):
    out = _smoke("--base", prod_like, "--base-path", PREFIX, "--wait", "10", "--packs", "retail")
    assert out.returncode == 0, out.stdout + out.stderr
    for line in ("signup is invite", "a new org is refused", "the demo door opens a viewer seat",
                 "a visitor reads evals retail", "a visitor cannot write", "smoke passed"):
        assert line in out.stdout, out.stdout


def test_the_wrong_prefix_breaks_at_the_first_step(prod_like):
    out = _smoke("--base", prod_like, "--base-path", "/somewhere/else", "--wait", "3", "--packs", "retail")
    assert out.returncode != 0
    assert "BROKEN  /readyz" in out.stdout


def test_the_root_of_the_host_still_answers(prod_like):
    out = _smoke("--base", prod_like, "--wait", "10", "--packs", "retail")
    assert out.returncode == 0, out.stdout + out.stderr
