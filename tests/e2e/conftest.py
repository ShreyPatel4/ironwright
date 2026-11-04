"""The journey in process: same steps as the compose run, with the door closed.

meta_db runs each test on SQLite and on Postgres, and in CI the Postgres half fails
instead of skipping. Jobs run through the real Worker, one claim at a time, the same
way submit_run drives them when there is no worker process.
"""
from __future__ import annotations

import json
import time

import journey
import pytest
from fastapi.testclient import TestClient

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
