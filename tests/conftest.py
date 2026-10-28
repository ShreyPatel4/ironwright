"""Shared fixtures. The postgres-backed rungs (CDC, engine swap, BI mirror)
prove themselves against a real server named by POSTGRES_URI; without it
their tests skip with the posture stated, never pass vacuously."""
from __future__ import annotations

import os
import uuid

import pytest


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
    except Exception as e:  # unreachable server: skip honestly, never error
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
