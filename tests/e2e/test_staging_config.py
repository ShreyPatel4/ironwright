"""The Railway staging config. Nothing here talks to Railway, and CI must not either."""
from __future__ import annotations

from pathlib import Path

import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[2]
TOML = ROOT / "deploy" / "railway" / "railway.toml"
ENV = ROOT / "deploy" / "railway" / "staging.env.example"


def _env() -> dict[str, str]:
    out = {}
    for line in ENV.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_the_service_builds_the_one_image_and_runs_both_processes():
    """T5's file. tests/test_hosted.py owns how the start command behaves."""
    deploy = tomllib.loads(TOML.read_text())["deploy"]
    assert deploy["healthcheckPath"] == "/readyz"
    assert "ironwright worker" in deploy["startCommand"] and "ironwright serve-platform" in deploy["startCommand"]


def test_the_env_says_staging_with_the_door_closed():
    env = _env()
    assert env["IRONWRIGHT_ENV"] == "staging"
    assert "IRONWRIGHT_DEV_OPEN" not in env
    assert env["IRONWRIGHT_RUNS_WAIT"] == "0", "a run must not land in the API process"
    assert env["IRONWRIGHT_HOST"] == "0.0.0.0"
    assert env["IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED"] == "1"


def test_the_env_names_postgres_object_storage_and_the_data_volume():
    env = _env()
    assert env["IRONWRIGHT_DB_URL"] == "${{Postgres.DATABASE_URL}}"
    assert env["IRONWRIGHT_LAKE_URI"].startswith("s3://")
    assert env["IRONWRIGHT_DATA"] == "/data"
    assert {"IRONWRIGHT_S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"} <= set(env)


def test_the_example_holds_no_secret_values():
    env = _env()
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "IRONWRIGHT_SEED_PASSWORD"):
        assert env[key] == "", f"{key} must be blank in a committed file"


def test_every_setting_is_documented():
    docs = (ROOT / "docs" / "deploy-hosted.md").read_text()
    missing = [k for k in _env() if f"`{k}`" not in docs]
    assert not missing


def test_ci_never_deploys_to_railway():
    """The PMO asks Shrey before anything is created."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = [s.get("run", "") for job in workflow["jobs"].values() for s in job["steps"]]
    assert not [r for r in runs if "railway" in r.lower()]
