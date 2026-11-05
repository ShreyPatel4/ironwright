"""What an operator can see, and what they must never see.

These go through harden(create_platform_app()) with the door closed, because a
dev-open app builds its scope differently: BindPrincipalHeader only copies the
ASGI scope when there are real sessions, and that copy hides the route and the
session from anything outside it. Jobs run on a worker with its own engine,
because the real worker is another process and shares only the database with
the API that serves /metrics.

A leak test proves its canary reached the place it could leak from first.
A scan of output that never held the secret proves nothing.
"""
from __future__ import annotations

import http.server
import json
import shutil
import threading
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ironwright import config
from ironwright import workspaces_tenant as wt
from ironwright.api import harden
from ironwright.jobs import handlers
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import DEV_ORG_ID, quotas, usage
from ironwright.tracing import load_trace

PASSWORD = "correct-horse-9"
TOKEN = "operator-scrape-token-1234"
KEY_CANARY = "sk-ant-planted-key-canary-Zq42"
CELL_CANARY = "PLANTEDCELLCANARY"
REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "tenant_ws"
SCRAPE = {"Authorization": f"Bearer {TOKEN}"}


class Collector:
    """The smallest honest OTLP backend: keeps what it is sent."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        bucket = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                bucket.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def spans(self) -> list[dict]:
        return [s for req in self.requests for rs in req["resourceSpans"]
                for ss in rs["scopeSpans"] for s in ss["spans"]]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def collector(monkeypatch):
    c = Collector()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", c.endpoint)
    yield c
    c.close()


@pytest.fixture()
def client(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_METRICS_TOKEN", TOKEN)
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with TestClient(harden(create_platform_app()), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def handler(monkeypatch):
    monkeypatch.setattr(handlers, "HANDLERS", dict(handlers.HANDLERS))
    return handlers.job


def signup(c, name: str = "a") -> dict:
    r = c.post("/auth/signup", json={"email": f"{name}@{name}.example", "password": PASSWORD,
                                     "org_name": f"Org {name}"})
    assert r.status_code == 201, r.text
    return r.json()


def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


def log_lines(capsys) -> list[dict]:
    return log_lines_from(capsys.readouterr().out)


def log_lines_from(text: str) -> list[dict]:
    """Every line the process wrote that is one of ours. Stage print()s are not JSON."""
    out = []
    for line in text.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "event" in parsed:
            out.append(parsed)
    return out


def run_worker(job_id: str) -> None:
    """Its own engine, as the separate worker process has."""
    store = JobStore(fresh_engine=True)
    try:
        assert Worker(store=store, heartbeat_interval=30).run_once(job_id) is not None
    finally:
        store.engine.dispose()


def scrape(c) -> str:
    r = c.get("/metrics", headers=SCRAPE)
    assert r.status_code == 200, r.text
    return r.text


def value(text: str, series: str) -> float:
    """A series that was never written reads as 0, as Prometheus's rate() treats it."""
    for line in text.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def tenant_workspace(org_id: str, slug: str = "acme", planted_cell: str | None = None) -> str:
    dest = config.uploads_root(org_id) / "fixture"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE / "uploads" / "fixture", dest)
    if planted_cell:
        with open(dest / "claims.csv", "a", encoding="utf-8") as f:
            f.write(f'C-9009,P-100,"{planted_cell}",2026-01-07,open,\n')
    models = {layer: {f.name: f.read_text() for f in sorted((FIXTURE / "models" / layer).glob("*.sql"))}
              for layer in ("silver", "gold")}
    wt.create_workspace(org_id, slug, yaml.safe_load((FIXTURE / "workspace.yaml").read_text()), models=models,
                        ontology=yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
                        policies=yaml.safe_load((FIXTURE / "policies.yaml").read_text()))
    return slug


# ------------------------------------------------------------------- request logs
def test_a_request_line_names_the_request_org_user_route_status_and_time(client, capsys):
    a = signup(client)
    capsys.readouterr()
    r = client.get("/jobs", headers=bearer(a))
    line = [x for x in log_lines(capsys) if x["event"] == "request"][-1]
    assert line["request_id"] == r.headers["x-request-id"]
    assert line["org_id"] == a["org_id"]
    assert line["user_id"] == a["user_id"]
    assert line["route"] == "/jobs"
    assert line["status"] == 200
    assert line["duration_ms"] >= 0


def test_a_line_with_no_session_has_no_identity(client, capsys):
    client.get("/jobs")
    line = [x for x in log_lines(capsys) if x["event"] == "request"][-1]
    assert (line["org_id"], line["user_id"], line["status"]) == (None, None, 401)


def test_a_route_is_logged_as_its_template_never_the_path_or_the_query(client, capsys):
    a = signup(client)
    capsys.readouterr()
    client.get(f"/jobs/{CELL_CANARY}?q={CELL_CANARY}", headers=bearer(a))
    client.get(f"/nothing/{CELL_CANARY}", headers=bearer(a))
    out = capsys.readouterr().out
    assert CELL_CANARY not in out
    assert [json.loads(x)["route"] for x in out.splitlines()] == ["/jobs/{job_id}", "unmatched"]


def test_the_client_cannot_choose_its_request_id(client):
    r = client.get("/auth/status", headers={"X-Request-Id": KEY_CANARY})
    assert r.headers["x-request-id"] != KEY_CANARY


def test_a_refusal_from_the_hardening_is_logged_and_keeps_its_security_headers(client, capsys):
    r = client.post("/jobs", headers={"Origin": "https://evil.example"}, json={})
    line = [x for x in log_lines(capsys) if x["event"] == "request"][-1]
    assert r.status_code == 403 and line["status"] == 403
    assert r.headers["x-content-type-options"] == "nosniff"


def test_a_crash_is_logged_by_type_and_not_by_message(client, capsys):
    a = signup(client)

    @client.app.get("/boom")
    def boom():
        raise ValueError(f"cannot read {CELL_CANARY}")

    capsys.readouterr()
    r = client.get("/boom", headers=bearer(a))
    assert r.status_code == 500 and r.headers["x-content-type-options"] == "nosniff"
    assert CELL_CANARY not in r.text
    captured = capsys.readouterr()
    assert CELL_CANARY not in captured.out + captured.err
    crash = next(x for x in log_lines_from(captured.out) if x["event"] == "request.crashed")
    assert crash["error_type"] == "ValueError" and crash["org_id"] == a["org_id"]
    assert crash["request_id"] == r.headers["x-request-id"]
    assert any("test_obs.py" in frame for frame in crash["stack"]), "operators still get file and line"


def test_a_crash_does_not_reach_the_servers_own_traceback_log(client):
    """uvicorn logs whatever comes out of the app, message included."""
    a = signup(client)

    @client.app.get("/boom")
    def boom():
        raise ValueError(CELL_CANARY)

    strict = TestClient(client.app, raise_server_exceptions=True)
    assert strict.get("/boom", headers=bearer(a)).status_code == 500


def test_health_probes_write_no_log_lines(meta_db, monkeypatch, capsys):
    from ironwright.hosted import create_hosted_app

    with TestClient(create_hosted_app()) as c:
        assert c.get("/healthz").status_code == 200
    assert log_lines(capsys) == []


# ----------------------------------------------------------------------- no leaks
def test_a_key_sent_to_every_shape_of_the_key_route_never_reaches_a_log(client, capsys):
    a = signup(client)
    path = f"/orgs/{a['org_slug']}/llm-key"
    good = client.put(path, headers=bearer(a), json={"provider": "anthropic", "key": KEY_CANARY})
    assert good.status_code == 200, good.text
    # The shapes a validation error would echo, and the key as a bearer token.
    client.put(path, headers=bearer(a), json={"provider": "nobody", "key": KEY_CANARY})
    client.put(path, headers=bearer(a), json={"key": [KEY_CANARY]})
    client.get("/jobs", headers={"Authorization": f"Bearer {KEY_CANARY}"})
    captured = capsys.readouterr()
    assert captured.out.count('"event": "request"') >= 4
    assert KEY_CANARY not in captured.out + captured.err


def test_a_session_token_never_reaches_a_log(client, capsys):
    a = signup(client)
    client.get("/jobs", headers=bearer(a))
    captured = capsys.readouterr()
    assert a["token"] not in captured.out + captured.err


def test_a_failed_job_logs_the_type_while_the_org_keeps_the_message(client, handler, capsys):
    a = signup(client)

    @handler("boom")
    def boom(ctx, workspace, params, log):
        raise ValueError(f"could not parse {CELL_CANARY}")

    store = JobStore()
    job_id = store.enqueue(a["org_id"], "ws", "boom", f"user:{a['user_id']}", {})
    capsys.readouterr()
    run_worker(job_id)

    job = store.get(a["org_id"], job_id)
    assert CELL_CANARY in job["error"], "the canary never reached the error, so this test proves nothing"
    assert CELL_CANARY in store.read_log(a["org_id"], job_id)["data"]
    captured = capsys.readouterr()
    assert CELL_CANARY not in captured.out + captured.err
    done = next(x for x in log_lines_from(captured.out) if x["event"] == "job.finished")
    assert done["error_type"] == "ValueError" and done["state"] == "failed"


def test_a_failed_runs_exception_message_stays_on_the_orgs_disk(client, collector, monkeypatch, capsys):
    """Tracer writes str(e) into the span on disk. That is inside the org's prefix. A collector is not."""
    a = signup(client)
    slug = tenant_workspace(a["org_id"])

    def blows_up(org_id, name, mode="append"):
        tracer = handlers.Tracer(wt.load_workspace(org_id, name).paths.traces)
        with tracer.span("migrate"):
            raise ValueError(f"could not parse {CELL_CANARY}")

    monkeypatch.setattr(wt, "run_workspace", blows_up)
    store = JobStore()
    job_id = store.enqueue(a["org_id"], slug, "run", f"user:{a['user_id']}", {})
    run_worker(job_id)

    assert store.get(a["org_id"], job_id)["state"] == "failed"
    on_disk = wt.load_workspace(a["org_id"], slug).paths.traces / f"{job_id}.jsonl"
    assert CELL_CANARY in on_disk.read_text(), "the canary never reached the trace, so this test proves nothing"
    assert collector.requests, "nothing was exported, so there is nothing to scan"
    assert CELL_CANARY not in json.dumps(collector.requests)
    assert CELL_CANARY not in capsys.readouterr().out
    failed = next(s for s in collector.spans() if s["name"] == "migrate")
    assert failed["status"]["code"] == 2, "a scrubbed span still says it failed"
    assert [e["attributes"] for e in failed["events"] if e["name"] == "exception"] == [
        [{"key": "type", "value": {"stringValue": "ValueError"}}]]


def test_a_real_run_over_a_planted_cell_keeps_it_out_of_logs_and_the_collector(client, collector, capsys):
    a = signup(client)
    slug = tenant_workspace(a["org_id"], planted_cell=CELL_CANARY)
    upload = config.uploads_root(a["org_id"]) / "fixture" / "claims.csv"
    assert CELL_CANARY in upload.read_text(), "the canary never reached the upload"
    job_id = client.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": slug}).json()["id"]
    run_worker(job_id)
    job = JobStore().get(a["org_id"], job_id)
    assert job["state"] == "succeeded", job
    lake = config.tenants_root() / a["org_id"] / "workspaces" / slug / "run" / "lake"
    assert any(CELL_CANARY.encode() in f.read_bytes() for f in lake.rglob("*.parquet")), (
        "the pipeline never handled the canary, so this test proves nothing")
    captured = capsys.readouterr()
    assert CELL_CANARY not in captured.out + captured.err
    assert collector.requests, "nothing was exported, so there is nothing to scan"
    assert CELL_CANARY not in json.dumps(collector.requests)


def test_a_tenants_names_stay_out_of_the_collector(client, collector):
    """Tables, rules and check conditions are written by the tenant. Numbers and our own words only."""
    a = signup(client)
    slug = tenant_workspace(a["org_id"], slug="secret-name-ws")
    job_id = client.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": slug}).json()["id"]
    run_worker(job_id)
    wire = json.dumps(collector.requests)
    assert "secret-name-ws" not in wire
    assert "claims" not in wire  # the fixture's own source name


def test_another_loggers_exception_argument_is_written_as_its_type(client, capsys):
    """sqlguard logs the raw DuckDB error, which can quote a cell. The formatter is the last stop."""
    import logging

    logging.getLogger("ironwright.sql").error("sql error %s: %s", "e1", ValueError(f"bad {CELL_CANARY}"))
    out = capsys.readouterr().out
    assert CELL_CANARY not in out
    line = json.loads(out.splitlines()[-1])
    assert line["event"] == "sql error e1: ValueError"


# -------------------------------------------------------------------------- /metrics
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                                     {"Authorization": "Bearer "}, {"Authorization": "Basic x"}])
def test_metrics_refuses_without_the_token(client, headers):
    r = client.get("/metrics", headers=headers)
    assert r.status_code == 401
    assert "ironwright_" not in r.text


def test_metrics_refuses_a_non_ascii_token_without_crashing(client):
    r = client.get("/metrics", headers={"Authorization": "Bearer café".encode("latin-1")})
    assert r.status_code == 401


def test_metrics_refuses_a_tenant_owners_session(client):
    assert client.get("/metrics", headers=bearer(signup(client))).status_code == 401


def test_metrics_refuses_everyone_when_no_token_is_set(client, monkeypatch):
    for unset in (None, ""):
        if unset is None:
            monkeypatch.delenv("IRONWRIGHT_METRICS_TOKEN")
        else:
            monkeypatch.setenv("IRONWRIGHT_METRICS_TOKEN", unset)
        assert client.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 401
        assert client.get("/metrics", headers=SCRAPE).status_code == 401


def test_metrics_stays_shut_on_a_dev_open_app(client, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    assert client.get("/metrics").status_code == 401


def test_metrics_speaks_prometheus_and_keeps_the_security_headers(client):
    r = client.get("/metrics", headers=SCRAPE)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "# TYPE ironwright_requests_total counter" in r.text


def test_metrics_only_answers_get(client):
    assert client.post("/metrics", headers=SCRAPE).status_code == 405


def test_metrics_is_not_a_route_a_tenant_could_be_swept_past(client):
    """The isolation sweep walks app.routes. This answers before them, like the health routes."""
    assert "/metrics" not in {getattr(r, "path", None) for r in client.app.routes}


def test_requests_count_by_route_template_and_status(client):
    a = signup(client)
    series = 'ironwright_requests_total{route="/jobs",status="200"}'
    before = value(scrape(client), series)
    client.get("/jobs", headers=bearer(a))
    client.get("/jobs", headers=bearer(a))
    assert value(scrape(client), series) == before + 2


def test_a_run_moves_the_job_counters_from_another_process(client, handler):
    a = signup(client)

    @handler("tick")
    def tick(ctx, workspace, params, log):
        return {"ok": True}

    store = JobStore()
    done = 'ironwright_jobs{kind="tick",state="succeeded"}'
    queued = 'ironwright_jobs{kind="tick",state="queued"}'
    job_id = store.enqueue(a["org_id"], "ws", "tick", f"user:{a['user_id']}", {})
    before = scrape(client)
    assert value(before, queued) == 1
    assert value(before, "ironwright_queue_depth") == 1
    assert value(before, "ironwright_queue_oldest_age_seconds") >= 0
    run_worker(job_id)
    after = scrape(client)
    assert value(after, done) == 1 and value(after, queued) == 0
    assert value(after, "ironwright_queue_depth") == 0
    assert value(after, 'ironwright_job_duration_seconds_count{kind="tick",state="succeeded"}') == 1
    assert value(after, 'ironwright_job_duration_seconds_sum{kind="tick",state="succeeded"}') >= 0


def test_a_real_run_job_moves_the_counters(client):
    a = signup(client)
    slug = tenant_workspace(a["org_id"])
    series = 'ironwright_jobs{kind="run",state="succeeded"}'
    before = value(scrape(client), series)
    job_id = client.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": slug}).json()["id"]
    run_worker(job_id)
    assert JobStore().get(a["org_id"], job_id)["state"] == "succeeded"
    assert value(scrape(client), series) == before + 1


def test_llm_tokens_read_by_org_and_source_from_the_shared_table(client):
    a, b = signup(client, "a"), signup(client, "b")
    usage.add(a["org_id"], "llm_tokens", 1_790_000_000.0, 100)
    usage.add(a["org_id"], "llm_tokens", 1_790_000_000.0 + 86_400, 50)  # the counter crosses days
    usage.add(a["org_id"], "llm_tokens_byo", 1_790_000_000.0, 7)
    usage.add(b["org_id"], "llm_tokens", 1_790_000_000.0, 3)
    usage.add(a["org_id"], "runs", 1_790_000_000.0, 9)
    text = scrape(client)
    assert value(text, f'ironwright_llm_tokens_total{{org_id="{a["org_id"]}",source="platform"}}') == 150
    assert value(text, f'ironwright_llm_tokens_total{{org_id="{a["org_id"]}",source="byo"}}') == 7
    assert value(text, f'ironwright_llm_tokens_total{{org_id="{b["org_id"]}",source="platform"}}') == 3
    assert 'source="runs"' not in text


def test_a_big_counter_keeps_every_digit(client):
    """rate() reads flat between the steps of a rounded counter."""
    a = signup(client)
    usage.add(a["org_id"], "llm_tokens", 1_790_000_000.0, 1_234_567)
    line = f'ironwright_llm_tokens_total{{org_id="{a["org_id"]}",source="platform"}} 1234567'
    assert line in scrape(client).splitlines()


def test_quota_refusals_count_and_a_login_limit_refusal_does_not(client, monkeypatch):
    monkeypatch.setitem(quotas.PLANS, "free", {**quotas.PLANS["free"], "runs": 1})
    a = signup(client)
    slug = tenant_workspace(a["org_id"])
    body = {"kind": "run", "workspace": slug}
    assert client.post("/jobs", headers=bearer(a), json=body).status_code == 200
    assert client.post("/jobs", headers=bearer(a), json=body).status_code == 429
    text = scrape(client)
    assert value(text, f'ironwright_quota_refusals_total{{org_id="{a["org_id"]}",route="/jobs"}}') == 1

    door = 'ironwright_quota_refusals_total{org_id="unknown",route="/auth/login"}'
    before = value(text, door)
    codes = [client.post("/auth/login", json={"email": "x@x.example", "password": "no"}).status_code
             for _ in range(12)]
    assert 429 in codes
    text = scrape(client)
    assert value(text, door) == before
    assert 'ironwright_quota_refusals_total{org_id="unknown"' not in text
    assert value(text, 'ironwright_requests_total{route="/auth/login",status="429"}') >= 1


def test_a_big_duration_keeps_its_precision():
    from ironwright.obs.metrics import _sample

    assert _sample("x", 1234567.25) == "x 1234567.25"
    assert _sample("x", 0.5, a="b") == 'x{a="b"} 0.5'


def test_label_values_are_escaped(client):
    JobStore().enqueue(signup(client)["org_id"], "ws", 'odd"kind\\\nx', "user:x", {})
    text = scrape(client)
    assert 'ironwright_jobs{kind="odd\\"kind\\\\\\nx",state="queued"} 1' in text


def test_a_dead_database_still_answers_with_what_this_process_knows(client, monkeypatch):
    from ironwright.obs import metrics

    def down() -> list[str]:
        raise OSError(f"cannot reach postgres://u:{KEY_CANARY}@db")

    monkeypatch.setattr(metrics, "_database_lines", down)
    r = client.get("/metrics", headers=SCRAPE)
    assert r.status_code == 200
    assert value(r.text, "ironwright_metrics_database_up") == 0
    assert "# TYPE ironwright_requests_total counter" in r.text
    assert KEY_CANARY not in r.text


# --------------------------------------------------------------------------- traces
def test_a_tenant_run_job_is_one_trace_with_stage_spans(client, collector):
    a = signup(client)
    slug = tenant_workspace(a["org_id"])
    job_id = client.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": slug}).json()["id"]
    run_worker(job_id)

    assert len(collector.requests) == 1
    spans = collector.spans()
    assert {s["traceId"] for s in spans} == {job_id}, "an operator goes from job id to trace with no lookup"
    roots = [s for s in spans if "parentSpanId" not in s]
    assert [r["name"] for r in roots] == ["job.run"]
    ids = {s["spanId"] for s in spans}
    assert all(s["parentSpanId"] in ids for s in spans if s is not roots[0])
    children = {s["name"] for s in spans if s.get("parentSpanId") == roots[0]["spanId"]}
    assert {"migrate", "reconcile"} <= children
    attrs = {x["key"]: x["value"] for x in roots[0]["attributes"]}
    assert attrs["org_id"] == {"stringValue": a["org_id"]}
    assert attrs["job.kind"] == {"stringValue": "run"}


def test_no_endpoint_exports_nothing_and_the_job_is_unchanged(client, monkeypatch):
    posted = []
    monkeypatch.setattr("ironwright.otel_export.httpx.Client.post", lambda *a, **k: posted.append(a))
    a = signup(client)
    slug = tenant_workspace(a["org_id"])
    job_id = client.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": slug}).json()["id"]
    run_worker(job_id)
    assert JobStore().get(a["org_id"], job_id)["state"] == "succeeded"
    assert posted == []


def test_an_empty_endpoint_means_unset(client, monkeypatch):
    """Compose passes every optional setting through, so unset arrives as an empty string."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    posted = []
    monkeypatch.setattr("ironwright.otel_export.httpx.Client.post", lambda *a, **k: posted.append(a))
    a = signup(client)
    slug = tenant_workspace(a["org_id"])
    job_id = client.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": slug}).json()["id"]
    run_worker(job_id)
    assert posted == []


def test_a_dead_collector_never_fails_the_job(client, monkeypatch, capsys):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    a = signup(client)
    slug = tenant_workspace(a["org_id"])
    job_id = client.post("/jobs", headers=bearer(a), json={"kind": "run", "workspace": slug}).json()["id"]
    capsys.readouterr()
    run_worker(job_id)
    assert JobStore().get(a["org_id"], job_id)["state"] == "succeeded"
    failed = [x for x in log_lines(capsys) if x["event"] == "trace.export_failed"]
    assert failed and failed[0]["job_id"] == job_id and "error_type" in failed[0]


def test_only_a_run_job_is_exported(client, collector, handler):
    a = signup(client)

    @handler("tick")
    def tick(ctx, workspace, params, log):
        return None

    run_worker(JobStore().enqueue(a["org_id"], "ws", "tick", "user:x", {}))
    assert collector.requests == []


def test_a_demo_run_keeps_each_agent_decision_its_own_trace(tmp_path, monkeypatch, capsys):
    """export.py and the console read one trace per decision. A job root must not swallow them."""
    from ironwright.tenancy import db

    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    try:
        store = JobStore()
        job_id = store.enqueue(DEV_ORG_ID, "retail", "run", "user:x", {})
        run_worker(job_id)
        job = store.get(DEV_ORG_ID, job_id)
        assert job["state"] == "succeeded", job["error"]
        assert job["result"]["evals"]["passed"] is True

        pack = load_pack("retail")
        job_spans = load_trace(pack.paths.traces / f"{job_id}.jsonl")
        assert {f"stage.{s}" for s in handlers.STAGE_ORDER} <= {s["name"] for s in job_spans}
        assert not [s for s in job_spans if s["name"].startswith("agent.")]

        decisions = json.loads((pack.paths.reports / "agents.json").read_text())
        ids = [r["trace_id"] for uc in decisions.values() for r in uc["runs"]]
        assert ids and job_id not in ids and len(set(ids)) == len(ids)
        assert all((pack.paths.traces / f"{t}.jsonl").exists() for t in ids)
    finally:
        db.dispose_current()


# ------------------------------------------------------------------- job lifecycle
def test_a_worker_error_logs_file_and_line_but_not_the_message(client, capsys):
    """A database error quotes the row it choked on, and this used to go to stderr whole."""
    stop = threading.Event()
    store = JobStore()

    def claim(*_a, **_k):
        stop.set()
        raise RuntimeError(f"insert failed, parameters: {CELL_CANARY}")

    store.claim = claim
    capsys.readouterr()
    Worker(store=store, idle=0)._loop(stop)
    captured = capsys.readouterr()
    assert CELL_CANARY not in captured.out + captured.err
    line = next(x for x in log_lines_from(captured.out) if x["event"] == "worker.loop_failed")
    assert line["error_type"] == "RuntimeError" and any("test_obs.py" in f for f in line["stack"])


def test_job_lines_carry_the_ids_from_start_to_finish(client, handler, capsys):
    a = signup(client)

    @handler("tick")
    def tick(ctx, workspace, params, log):
        return None

    job_id = JobStore().enqueue(a["org_id"], "ws", "tick", f"user:{a['user_id']}", {})
    capsys.readouterr()
    run_worker(job_id)
    lines = {x["event"]: x for x in log_lines(capsys) if x.get("job_id") == job_id}
    assert {"job.started", "job.finished"} <= set(lines)
    for line in lines.values():
        assert line["org_id"] == a["org_id"] and line["user_id"] == a["user_id"] and line["kind"] == "tick"
    assert lines["job.finished"]["state"] == "succeeded"
    assert lines["job.finished"]["duration_ms"] >= 0
    assert lines["job.finished"]["error_type"] is None


def test_a_recovered_job_is_logged(client, capsys):
    a = signup(client)
    store = JobStore()
    job_id = store.enqueue(a["org_id"], "ws", "tick", "user:x", {})
    assert store.claim("dead-worker", job_id=job_id)
    capsys.readouterr()
    other = JobStore(fresh_engine=True)
    try:
        Worker(store=other, heartbeat_timeout=-1).run_once("no-such-job")
    finally:
        other.engine.dispose()
    assert [x["job_id"] for x in log_lines(capsys) if x["event"] == "job.requeued"] == [job_id]


# -------------------------------------------------------------------------- compose
def test_the_collector_is_optional_and_pinned():
    compose = yaml.safe_load((REPO / "deploy" / "compose.yaml").read_text())
    services = compose["services"]
    otel = services["otel-collector"]
    assert otel["profiles"] == ["otel"]
    assert ":" in otel["image"] and not otel["image"].endswith(":latest")
    assert "ports" not in otel, "only the API is published"
    assert "profiles" not in services["worker"], "the stack comes up without it"


def test_compose_passes_the_token_and_the_endpoint_to_both_processes():
    text = (REPO / "deploy" / "compose.yaml").read_text()
    hosted = yaml.safe_load(text)["x-hosted-env"]
    assert hosted["IRONWRIGHT_METRICS_TOKEN"] == "${IRONWRIGHT_METRICS_TOKEN:-}"
    assert hosted["OTEL_EXPORTER_OTLP_ENDPOINT"] == "${OTEL_EXPORTER_OTLP_ENDPOINT:-}"


def test_the_operations_page_covers_what_it_promises():
    doc = (REPO / "docs" / "operations.md").read_text()
    for needle in ("ironwright_queue_depth", "IRONWRIGHT_METRICS_TOKEN", "OTEL_EXPORTER_OTLP_ENDPOINT",
                   "job.finished", "/jobs/{id}/log"):
        assert needle in doc
    assert "—" not in doc, "house style: never an em dash"
