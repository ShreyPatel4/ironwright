"""The hosted stack: settings from env, the lake on object storage, health routes
outside the session gate, and the posture of the image and compose file.

The object store tests need a real S3 endpoint, because PyIceberg writes through
pyarrow's native client and moto's in-process mock only patches botocore. CI points
IRONWRIGHT_TEST_S3_ENDPOINT at the compose object store. Without it they skip and claim nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from ironwright import config, hosted
from ironwright.cli import STAGES
from ironwright.lake import Lakehouse, lake_location
from ironwright.packs import list_packs, load_pack
from ironwright.tracing import Tracer

REPO = pathlib.Path(__file__).resolve().parents[1]
HOSTED_ENV = ("IRONWRIGHT_DB_URL", "IRONWRIGHT_LAKE_URI", "IRONWRIGHT_S3_ENDPOINT", "IRONWRIGHT_HOST",
              "IRONWRIGHT_PORT", "PORT", "IRONWRIGHT_ENV", "AWS_REGION")


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    for name in HOSTED_ENV:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- settings
def test_db_url_defaults_to_sqlite_under_the_data_root(tmp_path):
    assert config.db_url() == f"sqlite:///{tmp_path.resolve()}/meta.sqlite"


@pytest.mark.parametrize("given", ["postgres://u:p@db:5432/iw", "postgresql://u:p@db:5432/iw"])
def test_db_url_names_the_driver_we_ship(monkeypatch, given):
    # Railway and most hosts hand out the bare form, which SQLAlchemy maps to psycopg2.
    monkeypatch.setenv("IRONWRIGHT_DB_URL", given)
    assert config.db_url() == "postgresql+psycopg://u:p@db:5432/iw"


def test_bind_host_is_loopback_unless_the_env_says_otherwise(monkeypatch):
    assert config.bind_host() == "127.0.0.1"
    monkeypatch.setenv("IRONWRIGHT_HOST", "0.0.0.0")
    assert config.bind_host() == "0.0.0.0"


def test_bind_port_takes_the_platform_port_then_ours(monkeypatch):
    assert config.bind_port() == 8000
    monkeypatch.setenv("PORT", "9001")
    assert config.bind_port() == 9001
    monkeypatch.setenv("IRONWRIGHT_PORT", "9002")
    assert config.bind_port() == 9002


def test_a_lake_uri_that_is_not_s3_is_refused(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "/some/folder")
    with pytest.raises(ValueError, match="s3://"):
        config.lake_uri()


def test_dev_open_is_refused_on_a_staging_or_prod_server(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    monkeypatch.setenv("IRONWRIGHT_ENV", "staging")
    with pytest.raises(SystemExit, match="DEV_OPEN"):
        hosted.check_posture()
    monkeypatch.setenv("IRONWRIGHT_ENV", "local")
    hosted.check_posture()


def test_every_setting_the_engine_reads_is_in_the_doc():
    doc = (REPO / "docs" / "deploy-hosted.md").read_text()
    names: set[str] = set()
    for f in (REPO / "ironwright").rglob("*.py"):
        text = f.read_text()
        names |= set(re.findall(r"IRONWRIGHT_[A-Z0-9_]+", text))
        names |= {f"IRONWRIGHT_{n}" for n in re.findall(r"\bsetting\(\s*\"([A-Z0-9_]+)\"", text)}
    missing = sorted(n for n in names if f"`{n}`" not in doc)
    assert not missing, f"not documented in docs/deploy-hosted.md: {missing}"


# ---------------------------------------------------------------- lake location
def _pack(name: str = "claims", **extra):
    return SimpleNamespace(name=name, spec=extra.pop("spec", {}), **extra)


def test_without_a_lake_uri_the_lake_stays_on_local_disk():
    assert lake_location(load_pack("retail")) is None


def test_a_demo_pack_lives_under_the_demo_prefix(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake/stage")
    loc = lake_location(_pack("retail"))
    assert loc.warehouse == "s3://iw-lake/stage/demo/retail/lake"
    assert loc.catalog == "demo/retail"


def test_a_tenant_workspace_lives_where_contract_2_says(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake/stage/")
    loc = lake_location(_pack("claims", org_id="abc123", spec={"kind": "workspace"}))
    assert loc.warehouse == "s3://iw-lake/stage/tenants/abc123/workspaces/claims/lake"


def test_a_bucket_with_no_prefix_makes_a_clean_path(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake")
    assert lake_location(_pack("retail")).warehouse == "s3://iw-lake/demo/retail/lake"


def test_two_orgs_with_one_workspace_slug_never_share_a_catalog(monkeypatch):
    # SqlCatalog partitions a shared database by catalog name, and only by that.
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake")
    a = lake_location(_pack("claims", org_id="orga", spec={"kind": "workspace"}))
    b = lake_location(_pack("claims", org_id="orgb", spec={"kind": "workspace"}))
    assert a.catalog != b.catalog and a.warehouse != b.warehouse


def test_a_tenant_workspace_with_no_org_fails_closed(monkeypatch):
    # Falling through to demo/<name> would put two tenants in one warehouse.
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake")
    with pytest.raises(ValueError, match="org"):
        lake_location(_pack("claims", spec={"kind": "workspace"}))


@pytest.mark.parametrize("org", ["../other", "a/b", "..", ""])
def test_an_org_id_cannot_climb_out_of_its_prefix(monkeypatch, org):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake")
    with pytest.raises(ValueError):
        lake_location(_pack("claims", org_id=org, spec={"kind": "workspace"}))


def test_the_catalog_uses_the_metadata_database_when_hosted(monkeypatch, tmp_path):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake")
    monkeypatch.setenv("IRONWRIGHT_DB_URL", "postgres://u:p@db/iw")
    assert lake_location(_pack("retail")).catalog_uri == "postgresql+psycopg://u:p@db/iw"


# ---------------------------------------------------------------- health routes
@pytest.fixture()
def closed_door(monkeypatch):
    """conftest opens the platform for the rest of the suite. These prove the real door."""
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)


def test_healthz_answers_with_no_session(closed_door):
    r = TestClient(hosted.create_hosted_app()).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_answers_with_no_session_on_local_settings(closed_door):
    r = TestClient(hosted.create_hosted_app()).get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"db": "ok", "object_store": "ok"}}


def test_the_health_routes_did_not_open_the_rest_of_the_platform(closed_door):
    assert TestClient(hosted.create_hosted_app()).get("/workspaces").status_code == 401


def test_healthz_stays_up_when_a_dependency_is_down(closed_door, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DB_URL", "postgresql://u:hunter2@127.0.0.1:1/iw")
    assert TestClient(hosted.create_hosted_app()).get("/healthz").status_code == 200


def test_readyz_names_a_dead_database_and_leaks_nothing(closed_door, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DB_URL", "postgresql://svc:hunter2@127.0.0.1:1/iw")
    r = TestClient(hosted.create_hosted_app()).get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "checks": {"db": "failed", "object_store": "ok"}}
    assert "hunter2" not in r.text and "127.0.0.1" not in r.text


def test_readyz_names_a_dead_object_store_and_leaks_nothing(closed_door, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://secret-bucket/x")
    monkeypatch.setenv("IRONWRIGHT_S3_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAhunter2")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    r = TestClient(hosted.create_hosted_app()).get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["object_store"] == "failed"
    assert not {"hunter2", "secret-bucket"} & set(re.findall(r"[\w-]+", r.text))


def test_only_get_reads_the_health_routes(closed_door):
    assert TestClient(hosted.create_hosted_app()).post("/healthz").status_code == 405


# ---------------------------------------------------------------- image and compose posture
def _compose() -> dict:
    return yaml.safe_load((REPO / "deploy" / "compose.yaml").read_text())


def test_compose_has_the_four_services_and_an_init_for_the_bucket():
    assert {"api", "worker", "postgres", "objectstore", "bucket-init"} <= set(_compose()["services"])


def test_compose_pulls_nothing_from_the_withdrawn_minio_distribution():
    # Docker Hub and dl.min.io stopped serving MinIO. A pin on them cannot even pull.
    images = [v.get("image", "") for v in _compose()["services"].values()]
    assert not [i for i in images if "minio" in i]


def test_the_bucket_init_uses_our_own_image_and_needs_no_extra_client():
    init = _compose()["services"]["bucket-init"]
    assert init["image"] == "ironwright:local"
    assert "init_bucket" in " ".join(init["command"])


def test_compose_publishes_only_the_api_and_only_on_loopback():
    services = _compose()["services"]
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]
    assert not [s for s, v in services.items() if v.get("ports") and s != "api"]


def test_compose_makes_the_container_bind_explicit():
    assert _compose()["services"]["api"]["environment"]["IRONWRIGHT_HOST"] == "0.0.0.0"


def test_compose_leaves_the_door_closed_by_default():
    env = _compose()["services"]["api"]["environment"]
    assert env["IRONWRIGHT_DEV_OPEN"] == "${IRONWRIGHT_DEV_OPEN:-0}"


def test_the_image_runs_as_a_non_root_user_and_bakes_in_no_secret():
    text = (REPO / "Dockerfile").read_text()
    users = re.findall(r"^USER\s+(\S+)", text, flags=re.MULTILINE)
    assert users and users[-1] not in ("root", "0")
    # ENV and ARG persist in image layers and history.
    assert not re.search(r"^(ENV|ARG)\s+\S*(SECRET|PASSWORD|TOKEN|API_KEY|ACCESS_KEY)", text, flags=re.MULTILINE)
    assert "ENV IRONWRIGHT_HOST=" not in text, "the bind is stated by whoever runs the container"


def test_the_docker_context_keeps_local_secrets_and_data_out():
    ignored = (REPO / ".dockerignore").read_text().split()
    assert {".env", "data", ".git", ".agent"} <= set(ignored)


# ---------------------------------------------------------------- real object store
@pytest.fixture()
def s3_lake(monkeypatch):
    endpoint = os.environ.get("IRONWRIGHT_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("IRONWRIGHT_TEST_S3_ENDPOINT not set: the object store lake is "
                    "code-present but only claimed where a real endpoint proves it")
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", os.environ.get("IRONWRIGHT_TEST_S3_KEY", "testing"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", os.environ.get("IRONWRIGHT_TEST_S3_SECRET", "testing"))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("IRONWRIGHT_S3_ENDPOINT", endpoint)
    bucket = f"iw-test-{uuid.uuid4().hex[:10]}"
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    client.create_bucket(Bucket=bucket)
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", f"s3://{bucket}/t")
    return SimpleNamespace(client=client, bucket=bucket)


@pytest.fixture()
def catalog_db(request, monkeypatch):
    """A throwaway schema when there is a Postgres to prove on. Catalog names are
    fixed, so rows left in a shared database would point at a bucket from an old run."""
    if not os.environ.get("POSTGRES_URI"):
        return
    schema = request.getfixturevalue("pg_schema")
    uri = os.environ["POSTGRES_URI"]
    sep = "&" if "?" in uri else "?"
    monkeypatch.setenv("IRONWRIGHT_DB_URL", f"{uri}{sep}options=-csearch_path%3D{schema}")


def _keys(s3_lake, prefix: str) -> list[str]:
    pages = s3_lake.client.get_paginator("list_objects_v2").paginate(Bucket=s3_lake.bucket, Prefix=prefix)
    return [o["Key"] for page in pages for o in page.get("Contents", [])]


@pytest.mark.parametrize("name", list_packs())
def test_pack_passes_all_eval_layers_on_object_storage(s3_lake, catalog_db, tmp_path, name):
    pack = load_pack(name)
    tracer = Tracer(pack.paths.ensure().traces)
    args = argparse.Namespace(limit=None, seed={"retail": 7, "supply_chain": 11, "banking_aml": 23,
                                                "insurance_claims": 31}.get(name, 7))
    for stage in ("generate", "migrate", "reconcile", "ontology", "agents", "evals"):
        STAGES[stage](pack, tracer, args)
    rep = json.loads((pack.paths.reports / "evals.json").read_text())
    failed = [f"{layer['layer']}: {layer['headline']}" for layer in rep["layers"] if not layer["passed"]]
    assert not failed, failed
    # Green is not enough: the data has to be in the bucket and not on the disk.
    assert any(k.endswith(".parquet") for k in _keys(s3_lake, f"t/demo/{name}/lake/"))
    assert not list(pack.paths.lake.rglob("*.parquet"))


def test_two_orgs_with_one_slug_stay_apart_in_one_catalog_database(s3_lake, catalog_db, tmp_path, monkeypatch):
    import pyarrow as pa

    if not os.environ.get("POSTGRES_URI"):
        monkeypatch.setenv("IRONWRIGHT_DB_URL", f"sqlite:///{tmp_path}/shared.sqlite")
    slug = f"claims{uuid.uuid4().hex[:6]}"

    def tenant(org: str):
        pack = load_pack("retail")
        return SimpleNamespace(name=slug, org_id=org, spec={"kind": "workspace"}, paths=pack.paths)

    orga, orgb = f"a{uuid.uuid4().hex[:8]}", f"b{uuid.uuid4().hex[:8]}"
    lake_a, lake_b = Lakehouse(tenant(orga)), Lakehouse(tenant(orgb))
    data = pa.table({"secret": ["only for a"]})
    table = lake_a.catalog.create_table("silver.claims", schema=data.schema)
    table.append(data)

    assert lake_a.tables() == ["silver.claims"]
    assert lake_b.tables() == []
    assert _keys(s3_lake, f"t/tenants/{orga}/workspaces/{slug}/lake/")
    assert not _keys(s3_lake, f"t/tenants/{orgb}/")


def test_init_bucket_creates_the_bucket_and_can_run_twice(s3_lake, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", f"s3://{s3_lake.bucket}-new/t")
    hosted.init_bucket(wait_seconds=10)
    hosted.init_bucket(wait_seconds=10)
    s3_lake.client.head_bucket(Bucket=f"{s3_lake.bucket}-new")


def test_init_bucket_gives_up_loudly_when_the_store_never_answers(monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://iw-lake/t")
    monkeypatch.setenv("IRONWRIGHT_S3_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    with pytest.raises(SystemExit, match="object store"):
        hosted.init_bucket(wait_seconds=0.1)


def test_init_bucket_with_no_lake_uri_has_nothing_to_do():
    hosted.init_bucket(wait_seconds=0.1)


# ---------------------------------------------------------------- the S5 serving posture
@pytest.fixture()
def launch(monkeypatch):
    """uvicorn.run captured, so the bind rule is tested without binding anything."""
    calls: list[dict] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append({"app": app, **kw}))
    return calls


def test_serve_platform_binds_loopback_by_default(launch):
    from ironwright.cli import main
    main(["serve-platform"])
    assert launch[0]["host"] == "127.0.0.1" and launch[0]["port"] == 8000
    assert launch[0]["proxy_headers"] is False and launch[0]["server_header"] is False


def test_exposure_from_env_alone_refuses_to_start(launch, monkeypatch):
    # S5's rule is two keys: an address and a stated acknowledgment. One setting is not enough.
    from ironwright.cli import main
    monkeypatch.setenv("IRONWRIGHT_HOST", "0.0.0.0")
    with pytest.raises(SystemExit, match="IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED"):
        main(["serve-platform"])
    assert not launch


def test_exposure_with_both_keys_binds_and_warns(launch, monkeypatch, capsys):
    from ironwright.cli import main
    monkeypatch.setenv("IRONWRIGHT_HOST", "0.0.0.0")
    monkeypatch.setenv("IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED", "1")
    main(["serve-platform"])
    assert launch[0]["host"] == "0.0.0.0"
    assert "exposed" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("flags", [["--host", "0.0.0.0"], ["--port", "9000"], ["--i-know-this-is-exposed"]])
def test_serve_platform_takes_its_bind_from_env_and_refuses_flags(launch, flags):
    # A flag it silently ignored would leave someone believing they had chosen an address.
    from ironwright.cli import main
    with pytest.raises(SystemExit):
        main(["serve-platform", *flags])
    assert not launch


def test_the_local_platform_command_keeps_s5s_flag_rule(launch, monkeypatch):
    from ironwright.cli import main
    monkeypatch.setenv("IRONWRIGHT_HOST", "0.0.0.0")
    monkeypatch.setenv("IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED", "1")
    main(["platform"])
    assert launch[0]["host"] == "127.0.0.1"
    with pytest.raises(SystemExit):
        main(["platform", "--host", "0.0.0.0"])


def test_the_hosted_app_carries_the_hardening(closed_door):
    # Every response, the public health routes included, has S5's security headers.
    c = TestClient(hosted.create_hosted_app())
    for path in ("/healthz", "/readyz", "/workspaces"):
        assert c.get(path).headers.get("x-content-type-options") == "nosniff", path
    assert c.post("/auth/login", json={"email": "x@y", "password": "w"},
                  headers={"Origin": "https://evil.example"}).status_code == 403


def test_the_kill_switch_does_not_take_the_health_routes_down(closed_door, monkeypatch):
    # An operator who pulled the switch must not also get the service restarted by its health check.
    monkeypatch.setenv("IRONWRIGHT_KILL_SWITCH", "1")
    c = TestClient(hosted.create_hosted_app())
    assert c.get("/healthz").status_code == 200 and c.get("/readyz").status_code == 200


def test_compose_states_both_exposure_keys():
    env = _compose()["services"]["api"]["environment"]
    assert env["IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED"] == "1"


# ---------------------------------------------------------------- with T1's tenancy
@pytest.mark.parametrize("given", [None, "postgres://u:p@db/iw?options=-csearch_path%3Dx"])
def test_the_lake_catalog_and_tenancy_use_one_store(monkeypatch, given):
    from ironwright.tenancy import db
    if given:
        monkeypatch.setenv("IRONWRIGHT_DB_URL", given)
    assert config.db_url() == db._current_url()


def test_the_image_gets_the_postgres_driver_from_the_lock():
    text = (REPO / "Dockerfile").read_text()
    assert "--extra postgres" in text
    assert "uv pip install" not in text, "a pin outside the lock drifts from it"


def test_the_ci_smoke_runs_with_the_door_closed():
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    assert "IRONWRIGHT_DEV_OPEN" not in (ci["jobs"]["hosted-smoke"].get("env") or {})
