"""The seams a live walk of the journey found, one fast test each, through real routes.

tests/e2e/test_wizard_real_backend.py walks the same journey in a browser.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ironwright import cli, platform_api
from ironwright.ingest import drift, profiler, propose
from ironwright.jobs.worker import Worker
from ironwright.legacy import _read_upload_csv
from ironwright.tenancy import db

ROOT = Path(__file__).resolve().parents[1]
TWO_CSVS = sorted((ROOT / "tests" / "fixtures" / "tenant_ws" / "uploads" / "fixture").glob("*.csv"))
PASSWORD = "correct horse battery"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "0")
    with TestClient(platform_api.create_platform_app()) as c:
        yield c
    db.dispose_current()


def sign_up(client, name: str) -> dict[str, str]:
    r = client.post("/auth/signup", json={"email": f"{name}@acme.test", "password": PASSWORD, "org_name": f"{name} org"})
    assert r.status_code < 300, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def put_files(client, who, files=TWO_CSVS) -> str:
    r = client.post("/uploads", headers=who, files=[("files", (f.name, f.read_bytes())) for f in files])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def finish_jobs() -> None:
    worker = Worker(idle=0.01)
    while worker.run_once() is not None:
        pass


def job_named(client, who, job_id):
    r = client.get(f"/jobs/{job_id}", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def profiled_proposal(client, who, upload_id) -> dict:
    started = client.post(f"/uploads/{upload_id}/profile", headers=who)
    assert started.status_code == 202, started.text
    finish_jobs()
    assert job_named(client, who, started.json()["id"])["state"] == "succeeded"
    return client.get(f"/uploads/{upload_id}/proposal", headers=who).json()


# ------------------------------------------------------------------ GET /uploads

def test_uploads_list_newest_first_with_the_status_of_each(client, tmp_path):
    who = sign_up(client, "owner")
    assert client.get("/uploads", headers=who).json() == []
    older, newer = put_files(client, who), put_files(client, who)
    # Two uploads inside one clock tick would tie, and nobody uploads that fast.
    manifest = next(tmp_path.glob(f"tenants/*/uploads/{older}/manifest.json"))
    os.utime(manifest, (time.time() - 60, time.time() - 60))

    listed = client.get("/uploads", headers=who).json()
    assert [u["id"] for u in listed] == [newer, older]
    assert {f["name"] for f in listed[0]["files"]} == {f.name for f in TWO_CSVS}
    assert listed[0]["profile_job"] is None
    assert (listed[0]["profiled"], listed[0]["proposed"]) == (False, False)

    profiled_proposal(client, who, newer)
    newest, oldest = client.get("/uploads", headers=who).json()
    assert newest["profile_job"]["state"] == "succeeded"
    assert (newest["profiled"], newest["proposed"]) == (True, True)
    assert oldest["profile_job"] is None


def test_uploads_list_stays_in_your_org(client):
    a, b = sign_up(client, "aaa"), sign_up(client, "bbb")
    mine = put_files(client, a)
    assert client.get("/uploads", headers=b).json() == []
    assert mine in client.get("/uploads", headers=a).text


def test_uploads_list_needs_a_session(client):
    assert client.get("/uploads").status_code == 401


# ------------------------------------------------------------------ the trailing delimiter

HEADERS_THAT_END_IN_A_DELIMITER = b"Claim ID,Status,\nC-1,open,\nC-2,closed,\n"


def read_like_bronze(raw: bytes, columns=()) -> list[dict]:
    src = {"name": "x", "layout": {"delimiter": ",", "header": True}, "columns": list(columns)}
    return _read_upload_csv(src, raw.decode())


def test_the_profiler_leaves_out_an_empty_unnamed_trailing_column_and_says_why():
    prof = profiler.profile_files([("claims.csv", HEADERS_THAT_END_IN_A_DELIMITER)])
    assert [c["name"] for c in prof["files"][0]["columns"]] == ["claim_id", "status"]
    made = propose.propose(prof, "a" * 32)
    assert [c["name"] for c in made["workspace"]["sources"][0]["columns"]] == ["claim_id", "status"]
    said = [d for d in made["decisions"] if d["value"] == "left out"]
    assert len(said) == 1 and "trailing" in said[0]["reason"] and said[0]["reason"]


def test_the_reader_leaves_out_the_same_column():
    assert list(read_like_bronze(HEADERS_THAT_END_IN_A_DELIMITER)[0]) == ["claim_id", "status"]


def test_the_reader_keeps_a_late_cell_past_the_empty_column():
    rows = read_like_bronze(b"a,b,\n1,2,\n3,4,,late note\n")
    assert rows[0] == {"a": "1", "b": "2"}
    assert rows[1] == {"a": "3", "b": "4", "_extra": "late note"}


@pytest.mark.parametrize("raw, why", [
    (b"a,b,\n1,2,\n3,4,x\n", "an unnamed column that holds a value is data"),
    (b"a,b,notes\n1,2,\n3,4,\n", "a named column stays even when it is empty"),
    (b"a,,c\n1,,3\n4,,6\n", "an unnamed column in the middle is not trailing"),
])
def test_only_an_unnamed_empty_trailing_column_goes(raw, why):
    prof = profiler.profile_files([("f.csv", raw)])
    assert len(prof["files"][0]["columns"]) == 3, why


@pytest.mark.parametrize("raw", [
    HEADERS_THAT_END_IN_A_DELIMITER,
    b"a,b,\n1,2,\n3,4,x\n",
    b"a,,c\n1,,3\n4,,6\n",
    b"a,b,,\n1,2,,\n3,4,,\n",
    b"a,b,\n1,2,\n3,4,,late note\n",
    b"Order Date,2nd Amount\n2026-01-01,4\n2026-01-02,5\n",
    b"orderId,date,status\n1,2026-01-01,open\n2,2026-01-02,done\n",
])
def test_the_profiler_and_the_reader_name_every_column_the_same(raw):
    profiled = [c["name"] for c in profiler.profile_files([("f.csv", raw)])["files"][0]["columns"]]
    read = [k for k in read_like_bronze(raw)[0] if k != "_extra"]
    assert profiled == read


def test_the_first_run_of_the_two_csvs_is_not_stopped_as_drift(client):
    """The repro from the live walk, through the real routes, with the draft as proposed."""
    who = sign_up(client, "owner")
    upload_id = put_files(client, who)
    proposal = profiled_proposal(client, who, upload_id)
    made = client.post("/workspaces", headers=who, json={"from_upload": upload_id, "slug": "acme-claims",
                                                         "spec": proposal["workspace"]})
    assert made.status_code == 201, made.text
    job = client.post("/jobs", headers=who, json={"kind": "run", "workspace": "acme-claims", "params": {}}).json()
    finish_jobs()
    done = job_named(client, who, job["id"])
    assert done["state"] == "succeeded", done["error"]


def test_reserved_word_headers_as_proposed_reach_a_cited_answer(client):
    """The rename the profiler used to make broke this at Run, so I hold the whole road."""
    who = sign_up(client, "owner")
    raw = b"id,date,month,select,orderId\n1,2026-01-01,1,a,x\n2,2026-01-02,2,b,y\n3,2026-01-03,3,c,z\n"
    made = client.post("/uploads", headers=who, files=[("files", ("t.csv", raw))])
    upload_id = made.json()["id"]
    spec = profiled_proposal(client, who, upload_id)["workspace"]
    assert client.post("/workspaces", headers=who, json={"from_upload": upload_id, "slug": "words", "spec": spec}).status_code == 201
    run = client.post("/jobs", headers=who, json={"kind": "run", "workspace": "words", "params": {}}).json()
    finish_jobs()
    assert job_named(client, who, run["id"])["state"] == "succeeded"
    client.post("/jobs", headers=who, json={"kind": "ontology_propose", "workspace": "words", "params": {}})
    finish_jobs()
    proposal = client.get("/workspaces/words/ontology/proposal", headers=who).json()
    assert proposal["objects"], proposal
    done = client.post("/workspaces/words/ontology/accept", headers=who, json={"proposal_id": proposal["id"], "edits": []})
    assert done.status_code == 200, done.text
    finish_jobs()
    question = client.get("/workspaces/words/ask/examples", headers=who).json()[0]["question"]
    answer = client.post("/workspaces/words/ask", headers=who, json={"question": question}).json()
    assert answer["status"] == "answered" and answer["citations"], answer


def test_no_baseline_means_nothing_to_drift_from():
    src = {"name": "x", "format": "csv", "layout": {"delimiter": ",", "header": True},
           "columns": [{"name": "a", "type": "string", "key": True}, {"name": "gone", "type": "string"}]}
    new = drift.Observation(rows=1, rows_read=1, cells={"a": ["1"]}, present=["a"])
    report = drift.diff(src, new, None, version=1, against=None, loaded_before=False)
    assert report["items"] == [] and report["blocking"] is False


def test_a_first_run_whose_spec_does_not_fit_fails_in_the_loaders_words(client):
    """Not drift, and still not a pass: the spec names a column the file does not have."""
    who = sign_up(client, "owner")
    upload_id = put_files(client, who)
    spec = profiled_proposal(client, who, upload_id)["workspace"]
    spec["sources"][0]["columns"].append({"name": "never_there", "type": "string"})
    made = client.post("/workspaces", headers=who, json={"from_upload": upload_id, "slug": "acme-claims", "spec": spec})
    assert made.status_code == 201, made.text
    job = client.post("/jobs", headers=who, json={"kind": "run", "workspace": "acme-claims", "params": {}}).json()
    finish_jobs()
    done = job_named(client, who, job["id"])
    assert done["state"] == "failed"
    assert "the file has no column never_there" in done["error"]
    assert "bronze has no earlier run" not in done["error"]


# ------------------------------------------------------------------ the ontology read

def test_a_tenant_workspace_can_read_its_own_ontology(client):
    """The wizard learns an ontology was accepted from this route."""
    who = sign_up(client, "owner")
    upload_id = put_files(client, who)
    spec = profiled_proposal(client, who, upload_id)["workspace"]
    assert client.post("/workspaces", headers=who, json={"from_upload": upload_id, "slug": "acme-claims", "spec": spec}).status_code == 201
    client.post("/jobs", headers=who, json={"kind": "run", "workspace": "acme-claims", "params": {}})
    finish_jobs()
    read = client.get("/workspaces/acme-claims/ontology", headers=who)
    assert read.status_code == 200, read.text
    assert read.json()["describe"]["object_types"] == {}


def test_a_second_org_cannot_read_that_ontology(client):
    a, b = sign_up(client, "aaa"), sign_up(client, "bbb")
    upload_id = put_files(client, a)
    spec = profiled_proposal(client, a, upload_id)["workspace"]
    assert client.post("/workspaces", headers=a, json={"from_upload": upload_id, "slug": "acme-claims", "spec": spec}).status_code == 201
    assert client.get("/workspaces/acme-claims/ontology", headers=b).status_code == 404


# ------------------------------------------------------------------ the console files

@pytest.fixture()
def built_console(monkeypatch, tmp_path):
    dist = tmp_path / "console-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")
    (dist / "assets" / "index-abc.js").write_text("1")
    monkeypatch.setattr(platform_api, "CONSOLE_DIST", dist)
    return dist


@pytest.mark.parametrize("path", ["/console/", "/console/index.html"])
def test_the_console_page_is_revalidated_every_time(client, built_console, path):
    with TestClient(platform_api.create_platform_app()) as c:
        r = c.get(path)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_hashed_assets_can_stay_cached(client, built_console):
    with TestClient(platform_api.create_platform_app()) as c:
        r = c.get("/console/assets/index-abc.js")
    assert r.status_code == 200 and "no-cache" not in r.headers.get("cache-control", "")


@pytest.fixture()
def console_tree(tmp_path):
    src = tmp_path / "console" / "src"
    src.mkdir(parents=True)
    (src / "main.jsx").write_text("1")
    return tmp_path / "console"


def build(console: Path, ago: float = 0) -> None:
    (console / "dist").mkdir(exist_ok=True)
    index = console / "dist" / "index.html"
    index.write_text("x")
    at = time.time() - ago
    os.utime(index, (at, at))


def test_a_missing_build_is_named(console_tree):
    assert "console/dist is missing" in cli.console_build_problem(console_tree)


def test_a_build_older_than_the_source_is_named(console_tree):
    build(console_tree, ago=3600)
    assert "older than" in cli.console_build_problem(console_tree)


def test_a_fresh_build_says_nothing(console_tree):
    build(console_tree, ago=-3600)
    assert cli.console_build_problem(console_tree) is None


def test_no_source_tree_says_nothing(console_tree):
    """The hosted image has dist and no source, and that is the right shape for it."""
    build(console_tree)
    shutil.rmtree(console_tree / "src")
    assert cli.console_build_problem(console_tree) is None


def test_platform_warns_and_still_serves(monkeypatch, capsys, console_tree):
    served = []
    monkeypatch.setattr(cli, "_serve", lambda app, args: served.append(app))
    monkeypatch.setattr(cli, "CONSOLE", console_tree)
    cli.main(["platform"])
    assert served
    err = capsys.readouterr().err
    assert "console/dist is missing" in err and "make console" in err


def test_make_console_really_builds():
    made = subprocess.run(["make", "-n", "console"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert "npm run build" in made.stdout, made.stdout + made.stderr
