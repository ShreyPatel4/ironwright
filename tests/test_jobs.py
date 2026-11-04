"""Background jobs (docs/saas-mvp.md contract 4). Every test that touches the
store runs twice, on SQLite and on a real Postgres. Without POSTGRES_URI the
Postgres half skips and says so; it never passes vacuously."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import Counter
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from ironwright import cli
from ironwright.config import data_root
from ironwright.jobs import handlers, routes
from ironwright.jobs import store as store_mod
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.platform_api import create_platform_app


@pytest.fixture()
def handler(monkeypatch):
    """Register a handler for one test only."""
    monkeypatch.setattr(handlers, "HANDLERS", dict(handlers.HANDLERS))
    return handlers.job


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path}/meta.sqlite"
    else:
        uri = request.getfixturevalue("postgres_uri")
        schema = request.getfixturevalue("pg_schema")
        sep = "&" if "?" in uri else "?"
        url = f"{uri}{sep}options=-csearch_path%3D{schema}"
    monkeypatch.setenv("IRONWRIGHT_DB_URL", url)
    yield url
    store_mod.dispose_engines()


@pytest.fixture()
def store(db_url) -> JobStore:
    return JobStore()


def _race(store_factory, workers: int, rounds: int) -> list[str]:
    """Each worker has its own store and engine, so its own connections. All
    start on one barrier and claim until nothing is left, finishing each job
    at once so the cap never hides a double claim."""
    barrier = threading.Barrier(workers)
    claimed: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def loop(n: int) -> None:
        try:
            s = store_factory()
            barrier.wait()
            idle = 0
            while idle < rounds:
                got = s.claim(f"worker-{n}")
                if got is None:
                    idle += 1
                    continue
                idle = 0
                with lock:
                    claimed.append(got["id"])
                s.finish(got["id"], got["token"], "succeeded")
        except BaseException as e:  # noqa: BLE001  surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=loop, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return claimed


def test_two_workers_never_claim_the_same_job(store):
    ids = [store.enqueue(f"org{i}", "retail", "run", "user:a", {}) for i in range(40)]
    claimed = _race(lambda: JobStore(fresh_engine=True), workers=2, rounds=5)
    counts = Counter(claimed)
    assert [j for j, n in counts.items() if n > 1] == []
    assert set(claimed) == set(ids)


def _hold_race(workers: int, tries: int, cap: int) -> list[dict]:
    """Workers keep what they claim, so a claim past a limit stays visible. A
    thread that dies on an error fails the test: a silent death would hide the
    race by leaving fewer claimers."""
    barrier = threading.Barrier(workers)
    held: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def loop(n: int) -> None:
        try:
            s = JobStore(fresh_engine=True)
            barrier.wait()
            for _ in range(tries):
                got = s.claim(f"w{n}", cap=cap)
                if got:
                    with lock:
                        held.append(got)
        except BaseException as e:  # noqa: BLE001  surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=loop, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    return held


def test_race_holds_one_running_per_org(store):
    """SKIP LOCKED alone would let two workers take two different queued jobs
    of one org at the same moment."""
    for org in range(6):
        for _ in range(4):
            store.enqueue(f"org{org}", "retail", "run", "user:a", {})
    held = _hold_race(workers=4, tries=15, cap=100)
    assert len(held) == 6
    assert len({j["org_id"] for j in held}) == 6


def test_race_holds_the_global_cap(store):
    """One job per org, so only the cap can stop a claim. Without the claim
    lock each worker counts two running and all of them take a third."""
    for org in range(30):
        store.enqueue(f"org{org}", "retail", "run", "user:a", {})
    assert len(_hold_race(workers=6, tries=15, cap=3)) == 3


def test_second_job_of_a_busy_org_waits_and_other_orgs_run(store):
    first = store.enqueue("a", "retail", "run", "user:a", {})
    second = store.enqueue("a", "retail", "run", "user:a", {})
    other = store.enqueue("b", "retail", "run", "user:a", {})
    got = store.claim("w", cap=5)
    assert got["id"] == first
    assert store.claim("w", cap=5)["id"] == other
    assert store.claim("w", cap=5) is None
    store.finish(first, got["token"], "succeeded")
    assert store.claim("w", cap=5)["id"] == second


def test_the_database_itself_refuses_two_running_jobs_in_one_org(store):
    a = store.enqueue("a", "retail", "run", "user:a", {})
    b = store.enqueue("a", "retail", "run", "user:a", {})
    with pytest.raises(sa.exc.IntegrityError), store.engine.begin() as c:
        c.execute(store_mod.jobs.update().where(store_mod.jobs.c.id.in_([a, b]))
                  .values(state="running"))


def test_cap_defaults_from_ironwright_workers(store, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_WORKERS", "1")
    store.enqueue("a", "retail", "run", "user:a", {})
    store.enqueue("b", "retail", "run", "user:a", {})
    assert store.claim("w") is not None
    assert store.claim("w") is None


def test_many_processes_can_boot_on_an_empty_database(db_url):
    errors = []
    barrier = threading.Barrier(4)

    def boot() -> None:
        try:
            barrier.wait()
            JobStore(fresh_engine=True)
        except Exception as e:  # noqa: BLE001  surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# --- cancel


def test_cancel_a_queued_job_means_it_never_runs(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    assert store.cancel("a", j) == "cancelled"
    assert store.claim("w") is None
    assert store.get("a", j)["finished_at"]


def test_cancel_a_running_job_only_flips_the_flag(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    store.claim("w")
    assert store.cancel_requested(j) is False
    assert store.cancel("a", j) == "running"
    assert store.cancel_requested(j) is True
    assert store.get("a", j)["state"] == "running"


def test_cancel_does_not_rewrite_a_finished_job(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    got = store.claim("w")
    store.finish(j, got["token"], "succeeded")
    assert store.cancel("a", j) == "succeeded"
    assert store.get("a", j)["state"] == "succeeded"
    assert store.cancel_requested(j) is False


def test_handler_that_polls_stops_when_cancelled(store, handler):
    started = threading.Event()

    @handler("t_poll")
    def poll(ctx, workspace, params, log):
        log("waiting")
        started.set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ctx.check_cancel()
            time.sleep(0.01)
        raise AssertionError("cancel never arrived")

    j = store.enqueue("a", "retail", "t_poll", "user:a", {})
    t = threading.Thread(target=Worker(store).run_once)
    t.start()
    assert started.wait(10)
    store.cancel("a", j)
    t.join(25)
    got = store.get("a", j)
    assert got["state"] == "cancelled"
    assert got["error"] is None


# --- logs


def test_each_job_logs_to_its_own_private_file(store, handler, tmp_path):
    @handler("t_log")
    def say(ctx, workspace, params, log):
        log(f"hello from {params['who']}")

    a = store.enqueue("a", "retail", "t_log", "user:a", {"who": "a"})
    b = store.enqueue("b", "retail", "t_log", "user:a", {"who": "b"})
    w = Worker(store)
    w.run_once()
    w.run_once()
    assert "hello from a" in store.read_log("a", a)["data"]
    assert "hello from b" not in store.read_log("a", a)["data"]
    assert "hello from b" in store.read_log("b", b)["data"]
    path = tmp_path / "tenants" / "a" / "jobs" / f"{a}.log"
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.get("a", a)["log_path"] == f"tenants/a/jobs/{a}.log"


def test_tail_resumes_from_a_byte_offset(store, handler):
    @handler("t_tail")
    def say(ctx, workspace, params, log):
        log("one")
        log("two")

    j = store.enqueue("a", "retail", "t_tail", "user:a", {})
    Worker(store).run_once()
    whole = store.read_log("a", j)
    assert whole["data"].endswith("two\n")
    assert store.read_log("a", j, offset=whole["next_offset"]) == {
        "data": "", "next_offset": whole["next_offset"]}
    assert store.read_log("a", j, offset=whole["next_offset"] - 4)["data"] == "two\n"


def test_tail_never_splits_a_character(store, handler):
    @handler("t_utf")
    def say(ctx, workspace, params, log):
        log("é" * 10)

    j = store.enqueue("a", "retail", "t_utf", "user:a", {})
    Worker(store).run_once()
    size = store.read_log("a", j)["next_offset"]
    data, offset = "", 0
    while offset < size:
        got = store.read_log("a", j, offset=offset, limit=5)
        assert got["next_offset"] > offset
        data += got["data"]
        offset = got["next_offset"]
    assert "é" * 10 in data
    assert "�" not in data


def test_tail_before_any_log_exists_is_empty(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    assert store.read_log("a", j) == {"data": "", "next_offset": 0}


# --- handlers and failure


def test_every_contract_kind_is_registered():
    assert {"run", "profile", "ontology_propose"} <= set(handlers.HANDLERS)


def test_a_kind_cannot_register_twice(handler):
    @handler("t_once")
    def one(ctx, workspace, params, log):
        return None

    with pytest.raises(ValueError):
        handlers.job("t_once")(one)


@pytest.mark.parametrize("kind", ["profile", "ontology_propose"])
def test_stubs_fail_out_loud_instead_of_faking_success(store, kind):
    j = store.enqueue("a", "retail", kind, "user:a", {})
    Worker(store).run_once()
    got = store.get("a", j)
    assert got["state"] == "failed"
    assert "not built" in got["error"]


def test_a_failing_handler_marks_the_job_failed_and_the_worker_lives(store, handler):
    @handler("t_boom")
    def boom(ctx, workspace, params, log):
        raise RuntimeError("kaboom")

    bad = store.enqueue("a", "retail", "t_boom", "user:a", {})
    next_job = store.enqueue("a", "retail", "t_boom", "user:a", {})
    w = Worker(store)
    assert w.run_once()["id"] == bad
    got = store.get("a", bad)
    assert got["state"] == "failed" and "kaboom" in got["error"]
    assert "kaboom" in store.read_log("a", bad)["data"]
    assert w.run_once()["id"] == next_job


def test_an_unknown_kind_fails_instead_of_wedging_the_org(store):
    j = store.enqueue("a", "retail", "no_such_kind", "user:a", {})
    Worker(store).run_once()
    assert store.get("a", j)["state"] == "failed"


def test_handler_receives_contract_arguments(store, handler):
    seen = {}

    @handler("t_args")
    def grab(ctx, workspace, params, log):
        seen.update(org=ctx.org_id, principal=ctx.principal, job=ctx.job_id,
                    workspace=workspace, params=params)
        return {"answer": 42}

    j = store.enqueue("a", "ws1", "t_args", "user:me", {"x": 1})
    Worker(store).run_once()
    assert seen == {"org": "a", "principal": "user:me", "job": j,
                    "workspace": "ws1", "params": {"x": 1}}
    assert store.get("a", j)["result"] == {"answer": 42}


def test_a_foreign_org_may_not_run_a_demo_pack(store):
    """Contract 2: demo packs are read only to every tenant. generate wipes
    the pack's folder, so this is the difference between read and destroy."""
    j = store.enqueue("acme", "retail", "run", "user:a", {"stages": ["generate"]})
    Worker(store).run_once()
    got = store.get("acme", j)
    assert got["state"] == "failed"
    assert "read only" in got["error"]
    assert not (data_root() / "retail").exists()


def test_tenant_workspace_loads_for_its_own_org_and_skips_generate(store, monkeypatch):
    """T2's load_workspace arrives as ironwright.workspaces_tenant. Until it
    merges, stand in only for that one seam."""
    from ironwright.packs import load_pack

    calls: list[tuple[str, str]] = []
    ran: list[str] = []

    def load_workspace(org_id, ws_slug):
        calls.append((org_id, ws_slug))
        pack = load_pack("retail")
        return SimpleNamespace(paths=pack.paths, name="acme_ws", spec={"kind": "workspace"})

    fake = ModuleType("ironwright.workspaces_tenant")
    fake.load_workspace = load_workspace
    monkeypatch.setitem(sys.modules, "ironwright.workspaces_tenant", fake)
    monkeypatch.setattr(handlers, "pipeline_stages", lambda: {
        s: (lambda pack, tracer, args, s=s: ran.append(s))
        for s in ("generate", "migrate", "reconcile")})
    monkeypatch.setattr(handlers, "derive_verification", lambda pack: {"level": "L1"})
    j = store.enqueue("acme", "claims", "run", "user:a", {})
    Worker(store).run_once()
    got = store.get("acme", j)
    assert got["state"] == "succeeded", got["error"]
    assert calls == [("acme", "claims")]
    assert ran == ["migrate", "reconcile"]


# --- crash safety


def test_a_dead_workers_job_is_requeued_only_after_the_timeout(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    store.claim("dead")
    now = time.time()
    assert store.requeue_stale(timeout=60, now=now + 30) == []
    assert store.get("a", j)["state"] == "running"
    assert store.requeue_stale(timeout=60, now=now + 90) == [j]
    got = store.get("a", j)
    assert got["state"] == "queued" and got["started_at"] is None
    assert store.claim("alive")["id"] == j


def test_a_heartbeat_keeps_a_job_alive(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    got = store.claim("w")
    now = time.time()
    assert store.heartbeat(j, got["token"], now=now + 50) is True
    assert store.requeue_stale(timeout=60, now=now + 90) == []


def test_a_zombie_worker_cannot_finish_a_job_it_lost(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    zombie = store.claim("zombie")
    store.requeue_stale(timeout=60, now=time.time() + 90)
    alive = store.claim("alive")
    assert store.heartbeat(j, zombie["token"]) is False
    assert store.finish(j, zombie["token"], "failed", error="late") is False
    assert store.get("a", j)["state"] == "running"
    assert store.finish(j, alive["token"], "succeeded") is True
    assert store.get("a", j)["state"] == "succeeded"


def test_a_job_that_keeps_killing_workers_eventually_fails(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    for _ in range(store_mod.MAX_ATTEMPTS):
        assert store.claim("w")["id"] == j
        store.requeue_stale(timeout=60, now=time.time() + 90)
    got = store.get("a", j)
    assert got["state"] == "failed"
    assert "worker" in got["error"]
    assert store.claim("w") is None


def test_a_cancelled_job_of_a_dead_worker_stays_cancelled(store):
    j = store.enqueue("a", "retail", "run", "user:a", {})
    store.claim("dead")
    store.cancel("a", j)
    store.requeue_stale(timeout=60, now=time.time() + 90)
    assert store.get("a", j)["state"] == "cancelled"
    assert store.claim("w") is None


def test_a_really_killed_worker_process_is_recovered(store, handler, db_url, tmp_path):
    """No simulated crash: a real process claims, then dies with no cleanup."""
    j = store.enqueue("a", "retail", "t_after", "user:a", {})
    code = ("import os; from ironwright.jobs.store import JobStore; "
            "s = JobStore(); assert s.claim('doomed'); os._exit(9)")
    done = subprocess.run([sys.executable, "-c", code], check=False, env=os.environ.copy(), timeout=60)
    assert done.returncode == 9
    assert store.get("a", j)["state"] == "running"

    @handler("t_after")
    def after(ctx, workspace, params, log):
        return {"recovered": True}

    assert Worker(store, heartbeat_timeout=3600).run_once() is None
    assert Worker(store, heartbeat_timeout=0).run_once()["id"] == j
    assert store.get("a", j)["state"] == "succeeded"


def test_worker_heartbeats_while_a_long_handler_runs(store, handler):
    """A stage can block for minutes. If only the job loop beat, every long
    run would look dead and get run twice."""
    release = threading.Event()
    started = threading.Event()

    @handler("t_long")
    def long_one(ctx, workspace, params, log):
        started.set()
        release.wait(20)

    j = store.enqueue("a", "retail", "t_long", "user:a", {})
    w = Worker(store, heartbeat_interval=0.05)
    t = threading.Thread(target=w.run_once)
    t.start()
    assert started.wait(10)
    before = store.last_heartbeat(j)
    deadline = time.monotonic() + 10
    while store.last_heartbeat(j) == before and time.monotonic() < deadline:
        time.sleep(0.02)
    beat = store.last_heartbeat(j) > before
    release.set()
    t.join(20)
    assert beat
    assert store.get("a", j)["state"] == "succeeded"


def test_a_worker_that_lost_its_job_tells_the_handler_to_stop(store, handler):
    started = threading.Event()

    @handler("t_lost")
    def stubborn(ctx, workspace, params, log):
        started.set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ctx.check_cancel()
            time.sleep(0.01)
        raise AssertionError("never told")

    j = store.enqueue("a", "retail", "t_lost", "user:a", {})
    w = Worker(store, heartbeat_interval=0.02)
    t = threading.Thread(target=w.run_once)
    t.start()
    assert started.wait(10)
    store.requeue_stale(timeout=60, now=time.time() + 90)
    t.join(25)
    assert not t.is_alive()
    assert store.get("a", j)["state"] == "queued"


def test_worker_loop_honors_the_cap_and_stops(store, handler, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_WORKERS", "2")
    gate = threading.Event()
    lock = threading.Lock()
    live = [0, 0]  # now, peak

    @handler("t_busy")
    def busy(ctx, workspace, params, log):
        with lock:
            live[0] += 1
            live[1] = max(live)
        gate.wait(5)
        with lock:
            live[0] -= 1

    ids = {f"o{i}": store.enqueue(f"o{i}", "retail", "t_busy", "user:a", {}) for i in range(4)}
    stop = threading.Event()
    w = Worker(store, idle=0.01)
    t = threading.Thread(target=w.run, args=(stop,))
    t.start()
    deadline = time.monotonic() + 10
    while live[1] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    peak = live[1]
    gate.set()
    while time.monotonic() < deadline and any(
            store.get(o, j)["state"] != "succeeded" for o, j in ids.items()):
        time.sleep(0.02)
    stop.set()
    t.join(10)
    assert not t.is_alive()
    assert peak == 2
    assert all(store.get(o, j)["state"] == "succeeded" for o, j in ids.items())


# --- API


@pytest.fixture()
def client(db_url):
    with TestClient(create_platform_app()) as c:
        yield c


def _poll(client, job_id, want="succeeded", seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body["state"] == want:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {body['state']}: {body.get('error')}")


def test_acceptance_post_runs_returns_id_and_the_worker_completes_it(client, capsys):
    r = client.post("/workspaces/retail/runs", json={"seed": 7, "wait": False})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert r.json()["state"] == "queued"
    assert client.get(f"/jobs/{job_id}").json()["state"] == "queued"
    assert not (data_root() / "retail" / "reports" / "evals.json").exists()

    cli.main(["worker", "--once"])

    body = _poll(client, job_id)
    assert {s["stage"] for s in body["result"]["stages"]} == set(handlers.STAGE_ORDER)
    assert body["result"]["evals"]["passed"] is True
    assert body["result"]["verification"]["level"] == "L3"
    tail = client.get(f"/jobs/{job_id}/log").json()
    assert "[evals]" in tail["data"]
    assert client.get(f"/jobs/{job_id}/log", params={"offset": tail["next_offset"]}).json()["data"] == ""
    assert "log_path" not in body


def test_default_post_runs_goes_through_the_queue_and_keeps_the_old_body(client):
    """The console and the older suites read stages from the response. They
    get them, and the run still went through the jobs table and the claim."""
    r = client.post("/workspaces/retail/runs", json={"seed": 7, "stages": ["generate"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace"] == "retail"
    assert [s["stage"] for s in body["stages"]] == ["generate"]
    assert body["state"] == "succeeded"
    assert client.get(f"/jobs/{body['id']}").json()["state"] == "succeeded"


def test_when_hosted_a_client_cannot_force_a_run_inside_the_api(client, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "0")
    r = client.post("/workspaces/retail/runs", json={"seed": 7, "wait": True})
    assert r.status_code == 202
    assert r.json()["state"] == "queued"


def test_a_waited_run_that_fails_is_not_a_200(client, monkeypatch):
    def boom():
        raise RuntimeError("stage broke")

    monkeypatch.setattr(handlers, "pipeline_stages", boom)
    r = client.post("/workspaces/retail/runs", json={"seed": 7})
    assert r.status_code == 500
    assert "stage broke" in r.json()["detail"]["error"]
    assert r.json()["detail"]["id"]


def test_a_waited_run_behind_a_busy_org_returns_queued_not_a_hang(client, store, monkeypatch):
    monkeypatch.setattr(routes, "RUN_WAIT_SECONDS", 0.1)
    store.enqueue(routes.DEV_ORG, "retail", "run", "user:a", {})
    store.claim("other")
    r = client.post("/workspaces/retail/runs", json={"seed": 7})
    assert r.status_code == 202
    assert r.json()["state"] == "queued"


def test_runs_still_rejects_unknown_workspaces_and_stages(client):
    assert client.post("/workspaces/nonsense/runs", json={}).status_code == 404
    bad = client.post("/workspaces/retail/runs", json={"stages": ["nope"], "wait": False})
    assert bad.status_code == 422
    assert client.get("/jobs").json() == []


def test_post_jobs_returns_only_an_id(client):
    r = client.post("/jobs", json={"kind": "profile", "workspace": "claims", "params": {"a": 1}})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"id"}
    got = client.get(f"/jobs/{r.json()['id']}").json()
    assert got["kind"] == "profile" and got["workspace"] == "claims"
    assert got["org_id"] == routes.DEV_ORG
    assert got["created_by"] == "user:console"


@pytest.mark.parametrize("body", [
    {"kind": "nope", "workspace": "retail"},
    {"kind": "run", "workspace": "../../etc"},
    {"kind": "run", "workspace": "retail", "params": {"stages": ["nope"]}},
    {"kind": "run", "workspace": "retail", "params": {"blob": "x" * 70_000}},
])
def test_post_jobs_refuses_bad_input(client, body):
    assert client.post("/jobs", json=body).status_code == 422
    assert client.get("/jobs").json() == []


def test_list_filters_by_workspace_newest_first(client):
    a = client.post("/jobs", json={"kind": "profile", "workspace": "one"}).json()["id"]
    b = client.post("/jobs", json={"kind": "profile", "workspace": "two"}).json()["id"]
    c = client.post("/jobs", json={"kind": "profile", "workspace": "one"}).json()["id"]
    assert [j["id"] for j in client.get("/jobs").json()] == [c, b, a]
    assert [j["id"] for j in client.get("/jobs", params={"workspace": "one"}).json()] == [c, a]


def test_cancel_route_on_a_queued_job(client):
    j = client.post("/jobs", json={"kind": "profile", "workspace": "x"}).json()["id"]
    r = client.post(f"/jobs/{j}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"


def test_log_offset_must_not_be_negative(client):
    j = client.post("/jobs", json={"kind": "profile", "workspace": "x"}).json()["id"]
    assert client.get(f"/jobs/{j}/log", params={"offset": -1}).status_code == 422


def test_another_orgs_job_is_invisible_on_every_route(client, store, handler):
    @handler("t_secret")
    def secret(ctx, workspace, params, log):
        log("acme secret")

    foreign = store.enqueue("acme", "claims", "t_secret", "user:acme", {})
    Worker(store).run_once()
    queued = store.enqueue("acme", "claims", "t_secret", "user:acme", {})
    for j in (foreign, queued):
        assert client.get(f"/jobs/{j}").status_code == 404
        assert client.get(f"/jobs/{j}/log").status_code == 404
        assert client.post(f"/jobs/{j}/cancel").status_code == 404
    assert client.get("/jobs").json() == []
    assert client.get("/jobs", params={"workspace": "claims"}).json() == []
    assert store.get("acme", queued)["state"] == "queued"


def test_job_routes_are_closed_without_a_session(db_url, monkeypatch):
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN")
    with TestClient(create_platform_app()) as c:
        assert c.get("/jobs").status_code == 401
        assert c.post("/jobs", json={"kind": "run", "workspace": "retail"}).status_code == 401
        assert c.get("/jobs/x").status_code == 401
        assert c.post("/workspaces/retail/runs", json={}).status_code == 401


# --- engine and CLI


def test_postgres_urls_use_the_driver_we_ship():
    assert store_mod.driver_url("postgresql://u@h/d") == "postgresql+psycopg://u@h/d"
    assert store_mod.driver_url("sqlite:///x.db") == "sqlite:///x.db"


def test_default_database_is_a_private_file_under_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path / "root"))
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    try:
        JobStore().enqueue("a", "retail", "run", "user:a", {})
        path = tmp_path / "root" / "meta.sqlite"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        store_mod.dispose_engines()


def test_worker_once_with_nothing_queued_returns(db_url, capsys):
    cli.main(["worker", "--once"])
