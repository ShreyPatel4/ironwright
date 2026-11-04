"""POST /uploads: the door tenant bytes come through.

Every refusal must leave nothing behind, every accepted file must be read
only with the sha256 of what the client sent, and the caps must trip while
the body streams, not after it has been spooled somewhere.
"""
from __future__ import annotations

import hashlib
import stat

import pytest
from fastapi.testclient import TestClient

from ironwright import platform_api
from ironwright.config import UnsafePath, data_root
from ironwright.ingest import profiler, uploads
from ironwright.jobs.handlers import HANDLERS, JobCancelled, JobCtx
from ironwright.jobs.worker import Worker
from ironwright.tenancy import DEV_ORG_ID

CSV = b"id,name\n1,ann\n2,bob\n"
ORG_A, ORG_B = "a" * 32, "b" * 32


@pytest.fixture()
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client(root):
    return TestClient(platform_api.create_platform_app())


def uploads_root(root):
    return root / "tenants" / DEV_ORG_ID / "uploads"


def post(client, *files):
    return client.post("/uploads", files=[("files", f) for f in files])


def nothing_stored(root) -> bool:
    d = uploads_root(root)
    return not d.exists() or not any(d.iterdir())


# ----------------------------------------------------------------- accepted


def test_upload_stores_under_the_org_prefix_and_reports_the_sha256(client, root):
    r = post(client, ("claims.csv", CSV, "text/csv"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["files"] == [{"name": "claims.csv", "bytes": len(CSV),
                              "sha256": hashlib.sha256(CSV).hexdigest()}]
    assert (uploads_root(root) / body["id"] / "claims.csv").read_bytes() == CSV


def test_stored_bytes_are_read_only_and_the_folder_is_private(client, root):
    upload_id = post(client, ("a.csv", CSV)).json()["id"]
    d = uploads_root(root) / upload_id
    assert stat.S_IMODE((d / "a.csv").stat().st_mode) == 0o400
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_several_files_go_in_one_upload(client):
    body = post(client, ("a.csv", CSV), ("b.tsv", b"x\ty\n1\t2\n"),
                ("c.txt", b"hello\nthere\n"), ("d.dat", b"AAA111\nBBB222\n")).json()
    assert [f["name"] for f in body["files"]] == ["a.csv", "b.tsv", "c.txt", "d.dat"]


def test_an_upper_case_mainframe_extension_is_allowed(client):
    assert post(client, ("ITEMMAST.DAT", b"AAA111\nBBB222\n")).status_code == 201


def test_utf16_text_is_text_even_though_it_is_full_of_nuls(client):
    raw = "id,name\n1,ann\n".encode("utf-16")
    assert b"\x00" in raw
    assert post(client, ("excel.txt", raw)).status_code == 201


def test_cp1252_text_is_text(client):
    assert post(client, ("names.csv", "id,name\n1,José\n".encode("cp1252"))).status_code == 201


def test_get_upload_returns_the_manifest(client):
    made = post(client, ("a.csv", CSV)).json()
    r = client.get(f"/uploads/{made['id']}")
    assert r.status_code == 200
    assert r.json() == made


def test_each_upload_gets_its_own_id(client):
    assert post(client, ("a.csv", CSV)).json()["id"] != post(client, ("a.csv", CSV)).json()["id"]


# ---------------------------------------------------------------- write once


def test_a_stored_file_cannot_be_opened_for_write_again(root):
    session = uploads.UploadSession(DEV_ORG_ID)
    session.begin_file("a.csv")
    with pytest.raises(uploads.UploadRefused):
        session.begin_file("a.csv")
    session.abort()


def test_two_files_with_one_name_are_refused(client, root):
    r = post(client, ("a.csv", CSV), ("a.csv", b"other,bytes\n1,2\n"))
    assert r.status_code == 400
    assert "more than once" in r.json()["detail"]
    assert nothing_stored(root)


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize("name", ["data.xlsx", "data.json", "workspace.yaml", "profile.json",
                                  "manifest.json", "run.sh", "noextension", "csv"])
def test_a_name_off_the_allowlist_is_refused(client, root, name):
    r = post(client, (name, CSV))
    assert r.status_code == 415, r.text
    assert nothing_stored(root)


@pytest.mark.parametrize("name", ["../evil.csv", "../../etc/x.csv", "a/b.csv", ".hidden.csv",
                                  "..csv", "a\x00.csv"])
def test_a_name_that_is_not_one_plain_component_is_refused(client, root, name):
    r = post(client, (name, CSV))
    assert r.status_code == 400, r.text
    assert nothing_stored(root)
    assert not (root / "tenants" / DEV_ORG_ID / "evil.csv").exists()


@pytest.mark.parametrize("raw", [
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    b"PK\x03\x04" + b"\x00" * 40,
    b"%PDF-1.7\n1 0 obj\n",
    b"\x1f\x8b\x08\x00" + bytes(range(1, 8)) * 8,
    b"id,name\n1,ann\n" * 100 + b"\x00\x01\x02\x03",   # text head, binary tail
    bytes(range(1, 9)) * 20,                            # control characters, no NUL
    b"\xff\xfe" + b"\x00\xd8" * 4,                      # a UTF-16 BOM over bytes that do not decode
])
def test_binary_bytes_behind_a_text_extension_are_refused(client, root, raw):
    r = post(client, ("looks_fine.csv", raw))
    assert r.status_code == 415, r.text
    assert nothing_stored(root)


def test_ebcdic_is_refused_rather_than_read_as_garbage(client, root):
    raw = "ITEM0001 WIDGET    000123\nITEM0002 GADGET    000456\n".encode("cp037")
    r = post(client, ("ITEMMAST.dat", raw))
    assert r.status_code == 415
    assert "EBCDIC" in r.json()["detail"]
    assert nothing_stored(root)


def test_an_empty_file_is_refused(client, root):
    assert post(client, ("a.csv", b"")).status_code == 400
    assert nothing_stored(root)


def test_a_request_with_no_file_is_refused(client, root):
    r = client.post("/uploads", data={"note": "hello"}, files={"x": (None, "just a field")})
    assert r.status_code == 400
    assert nothing_stored(root)


def test_a_request_that_is_not_multipart_is_refused(client, root):
    r = client.post("/uploads", json={"files": []})
    assert r.status_code == 415
    assert nothing_stored(root)


def test_a_body_cut_off_before_the_closing_boundary_is_refused(client, root):
    b = "XbX"
    body = (f'--{b}\r\nContent-Disposition: form-data; name="f"; filename="a.csv"\r\n\r\n'
            f"id,name\n1,ann\n").encode()
    r = client.post("/uploads", content=body,
                    headers={"content-type": f"multipart/form-data; boundary={b}"})
    assert r.status_code == 400, r.text
    assert nothing_stored(root)


# ---------------------------------------------------------------------- caps


def test_the_size_cap_is_read_from_the_s3_setting_and_refuses(client, root, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILE_BYTES", "10")
    r = post(client, ("a.csv", b"x" * 11))
    assert r.status_code == 413
    assert nothing_stored(root)


def test_a_file_exactly_at_the_size_cap_is_accepted(client, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILE_BYTES", str(len(CSV)))
    assert post(client, ("a.csv", CSV)).status_code == 201


def test_the_count_cap_is_read_from_the_s3_setting_and_refuses(client, root, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILES", "1")
    r = post(client, ("a.csv", CSV), ("b.csv", CSV))
    assert r.status_code == 413
    assert nothing_stored(root)


def test_the_size_cap_trips_mid_stream_not_after_the_body_is_read(root, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILE_BYTES", "10")
    session = uploads.UploadSession(DEV_ORG_ID)
    session.begin_file("a.csv")
    session.write(b"x" * 10)
    with pytest.raises(uploads.UploadRefused) as e:
        session.write(b"x")
    assert e.value.status == 413
    session.abort()
    assert nothing_stored(root)


def test_oversized_part_headers_are_refused(client, root):
    b = "XbX"
    body = (f'--{b}\r\nX-Junk: {"j" * 100_000}\r\n'
            f'Content-Disposition: form-data; name="f"; filename="a.csv"\r\n\r\n'
            f"id\n1\n\r\n--{b}--\r\n").encode()
    r = client.post("/uploads", content=body,
                    headers={"content-type": f"multipart/form-data; boundary={b}"})
    assert r.status_code == 400, r.text
    assert nothing_stored(root)


# ---------------------------------------------------------- the staging area


def test_a_half_written_upload_is_not_reachable_by_id(root):
    session = uploads.UploadSession(DEV_ORG_ID)
    session.begin_file("a.csv")
    session.write(CSV)
    session.end_file()
    with pytest.raises(FileNotFoundError):
        uploads.manifest(DEV_ORG_ID, session.upload_id)
    session.abort()


# ----------------------------------------------------------- tenancy and auth


def test_one_org_cannot_read_the_upload_of_another(root):
    session = uploads.UploadSession(ORG_A)
    session.begin_file("a.csv")
    session.write(CSV)
    session.end_file()
    made = session.commit()
    assert uploads.manifest(ORG_A, made["id"]) == made
    with pytest.raises(FileNotFoundError):
        uploads.manifest(ORG_B, made["id"])


@pytest.mark.parametrize("upload_id", ["../" + ORG_A, "..", "ABC", "a" * 31, "a" * 33,
                                       "a" * 32 + ".partial"])
def test_an_upload_id_that_is_not_uuid_hex_is_refused(root, upload_id):
    with pytest.raises(UnsafePath):
        uploads.upload_dir(ORG_A, upload_id)


@pytest.mark.parametrize("org", ["../other", "..", "a/b", ""])
def test_an_org_id_that_is_not_one_component_is_refused(root, org):
    with pytest.raises(UnsafePath):
        uploads.UploadSession(org)


def test_get_of_an_unknown_upload_is_404(client):
    assert client.get("/uploads/" + "a" * 32).status_code == 404


def test_get_of_a_malformed_upload_id_is_404(client):
    assert client.get("/uploads/not-an-id").status_code == 404


def test_the_routes_sit_behind_the_session_gate(client, root, monkeypatch):
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    assert post(client, ("a.csv", CSV)).status_code == 401
    assert nothing_stored(root)
    assert client.get("/uploads/" + "a" * 32).status_code == 401


def test_the_route_stops_reading_the_body_once_the_cap_trips(root, monkeypatch):
    """The proof UploadFile would fail: it reads every byte before a handler runs."""
    import asyncio

    import httpx

    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILE_BYTES", "1000")
    sent = {"chunks": 0}

    async def body():
        yield (b'--XbX\r\nContent-Disposition: form-data; name="f"; filename="a.csv"\r\n\r\n')
        for _ in range(1000):
            sent["chunks"] += 1
            yield b"x" * 1024

    async def go():
        app = platform_api.create_platform_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/uploads", content=body(),
                                headers={"content-type": "multipart/form-data; boundary=XbX"})

    assert asyncio.run(go()).status_code == 413
    assert sent["chunks"] < 10
    assert nothing_stored(root)


def test_uploads_do_not_touch_the_stored_bytes_of_an_earlier_upload(client, root):
    first = post(client, ("a.csv", CSV)).json()
    post(client, ("a.csv", b"other,bytes\n1,2\n"))
    assert (uploads_root(root) / first["id"] / "a.csv").read_bytes() == CSV


def test_dev_mode_stores_under_the_dev_org(client, root):
    made = post(client, ("a.csv", CSV)).json()
    assert (root / "tenants" / DEV_ORG_ID / "uploads" / made["id"] / "a.csv").exists()


# ------------------------------------------------- real sessions, two orgs


def bearer(session):
    return {"Authorization": f"Bearer {session['token']}"}


def signup(c, slug):
    r = c.post("/auth/signup", json={"email": f"owner@{slug}.example", "password": "correct-horse-9",
                                     "org_name": f"Org {slug}"})
    assert r.status_code < 300, r.text
    return r.json()


def join(c, owner, role):
    invite = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                    json={"email": f"{role}@a.example", "role": role})
    assert invite.status_code < 300, invite.text
    r = c.post("/auth/accept-invite", json={"token": invite.json()["token"], "password": "correct-horse-9"})
    assert r.status_code < 300, r.text
    return r.json()


def send(c, session, name="a.csv", body=CSV):
    return c.post("/uploads", headers=bearer(session), files=[("files", (name, body))])


@pytest.fixture()
def two_orgs(meta_db):
    with TestClient(platform_api.create_platform_app()) as c:
        yield c, signup(c, "alpha"), signup(c, "beta")


def tenant_uploads(session):
    return data_root() / "tenants" / session["org_id"] / "uploads"


def test_an_upload_lands_under_the_callers_own_org(two_orgs):
    c, a, b = two_orgs
    made = send(c, a)
    assert made.status_code == 201, made.text
    upload_id = made.json()["id"]
    assert (tenant_uploads(a) / upload_id / "a.csv").read_bytes() == CSV
    assert not tenant_uploads(b).exists()
    assert not (data_root() / "tenants" / DEV_ORG_ID).exists()


def test_the_owner_reads_back_what_it_uploaded(two_orgs):
    c, a, _ = two_orgs
    upload_id = send(c, a).json()["id"]
    profiler.profile(a["org_id"], upload_id, lambda *_: None)
    for suffix in ("", "/profile", "/proposal"):
        assert c.get(f"/uploads/{upload_id}{suffix}", headers=bearer(a)).status_code == 200, suffix


@pytest.mark.parametrize("suffix", ["", "/profile", "/proposal"])
def test_another_org_gets_the_same_answer_as_for_nothing(two_orgs, suffix):
    c, a, b = two_orgs
    upload_id = send(c, a).json()["id"]
    profiler.profile(a["org_id"], upload_id, lambda *_: None)
    theirs = c.get(f"/uploads/{upload_id}{suffix}", headers=bearer(b))
    nothing = c.get(f"/uploads/{'a' * 32}{suffix}", headers=bearer(b))
    assert theirs.status_code == 404
    assert theirs.json() == {"detail": platform_api.NOT_FOUND}
    assert theirs.content == nothing.content


def test_the_same_id_in_two_orgs_stays_two_uploads(two_orgs):
    """The id is only half the address. The org comes from the session."""
    c, a, b = two_orgs
    first, second = send(c, a).json()["id"], send(c, b, body=b"other,bytes\n1,2\n").json()["id"]
    assert first != second
    assert not (tenant_uploads(b) / first).exists()
    assert not (tenant_uploads(a) / second).exists()


def test_a_viewer_cannot_upload(two_orgs):
    c, a, _ = two_orgs
    viewer = join(c, a, "viewer")
    assert send(c, viewer).status_code == 403
    assert not tenant_uploads(a).exists()


def test_a_viewer_can_read_what_the_org_uploaded(two_orgs):
    c, a, _ = two_orgs
    upload_id = send(c, a).json()["id"]
    assert c.get(f"/uploads/{upload_id}", headers=bearer(join(c, a, "viewer"))).status_code == 200


def test_a_member_can_upload(two_orgs):
    c, a, _ = two_orgs
    made = send(c, join(c, a, "member"))
    assert made.status_code == 201, made.text
    assert (tenant_uploads(a) / made.json()["id"] / "a.csv").exists()


def test_a_session_for_one_org_cannot_be_steered_to_another(two_orgs):
    """Nothing in the request names an org, so X-Principal and friends change nothing."""
    c, a, b = two_orgs
    made = c.post("/uploads", files=[("files", ("a.csv", CSV))],
                  headers={**bearer(a), "X-Principal": f"user:{b['user_id']}", "X-Org": b["org_id"]})
    assert (tenant_uploads(a) / made.json()["id"]).exists()
    assert not tenant_uploads(b).exists()


# ------------------------------------------------- the profile job


def workspace_for(session, name="claims"):
    (data_root() / "tenants" / session["org_id"] / "workspaces" / name).mkdir(parents=True, exist_ok=True)
    return name


def queue_profile(c, session, upload_id):
    r = c.post("/jobs", headers=bearer(session),
               json={"kind": "profile", "workspace": workspace_for(session), "params": {"upload_id": upload_id}})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def run_job(c, session, job_id):
    Worker().run_once(job_id)
    return c.get(f"/jobs/{job_id}", headers=bearer(session)).json()


def test_the_profile_job_writes_what_the_routes_serve(two_orgs):
    c, a, _ = two_orgs
    upload_id = send(c, a).json()["id"]
    assert c.get(f"/uploads/{upload_id}/profile", headers=bearer(a)).status_code == 404
    job = run_job(c, a, queue_profile(c, a, upload_id))
    assert job["state"] == "succeeded", job
    assert job["result"] == {"files": 1, "sources": 1}
    assert c.get(f"/uploads/{upload_id}/profile", headers=bearer(a)).json()["upload_id"] == upload_id
    assert c.get(f"/uploads/{upload_id}/proposal", headers=bearer(a)).json()["workspace"]["sources"]
    log = c.get(f"/jobs/{job['id']}/log", headers=bearer(a)).json()
    assert "profiling 1 files" in str(log)


def test_a_profile_job_cannot_reach_another_orgs_upload(two_orgs):
    """The id is in the body, so the gate never sees it. The org comes from the job."""
    c, a, b = two_orgs
    upload_id = send(c, a).json()["id"]
    theirs = run_job(c, b, queue_profile(c, b, upload_id))
    nothing = run_job(c, b, queue_profile(c, b, "a" * 32))
    assert theirs["state"] == nothing["state"] == "failed"
    assert theirs["error"] == nothing["error"]
    assert not (tenant_uploads(a) / upload_id / "profile.json").exists()
    assert c.get(f"/uploads/{upload_id}/profile", headers=bearer(a)).status_code == 404


@pytest.mark.parametrize("params", [{}, {"upload_id": 7}, {"upload_id": "../../etc"}])
def test_a_profile_job_with_no_good_upload_id_fails_out_loud(two_orgs, params):
    c, a, _ = two_orgs
    r = c.post("/jobs", headers=bearer(a),
               json={"kind": "profile", "workspace": workspace_for(a), "params": params})
    job = run_job(c, a, r.json()["id"])
    assert job["state"] == "failed"
    assert "upload" in job["error"]


def test_a_cancelled_profile_job_writes_nothing(two_orgs):
    c, a, _ = two_orgs
    upload_id = send(c, a).json()["id"]
    ctx = JobCtx(org_id=a["org_id"], principal="user:x", job_id="j", cancelled=lambda: True)
    with pytest.raises(JobCancelled):
        HANDLERS["profile"](ctx, "claims", {"upload_id": upload_id}, lambda _: None)
    assert not (tenant_uploads(a) / upload_id / "profile.json").exists()


@pytest.mark.xfail(strict=True, reason="POST /jobs needs a workspace the org can see, and profiling comes "
                                       "before any workspace exists. jobs/routes.py, ask on PR 8.")
def test_a_new_org_can_profile_before_it_has_a_workspace(two_orgs):
    c, a, _ = two_orgs
    upload_id = send(c, a).json()["id"]
    r = c.post("/jobs", headers=bearer(a),
               json={"kind": "profile", "workspace": "claims", "params": {"upload_id": upload_id}})
    assert r.status_code == 200, r.text


# ------------------------------------------------- behind the serving posture


def big_csv(mib: int = 3) -> bytes:
    row = b"1,ann\n"
    return b"id,name\n" + row * (mib * 1024 ** 2 // len(row))


def test_a_small_upload_passes_the_serving_posture(root):
    from ironwright.api import harden

    served = TestClient(harden(platform_api.create_platform_app()))
    assert post(served, ("a.csv", CSV)).status_code == 201


def test_a_body_refused_by_the_serving_posture_leaves_nothing(root):
    from ironwright.api import harden

    served = TestClient(harden(platform_api.create_platform_app()))
    assert post(served, ("a.csv", big_csv())).status_code == 413
    assert nothing_stored(root)


@pytest.mark.xfail(strict=True, reason="api.BodyCap holds every body to 2 MiB, under the upload caps. "
                                       "The fix is in api.py, outside T3. Cross-ticket ask on PR 8.")
def test_the_served_app_takes_a_file_over_the_global_body_cap(root):
    from ironwright.api import harden

    served = TestClient(harden(platform_api.create_platform_app()))
    assert post(served, ("a.csv", big_csv())).status_code == 201
