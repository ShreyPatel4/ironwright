"""The journey in process: same steps as the compose run, with the door closed.

meta_db runs each test on SQLite and on Postgres, and in CI the Postgres half fails
instead of skipping. Jobs run through the real Worker, one claim at a time, the same
way submit_run drives them when there is no worker process.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import journey
import pytest
import yaml
from fastapi.testclient import TestClient

from ironwright import workspaces_tenant as wt
from ironwright.config import ensure_tenant
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.platform_api import create_platform_app


class InProcess:
    """journey.Stack over TestClient."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def call(self, method, path, *, body=None, files=None, token=None, timeout=60.0):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        kw = {}
        if body is not None:
            kw["content"] = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if files is not None:
            kw["files"] = [("files", item) for item in files]
        r = self.client.request(method, path, headers=headers, follow_redirects=False, **kw)
        return journey.Answer(r.status_code, r.content)

    def pump(self, job_id: str) -> None:
        if Worker(JobStore()).run_once(job_id) is None:
            time.sleep(0.05)


@pytest.fixture()
def stack(meta_db):
    with TestClient(create_platform_app(), raise_server_exceptions=False) as client:
        yield InProcess(client)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tenant_ws"


@pytest.fixture()
def tenant_workspace(stack):
    """A contract 3 workspace made behind the API, to reach past the route nobody owns yet."""
    def make(state: journey.State) -> str:
        org_id = journey.expect(stack, "GET", "/auth/me", 200, "setup", token=state.a_token)["org_id"]
        root = ensure_tenant(org_id)
        shutil.copytree(FIXTURE / "uploads" / "fixture", root / "uploads" / "fixture")
        models = {layer: {f.name: f.read_text() for f in (FIXTURE / "models" / layer).glob("*.sql")}
                  for layer in ("silver", "gold")}
        wt.create_workspace(org_id, state.workspace, yaml.safe_load((FIXTURE / "workspace.yaml").read_text()),
                            models=models,
                            ontology=yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
                            policies=yaml.safe_load((FIXTURE / "policies.yaml").read_text()))
        return org_id
    return make
