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
from ironwright.config import UnsafePath
from ironwright.ingest import routes, uploads

CSV = b"id,name\n1,ann\n2,bob\n"


@pytest.fixture()
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client(root):
    return TestClient(platform_api.create_platform_app())


def uploads_root(root):
    return root / "tenants" / uploads.DEV_ORG / "uploads"


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
    session = uploads.UploadSession(uploads.DEV_ORG)
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
    assert not (root / "tenants" / uploads.DEV_ORG / "evil.csv").exists()


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
    session = uploads.UploadSession(uploads.DEV_ORG)
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
    session = uploads.UploadSession(uploads.DEV_ORG)
    session.begin_file("a.csv")
    session.write(CSV)
    session.end_file()
    with pytest.raises(FileNotFoundError):
        uploads.manifest(uploads.DEV_ORG, session.upload_id)
    session.abort()


# ----------------------------------------------------------- tenancy and auth


def test_one_org_cannot_read_the_upload_of_another(root):
    session = uploads.UploadSession("org-a")
    session.begin_file("a.csv")
    session.write(CSV)
    session.end_file()
    made = session.commit()
    assert uploads.manifest("org-a", made["id"]) == made
    with pytest.raises(FileNotFoundError):
        uploads.manifest("org-b", made["id"])


@pytest.mark.parametrize("upload_id", ["../org-a", "..", "ABC", "a" * 31, "a" * 33,
                                       "a" * 32 + ".partial"])
def test_an_upload_id_that_is_not_uuid_hex_is_refused(root, upload_id):
    with pytest.raises(UnsafePath):
        uploads.upload_dir("org-a", upload_id)


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


def test_the_org_comes_from_one_dependency_t1_can_swap(client, root):
    client.app.dependency_overrides[routes.current_org] = lambda: "org-z"
    made = post(client, ("a.csv", CSV)).json()
    assert (root / "tenants" / "org-z" / "uploads" / made["id"] / "a.csv").exists()
    client.app.dependency_overrides.clear()


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


def test_a_real_session_gets_no_org_until_t1_wires_one(monkeypatch):
    """Every session sharing dev-org would break tenant isolation, so outside dev
    mode this refuses. A merge that forgets the swap fails closed, not shared."""
    from fastapi import HTTPException

    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    with pytest.raises(HTTPException) as e:
        routes.current_org()
    assert e.value.status_code == 503


def test_dev_mode_maps_to_the_dev_org():
    assert routes.current_org() == uploads.DEV_ORG


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
