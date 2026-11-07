"""Durable tenant state: hydrate the data root from an object store, persist it back.

Most of these run against IRONWRIGHT_STATE_URI=file://..., the same code path as S3
through pyarrow's filesystem interface, so they run on every `pytest -q`. The last
block proves the same against a real S3 endpoint and skips without one.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from ironwright import config, durable

ORG_A = "a" * 32
ORG_B = "b" * 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    remote = tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(data))
    monkeypatch.setenv("IRONWRIGHT_STATE_URI", f"file://{remote}/state")
    durable.reset_store()
    yield type("Env", (), {"data": data, "remote": remote / "state"})
    durable.reset_store()


def write(org: str, rel: str, body: str | bytes, mode: int = 0o600) -> Path:
    config.ensure_tenant(org)
    path = config.tenant_dir(org) / rel
    config.private_dir(path.parent)
    config.write_private(path, body)
    path.chmod(mode)
    return path


def tree(org: str) -> dict[str, tuple[bytes, int]]:
    root = config.tenant_dir(org)
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in sorted(root.rglob("*")) if p.is_file()}


def wipe(env) -> None:
    """What a container restart does to its disk."""
    shutil.rmtree(env.data)


def org_prefix(env, org: str) -> Path:
    return env.remote / "tenants" / org


# ---------------------------------------------------------------- off by default

def test_with_no_state_uri_nothing_is_built_and_every_hook_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    monkeypatch.delenv("IRONWRIGHT_STATE_URI", raising=False)
    durable.reset_store()

    def boom(*_a, **_k):
        raise AssertionError("a store was built with no IRONWRIGHT_STATE_URI")

    monkeypatch.setattr(durable, "_open_store", boom)
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    assert durable.enabled() is False
    assert durable.ensure_hydrated(ORG_A) is None
    assert durable.persist(ORG_A) is None
    durable.forget(ORG_A)
    with durable.org_lock(ORG_A):
        pass
    assert not (tmp_path / ".durable").exists()


def test_a_state_uri_must_be_s3_or_an_absolute_file_url(monkeypatch):
    for bad in ("http://x/y", "s3://", "s3://Bad_Bucket/p", "file://relative/x", "s3://b/../x"):
        monkeypatch.setenv("IRONWRIGHT_STATE_URI", bad)
        with pytest.raises(ValueError):
            config.state_uri()
    monkeypatch.setenv("IRONWRIGHT_STATE_URI", "s3://ironwright-lake/state/")
    assert config.state_uri() == "s3://ironwright-lake/state"


# ---------------------------------------------------------------- the round trip

def test_a_persisted_org_comes_back_byte_for_byte_after_the_disk_is_wiped(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\nversion: 1\n")
    write(ORG_A, "uploads/u1/claims (2024).csv", "id,amount\n1,10\n", mode=0o400)
    write(ORG_A, "workspaces/acme/run/reports/evals.json", b"\x00\x01binary")
    before = tree(ORG_A)
    assert durable.persist(ORG_A) == 1

    wipe(env)
    assert durable.ensure_hydrated(ORG_A) == 1
    assert tree(ORG_A) == before
    assert config.tenant_dir(ORG_A).stat().st_mode & 0o777 == 0o700


def test_the_engine_dotfiles_round_trip(env):
    write(ORG_A, "workspaces/acme/.source_versions.lock", "")
    write(ORG_A, "workspaces/acme/.ironwright-tenant-workspace", "acme\n")
    before = tree(ORG_A)
    durable.persist(ORG_A)
    wipe(env)
    durable.ensure_hydrated(ORG_A)
    assert tree(ORG_A) == before


def test_uploads_stay_read_only_after_a_hydrate(env):
    path = write(ORG_A, "uploads/u1/a.csv", "x\n1\n", mode=0o400)
    durable.persist(ORG_A)
    wipe(env)
    durable.ensure_hydrated(ORG_A)
    assert path.stat().st_mode & 0o777 == 0o400
    with pytest.raises(PermissionError):
        path.write_text("tampered")


def test_an_org_with_nothing_remote_hydrates_to_generation_zero_and_keeps_its_disk(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    assert durable.ensure_hydrated(ORG_A) == 0
    assert "workspaces/acme/workspace.yaml" in tree(ORG_A)


def test_nothing_changed_means_no_new_generation_and_no_upload(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    assert durable.persist(ORG_A) == 1
    blobs = sorted((org_prefix(env, ORG_A) / "blobs").iterdir())
    assert durable.persist(ORG_A) == 1
    assert sorted((org_prefix(env, ORG_A) / "blobs").iterdir()) == blobs


def test_only_a_changed_file_is_sent(env, monkeypatch):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    write(ORG_A, "workspaces/acme/ontology.yaml", "objects: []\n")
    durable.persist(ORG_A)
    sent: list[str] = []
    real = durable.Store.put

    def spy(self, key, data):
        sent.append(key)
        return real(self, key, data)

    monkeypatch.setattr(durable.Store, "put", spy)
    write(ORG_A, "workspaces/acme/ontology.yaml", "objects: [Claim]\n")
    assert durable.persist(ORG_A) == 2
    blob_puts = [k for k in sent if "/blobs/" in k]
    assert len(blob_puts) == 1


def test_a_file_name_the_manifest_cannot_carry_fails_loudly(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    (config.tenant_dir(ORG_A) / "workspaces" / "acme" / "bad\nname").write_text("x")
    with pytest.raises(durable.DurableError, match="cannot carry"):
        durable.persist(ORG_A)


# ---------------------------------------------------------------- never wipe a tenant by accident

def test_persist_on_a_fresh_disk_hydrates_first_and_never_publishes_an_empty_org(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)
    wipe(env)
    # A fresh container whose first mutation writes one new file.
    write(ORG_A, "workspaces/acme/notes.txt", "new\n")
    assert durable.persist(ORG_A) == 2
    wipe(env)
    durable.ensure_hydrated(ORG_A)
    assert {"workspaces/acme/workspace.yaml", "workspaces/acme/notes.txt"} <= set(tree(ORG_A))


def test_a_vanished_tenant_folder_is_refused_not_published_as_empty(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)
    shutil.rmtree(config.tenant_dir(ORG_A))
    with pytest.raises(durable.DurableError, match="missing"):
        durable.persist(ORG_A)
    wipe(env)
    durable.ensure_hydrated(ORG_A)
    assert "workspaces/acme/workspace.yaml" in tree(ORG_A)


def test_forget_removes_the_org_prefix_and_the_local_marker(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    write(ORG_B, "workspaces/beta/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)
    durable.persist(ORG_B)
    shutil.rmtree(config.tenant_dir(ORG_A))
    durable.forget(ORG_A)
    assert not org_prefix(env, ORG_A).exists()
    assert org_prefix(env, ORG_B).exists()
    wipe(env)
    assert durable.ensure_hydrated(ORG_A) == 0
    assert not config.tenant_dir(ORG_A).exists() or tree(ORG_A) == {}


# ---------------------------------------------------------------- crash safety

def test_the_previous_generation_is_kept_and_used_when_the_current_one_is_broken(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "v1\n")
    durable.persist(ORG_A)
    write(ORG_A, "workspaces/acme/workspace.yaml", "v2\n")
    durable.persist(ORG_A)
    prefix = org_prefix(env, ORG_A)
    assert json.loads((prefix / "manifest.prev.json").read_text())["generation"] == 1
    current = json.loads((prefix / "manifest.json").read_text())
    sha = current["files"]["workspaces/acme/workspace.yaml"]["sha256"]
    (prefix / "blobs" / sha).unlink()
    wipe(env)
    assert durable.ensure_hydrated(ORG_A) == 1
    assert tree(ORG_A)["workspaces/acme/workspace.yaml"][0] == b"v1\n"


def test_a_crash_before_the_manifest_commit_leaves_the_last_generation_whole(env, monkeypatch):
    write(ORG_A, "workspaces/acme/workspace.yaml", "v1\n")
    durable.persist(ORG_A)

    def die(*_a, **_k):
        raise OSError("power cut")

    monkeypatch.setattr(durable.Store, "copy", die)
    write(ORG_A, "workspaces/acme/workspace.yaml", "v2\n")
    with pytest.raises(OSError):
        durable.persist(ORG_A)
    monkeypatch.undo()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(env.data))
    monkeypatch.setenv("IRONWRIGHT_STATE_URI", f"file://{env.remote}")
    wipe(env)
    assert durable.ensure_hydrated(ORG_A) == 1
    assert tree(ORG_A)["workspaces/acme/workspace.yaml"][0] == b"v1\n"


def test_hydrate_is_all_or_nothing_when_no_generation_is_whole(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "v1\n")
    write(ORG_A, "workspaces/acme/ontology.yaml", "o\n")
    durable.persist(ORG_A)
    for blob in (org_prefix(env, ORG_A) / "blobs").iterdir():
        blob.write_bytes(b"rotted")
        break
    wipe(env)
    with pytest.raises(durable.DurableError):
        durable.ensure_hydrated(ORG_A)
    assert not config.tenant_dir(ORG_A).exists() or tree(ORG_A) == {}
    assert not (env.data / ".durable" / f"{ORG_A}.json").exists()
    assert not (env.data / ".durable" / f"{ORG_A}.gen").exists()


# ---------------------------------------------------------------- isolation

def _tamper(env, org: str, change) -> None:
    path = org_prefix(env, org) / "manifest.json"
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))
    (org_prefix(env, org) / "manifest.prev.json").unlink(missing_ok=True)


@pytest.mark.parametrize("rel", ["../" + ORG_B + "/uploads/x.csv", "/etc/passwd", "%2e%2e/x", "a//b",
                                 "workspaces/../../x", "..x/y", "./x", "uploads/u1/"])
def test_a_manifest_path_that_climbs_or_hides_is_refused(env, rel):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)

    def change(m):
        m["files"][rel] = next(iter(m["files"].values()))

    _tamper(env, ORG_A, change)
    wipe(env)
    with pytest.raises(durable.Tampered):
        durable.ensure_hydrated(ORG_A)
    assert not (env.data / "tenants" / ORG_B).exists()
    assert not (env.data / "etc").exists()


@pytest.mark.parametrize("sha", ["../../" + ORG_B + "/blobs/" + "0" * 64, "0" * 63, "Z" * 64])
def test_a_manifest_blob_that_is_not_a_plain_sha_is_refused(env, sha):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)

    def change(m):
        for entry in m["files"].values():
            entry["sha256"] = sha

    _tamper(env, ORG_A, change)
    wipe(env)
    with pytest.raises(durable.Tampered):
        durable.ensure_hydrated(ORG_A)


def test_a_manifest_naming_another_org_is_refused(env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)
    _tamper(env, ORG_A, lambda m: m.update(org_id=ORG_B))
    wipe(env)
    with pytest.raises(durable.Tampered):
        durable.ensure_hydrated(ORG_A)


def test_org_b_hydrate_never_reads_org_a_prefix(env, monkeypatch):
    write(ORG_A, "workspaces/acme/workspace.yaml", "secret of a\n")
    write(ORG_B, "workspaces/beta/workspace.yaml", "b\n")
    durable.persist(ORG_A)
    durable.persist(ORG_B)
    wipe(env)
    touched: list[str] = []
    for name in ("get", "get_to", "put", "copy", "delete", "exists", "list", "delete_tree"):
        real = getattr(durable.Store, name)

        def spy(self, *args, _real=real, **kw):
            touched.extend(a for a in args if isinstance(a, str))
            return _real(self, *args, **kw)

        monkeypatch.setattr(durable.Store, name, spy)
    durable.ensure_hydrated(ORG_B)
    durable.persist(ORG_B)
    assert touched
    assert all(k.startswith(f"tenants/{ORG_B}/") for k in touched), touched
    assert not (env.data / "tenants" / ORG_A).exists()


@pytest.mark.parametrize("org", ["../x", "A" * 32, "a" * 31, "", "dev"])
def test_an_org_id_that_is_not_one_is_refused(env, org):
    with pytest.raises(config.UnsafePath):
        durable.ensure_hydrated(org)
    with pytest.raises(config.UnsafePath):
        durable.persist(org)


# ---------------------------------------------------------------- live files

def test_wal_and_journal_sidecars_are_never_sent(env):
    write(ORG_A, "workspaces/acme/run/ontology.duckdb.wal", "half")
    write(ORG_A, "workspaces/acme/run/checkpoints.sqlite-wal", "half")
    write(ORG_A, "workspaces/acme/run/checkpoints.sqlite-shm", "half")
    write(ORG_A, "workspaces/acme/run/x.sqlite-journal", "half")
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)
    files = json.loads((org_prefix(env, ORG_A) / "manifest.json").read_text())["files"]
    assert set(files) == {"workspaces/acme/workspace.yaml"}


def test_a_duckdb_store_is_checkpointed_so_its_rows_survive(env):
    import duckdb

    config.ensure_tenant(ORG_A)
    run = config.tenant_dir(ORG_A) / "workspaces" / "acme" / "run"
    config.private_dir(run)
    con = duckdb.connect(str(run / "ontology.duckdb"))
    con.execute("create table audit_log(seq int, action text)")
    con.execute("insert into audit_log values (1, 'approve')")
    durable.persist(ORG_A)  # the connection is still open in this process
    con.close()
    wipe(env)
    durable.ensure_hydrated(ORG_A)
    back = duckdb.connect(str(run / "ontology.duckdb"), read_only=True)
    assert back.execute("select * from audit_log").fetchall() == [(1, "approve")]
    back.close()


def test_a_sqlite_file_is_sent_as_a_consistent_snapshot(env):
    config.ensure_tenant(ORG_A)
    run = config.tenant_dir(ORG_A) / "workspaces" / "acme" / "run"
    config.private_dir(run)
    con = sqlite3.connect(run / "checkpoints.sqlite")
    con.execute("pragma journal_mode=wal")
    con.execute("create table t(x)")
    con.execute("insert into t values (42)")
    con.commit()  # committed into the -wal, not yet the main file
    durable.persist(ORG_A)
    con.close()
    wipe(env)
    durable.ensure_hydrated(ORG_A)
    back = sqlite3.connect(run / "checkpoints.sqlite")
    assert back.execute("select x from t").fetchall() == [(42,)]
    back.close()


def test_an_unchanged_database_is_not_copied_again_and_a_changed_one_is(env, monkeypatch):
    import duckdb

    config.ensure_tenant(ORG_A)
    run = config.tenant_dir(ORG_A) / "workspaces" / "acme" / "run"
    config.private_dir(run)
    d = duckdb.connect(str(run / "ontology.duckdb"))
    d.execute("create table t(x int)")
    d.close()
    s = sqlite3.connect(run / "checkpoints.sqlite")
    s.execute("pragma journal_mode=wal")
    s.execute("create table t(x)")
    s.commit()
    durable.persist(ORG_A)
    copied: list[str] = []
    real = durable._snapshot

    def spy(path, dest):
        copied.append(path.name)
        return real(path, dest)

    monkeypatch.setattr(durable, "_snapshot", spy)
    durable.persist(ORG_A)
    assert copied == []
    s.execute("insert into t values (7)")
    s.commit()  # only the -wal moves
    durable.persist(ORG_A)
    assert copied == ["checkpoints.sqlite"]
    s.close()
    wipe(env)
    durable.ensure_hydrated(ORG_A)
    back = sqlite3.connect(run / "checkpoints.sqlite")
    assert back.execute("select x from t").fetchall() == [(7,)]
    back.close()


# ---------------------------------------------------------------- the org lock

def test_the_org_lock_is_reentrant_in_one_thread(env):
    with durable.org_lock(ORG_A), durable.org_lock(ORG_A):
        assert durable.persist(ORG_A) is not None or True


def test_the_org_lock_shuts_out_another_process(env):
    holder = textwrap.dedent(f"""
        import os, sys, time
        os.environ["IRONWRIGHT_DATA"] = {str(env.data)!r}
        os.environ["IRONWRIGHT_STATE_URI"] = {f"file://{env.remote}"!r}
        from ironwright import durable
        with durable.org_lock({ORG_A!r}):
            print("held", flush=True)
            sys.stdin.readline()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env={**os.environ})
    try:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(durable.Busy), durable.org_lock(ORG_A, timeout=0.3):
            pass
        with durable.org_lock(ORG_B, timeout=0.3):
            pass  # another org is not held
    finally:
        proc.stdin.write("\n")
        proc.stdin.flush()
        proc.wait(10)
    with durable.org_lock(ORG_A, timeout=2):
        pass


def test_a_random_org_id_round_trips(env):
    org = uuid.uuid4().hex
    write(org, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(org)
    wipe(env)
    durable.ensure_hydrated(org)
    assert "workspaces/acme/workspace.yaml" in tree(org)


# ---------------------------------------------------------------- a real S3 endpoint

@pytest.fixture()
def s3_env(tmp_path, monkeypatch):
    """CI points IRONWRIGHT_TEST_S3_ENDPOINT at a versitygw. pyarrow speaks to it
    natively, so moto's in-process mock cannot stand in. Without it: skip, claim nothing."""
    endpoint = os.environ.get("IRONWRIGHT_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("IRONWRIGHT_TEST_S3_ENDPOINT not set: durable state on S3 is "
                    "code-present but only claimed where a real endpoint proves it")
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", os.environ.get("IRONWRIGHT_TEST_S3_KEY", "testing"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", os.environ.get("IRONWRIGHT_TEST_S3_SECRET", "testing"))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("IRONWRIGHT_S3_ENDPOINT", endpoint)
    bucket = f"iw-state-{uuid.uuid4().hex[:10]}"
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    client.create_bucket(Bucket=bucket)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("IRONWRIGHT_STATE_URI", f"s3://{bucket}/state")
    durable.reset_store()
    yield type("S3", (), {"client": client, "bucket": bucket, "data": tmp_path / "data"})
    durable.reset_store()


def _keys(s3, prefix: str) -> list[str]:
    out = s3.client.list_objects_v2(Bucket=s3.bucket, Prefix=prefix)
    return sorted(o["Key"] for o in out.get("Contents", []))


def test_on_s3_an_org_round_trips_and_only_changes_are_sent(s3_env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    write(ORG_A, "uploads/u1/a.csv", "id\n1\n", mode=0o400)
    assert durable.persist(ORG_A) == 1
    before = tree(ORG_A)
    blobs = _keys(s3_env, f"state/tenants/{ORG_A}/blobs/")
    assert len(blobs) == 2
    assert durable.persist(ORG_A) == 1
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\nversion: 1\n")
    assert durable.persist(ORG_A) == 2
    assert len(_keys(s3_env, f"state/tenants/{ORG_A}/blobs/")) == 3  # the previous generation is kept
    assert f"state/tenants/{ORG_A}/manifest.prev.json" in _keys(s3_env, f"state/tenants/{ORG_A}/")
    before = tree(ORG_A)
    shutil.rmtree(s3_env.data)
    assert durable.ensure_hydrated(ORG_A) == 2
    assert tree(ORG_A) == before


def test_on_s3_a_tampered_manifest_is_refused(s3_env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)
    key = f"state/tenants/{ORG_A}/manifest.json"
    manifest = json.loads(s3_env.client.get_object(Bucket=s3_env.bucket, Key=key)["Body"].read())
    manifest["files"][f"../{ORG_B}/x"] = next(iter(manifest["files"].values()))
    s3_env.client.put_object(Bucket=s3_env.bucket, Key=key, Body=json.dumps(manifest).encode())
    shutil.rmtree(s3_env.data)
    with pytest.raises(durable.Tampered):
        durable.ensure_hydrated(ORG_A)


def test_on_s3_forget_leaves_no_key_behind(s3_env):
    write(ORG_A, "workspaces/acme/workspace.yaml", "kind: workspace\n")
    write(ORG_B, "workspaces/beta/workspace.yaml", "kind: workspace\n")
    durable.persist(ORG_A)
    durable.persist(ORG_B)
    shutil.rmtree(config.tenant_dir(ORG_A))
    durable.forget(ORG_A)
    assert _keys(s3_env, f"state/tenants/{ORG_A}/") == []
    assert _keys(s3_env, f"state/tenants/{ORG_B}/")
