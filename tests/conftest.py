"""Shared fixtures. The postgres-backed rungs (CDC, engine swap, BI mirror)
prove themselves against a real server named by POSTGRES_URI; without it
their tests skip with the posture stated, never pass vacuously."""
from __future__ import annotations

import os
import uuid

import pytest

# The suite predates real sessions and drives the API without logging in.
# tests/test_authn.py clears this flag and proves the closed door.
os.environ["IRONWRIGHT_DEV_OPEN"] = "1"


@pytest.fixture()
def postgres_uri() -> str:
    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        pytest.skip("POSTGRES_URI not set: postgres-backed rungs are "
                    "code-present but only claimed where a real server proves them")
    psycopg = pytest.importorskip("psycopg")
    try:
        with psycopg.connect(uri, connect_timeout=3):
            pass
    except Exception as e:  # noqa: BLE001  unreachable server: skip honestly, never error
        pytest.skip(f"POSTGRES_URI set but unreachable: {e}")
    return uri


@pytest.fixture()
def pg_schema(postgres_uri: str):
    """A throwaway schema per test, dropped afterwards, so parallel tests
    and reruns never see each other's tables."""
    import psycopg

    name = f"iw_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(postgres_uri, autocommit=True) as con:
        con.execute(f"create schema {name}")
    try:
        yield name
    finally:
        with psycopg.connect(postgres_uri, autocommit=True) as con:
            con.execute(f"drop schema {name} cascade")


@pytest.fixture(params=["sqlite", "postgres"])
def meta_db(request, monkeypatch, tmp_path):
    """A fresh tenancy store on each backend, with the door closed.

    CI has POSTGRES_URI, so there the postgres half fails instead of skipping.
    A proof of tenant isolation that quietly did not run is worse than a red build.
    """
    monkeypatch.delenv("IRONWRIGHT_DEV_OPEN", raising=False)
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    from ironwright.tenancy import db
    if request.param == "sqlite":
        yield "sqlite"
        db.dispose_current()
        return
    try:
        schema = request.getfixturevalue("pg_schema")
    except pytest.skip.Exception as e:
        if os.environ.get("CI"):
            pytest.fail(f"postgres half of the tenancy proof cannot run in CI: {e}")
        raise
    uri = os.environ["POSTGRES_URI"]
    sep = "&" if "?" in uri else "?"
    monkeypatch.setenv("IRONWRIGHT_DB_URL", f"{uri}{sep}options=-csearch_path%3D{schema}")
    yield "postgres"
    db.dispose_current()  # CI's server allows 100 connections, and each test's URL is new
