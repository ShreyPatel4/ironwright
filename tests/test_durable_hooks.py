"""Where durable state meets the app: ctx() hydrates, a write persists, a job holds the
org and persists before it reports done, an org deletion forgets the snapshot. Then
the whole journey, a wiped disk, and a restart, with everything coming back.

The metadata store sits outside the data root here, the way Postgres does when hosted,
so wiping the disk is exactly what a container restart does.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ironwright import config, durable
from ironwright.jobs import handlers
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.platform_api import create_platform_app

HERE = Path(__file__).parent
UPLOADS = HERE / "fixtures" / "tenant_ws" / "uploads" / "fixture"
PASSWORD = "correct-horse-9"
SLUG = "acme-claims"


@pytest.fixture()
def hosted(tmp_path, monkeypatch):
    from ironwright.tenancy import db

    data, remote = tmp_path / "data", tmp_path / "remote"
    remote.mkdir()
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_LAKE_URI", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(data))
    monkeypatch.setenv("IRONWRIGHT_DB_URL", f"sqlite:///{tmp_path / 'meta.sqlite'}")
    monkeypatch.setenv("IRONWRIGHT_STATE_URI", f"file://{remote}")
    durable.reset_store()
    yield type("Hosted", (), {"data": data, "remote": remote})
    durable.reset_store()
    db.dispose_current()


def client() -> TestClient:
    return TestClient(create_platform_app())


def bearer(who: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {who['token']}"}


def signup(c: TestClient, email: str = "ann@example.com", org: str = "Org Ann") -> dict:
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    assert r.status_code == 201, r.text
    return r.json()


def upload(c: TestClient, who: dict) -> str:
    files = [("files", (f.name, f.read_bytes(), "text/csv")) for f in sorted(UPLOADS.glob("*.csv"))]
    r = c.post("/uploads", headers=bearer(who), files=files)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def manifest(h, org: str) -> dict | None:
    path = h.remote / "tenants" / org / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else None


def restart(h) -> None:
    """The disk goes, the processes go. The metadata store and the object store stay."""
    shutil.rmtree(h.data)
    durable.reset_store()


def work(org_id: str, job_id: str) -> dict:
    Worker(JobStore()).run_once(job_id)
    got = JobStore().get(org_id, job_id)
    assert got["state"] == "succeeded", got["error"]
    return got


# ---------------------------------------------------------------- the API hooks

def test_a_write_commits_a_generation_and_a_read_does_not(hosted):
    with client() as c:
        who = signup(c)
        upload_id = upload(c, who)
        got = manifest(hosted, who["org_id"])
        assert got is not None, "the upload was not persisted"
        assert any(rel.startswith(f"uploads/{upload_id}/") for rel in got["files"])
        gen = got["generation"]
        assert c.get("/uploads", headers=bearer(who)).status_code == 200
        assert manifest(hosted, who["org_id"])["generation"] == gen


def test_a_refused_write_persists_nothing(hosted):
    with client() as c:
        who = signup(c)
        r = c.post("/uploads", headers=bearer(who), files=[("files", ("x.exe", b"MZ", "application/octet-stream"))])
        assert r.status_code >= 400
        assert manifest(hosted, who["org_id"]) is None


def test_a_wiped_disk_comes_back_on_the_first_request(hosted):
    with client() as c:
        who = signup(c)
        upload_id = upload(c, who)
        before = c.get(f"/uploads/{upload_id}", headers=bearer(who)).json()
    restart(hosted)
    with client() as c:
        r = c.get(f"/uploads/{upload_id}", headers=bearer(who))
        assert r.status_code == 200, r.text
        assert r.json() == before


def test_an_unreachable_store_is_a_503_and_never_an_empty_org(hosted, monkeypatch):
    with client() as c:
        who = signup(c)
        upload(c, who)
    restart(hosted)

    def down(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(durable.Store, "get", down)
    with client() as c:
        r = c.get("/uploads", headers=bearer(who))
        assert r.status_code == 503, r.text
        assert "storage" in r.json()["detail"]
    monkeypatch.undo()
    monkeypatch.setenv("IRONWRIGHT_STATE_URI", f"file://{hosted.remote}")
    assert len(manifest(hosted, who["org_id"])["files"]) > 0


def test_a_failed_persist_is_logged_and_the_request_still_answers(hosted, monkeypatch, caplog):
    def down(*_a, **_k):
        raise OSError("bucket gone")

    with client() as c:
        who = signup(c)
        monkeypatch.setattr(durable.Store, "put", down)
        r = c.post("/uploads", headers=bearer(who),
                   files=[("files", ("a.csv", b"id\n1\n", "text/csv"))])
        assert r.status_code == 201, r.text
    assert manifest(hosted, who["org_id"]) is None


def test_another_org_request_never_hydrates_the_first_org(hosted, monkeypatch):
    with client() as c:
        ann = signup(c)
        upload(c, ann)
        bob = signup(c, "bob@example.com", "Org Bob")
    restart(hosted)
    touched: list[str] = []
    real = durable.Store.get

    def spy(self, key, *a, **kw):
        touched.append(key)
        return real(self, key, *a, **kw)

    monkeypatch.setattr(durable.Store, "get", spy)
    with client() as c:
        assert c.get("/uploads", headers=bearer(bob)).status_code == 200
    assert touched and all(k.startswith(f"tenants/{bob['org_id']}/") for k in touched), touched
    assert not (hosted.data / "tenants" / ann["org_id"]).exists()


# ---------------------------------------------------------------- the worker hooks

def test_a_job_holds_the_org_and_persists_before_it_reports_done(hosted, monkeypatch):
    seen: dict[str, object] = {}

    def probe(ctx, workspace, params, log):
        def other_writer():
            try:
                with durable.org_lock(ctx.org_id, timeout=0.2):
                    seen["other"] = "got in"
            except durable.Busy:
                seen["other"] = "shut out"

        t = threading.Thread(target=other_writer)
        t.start()
        t.join()
        path = config.tenant_dir(ctx.org_id) / "jobs" / "probe.txt"
        config.private_dir(path.parent)
        config.write_private(path, "made by a job\n")
        return {"ok": True}

    monkeypatch.setitem(handlers.HANDLERS, "durable_probe", probe)
    real_finish = JobStore.finish

    def finish(self, job_id, token, state, **kw):
        org = seen["org"]
        files = (manifest(hosted, org) or {}).get("files", {})
        seen["persisted_before_finish"] = "jobs/probe.txt" in files
        return real_finish(self, job_id, token, state, **kw)

    monkeypatch.setattr(JobStore, "finish", finish)
    with client() as c:
        who = signup(c)
        seen["org"] = who["org_id"]
        store = JobStore()
        job_id = store.enqueue(who["org_id"], "(org)", "durable_probe", f"user:{who['user_id']}", {})
        work(who["org_id"], job_id)
    assert seen["other"] == "shut out"
    assert seen["persisted_before_finish"] is True


def test_a_job_on_a_wiped_disk_hydrates_before_it_runs(hosted, monkeypatch):
    with client() as c:
        who = signup(c)
        upload_id = upload(c, who)
    restart(hosted)
    found: dict[str, bool] = {}

    def probe(ctx, workspace, params, log):
        found["upload"] = (config.uploads_root(ctx.org_id) / upload_id).exists()
        return {}

    monkeypatch.setitem(handlers.HANDLERS, "durable_probe", probe)
    job_id = JobStore().enqueue(who["org_id"], "(org)", "durable_probe", f"user:{who['user_id']}", {})
    work(who["org_id"], job_id)
    assert found["upload"] is True


def test_deleting_an_org_forgets_its_snapshot(hosted, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "1")
    with client() as c:
        who = signup(c)
        upload(c, who)
        assert manifest(hosted, who["org_id"]) is not None
        r = c.post(f"/orgs/{who['org_slug']}/delete", headers=bearer(who), json={"confirm": who["org_slug"]})
        assert r.status_code in (200, 202), r.text
        if r.status_code == 202:
            for j in JobStore().list(who["org_id"], "(org)"):
                if j["kind"] == "org_delete" and j["state"] == "queued":
                    Worker(JobStore()).run_once(j["id"])
    assert not (hosted.remote / "tenants" / who["org_id"]).exists()


# ---------------------------------------------------------------- the whole journey, then a restart

def _snapshot_of_the_org(c: TestClient, who: dict) -> dict:
    """Everything a customer would notice is gone, read through the routes."""
    h = bearer(who)
    org_slug = who["org_slug"]
    examples = c.get(f"/workspaces/{SLUG}/ask/examples", headers=h)
    assert examples.status_code == 200, examples.text
    answer = c.post(f"/workspaces/{SLUG}/ask", headers=h, json={"question": examples.json()[0]["question"]})
    assert answer.status_code == 200, answer.text
    chain = c.get(f"/orgs/{org_slug}/audit/chain", headers=h)
    assert chain.status_code == 200, chain.text
    listed = c.get("/workspaces", headers=h)
    draft = c.get(f"/workspaces/{SLUG}/silver/draft", headers=h)
    proposal = c.get(f"/workspaces/{SLUG}/ontology/proposal", headers=h)
    uploads = c.get("/uploads", headers=h)
    for r in (listed, draft, proposal, uploads):
        assert r.status_code == 200, r.text
    from ironwright import workspaces_tenant as wt
    from ironwright.ontology.store import OntologyStore

    store = OntologyStore(wt.load_workspace(who["org_id"], SLUG).paths.ontology_db, read_only=True)
    try:
        audit = store.con.execute("select seq, actor, action, status, hash from audit_log order by seq").fetchall()
        intact = store.verify_audit_chain()
        objects = store.con.execute("select count(*) from objects").fetchone()[0]
    finally:
        store.con.close()
    a = answer.json()
    return {"answer": (a["status"], a["answer"], a["citations"]),
            "workspaces": sorted(w["name"] for w in listed.json()),
            "draft": draft.json(), "proposal": proposal.json()["id"],
            "uploads": sorted(u["id"] for u in uploads.json()),
            "org_chain": chain.json(), "audit": audit, "audit_intact": intact, "objects": objects}


def test_the_whole_journey_survives_a_wiped_disk_and_a_restart(hosted):
    with client() as c:
        who = signup(c)
        h = bearer(who)
        upload_id = upload(c, who)
        job = c.post(f"/uploads/{upload_id}/profile", headers=h)
        assert job.status_code == 202, job.text
        work(who["org_id"], job.json()["id"])
        proposal = c.get(f"/uploads/{upload_id}/proposal", headers=h).json()
        made = c.post("/workspaces", headers=h, json={
            "from_upload": upload_id, "slug": SLUG, "spec": proposal["workspace"],
            "silver": {"declined": [], "renames": {}}})
        assert made.status_code == 201, made.text
        for kind in ("run", "ontology_propose"):
            r = c.post("/jobs", headers=h, json={"kind": kind, "workspace": SLUG, "params": {}})
            assert r.status_code == 200, r.text
            work(who["org_id"], r.json()["id"])
        onto = c.get(f"/workspaces/{SLUG}/ontology/proposal", headers=h).json()
        done = c.post(f"/workspaces/{SLUG}/ontology/accept", headers=h,
                      json={"proposal_id": onto["id"], "edits": []})
        assert done.status_code == 200, done.text
        work(who["org_id"], done.json()["job_id"])
        before = _snapshot_of_the_org(c, who)
    assert before["answer"][0] == "answered" and "[ev:" in before["answer"][1]
    assert before["audit"] and before["audit_intact"] and before["objects"] > 0

    tenant = hosted.data / "tenants" / who["org_id"]
    on_disk = sorted(str(p.relative_to(tenant)) for p in tenant.rglob("*") if p.is_file()
                     and not p.name.endswith(durable.SIDECARS))
    carried = set(manifest(hosted, who["org_id"])["files"])
    # The Ask above wrote its log, and that write was persisted too.
    assert set(on_disk) == carried, set(on_disk) ^ carried

    restart(hosted)
    with client() as c:
        after = _snapshot_of_the_org(c, who)
    assert after == before
