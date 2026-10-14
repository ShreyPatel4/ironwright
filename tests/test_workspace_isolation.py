"""Two workspaces, zero cross-observability.

The SaaS posture for now is: keep the boundaries clean, prove isolation
with an assertion instead of a migration. This builds two packs side by
side under one data root and asserts that nothing in A is observable from
B through the API surfaces a reader has: object reads, audit reads, and
the root report. If this ever fails, that failure is the finding.
"""
import argparse
import json

import pytest
from fastapi.testclient import TestClient

from ironwright.api import create_app
from ironwright.cli import STAGES
from ironwright.packs import load_pack
from ironwright.tracing import Tracer

A, B = "retail", "supply_chain"
SEEDS = {A: 7, B: 11}


@pytest.fixture(scope="module")
def two_workspaces(tmp_path_factory):
    root = tmp_path_factory.mktemp("iso")
    import os

    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        for name in (A, B):
            pack = load_pack(name)
            tracer = Tracer(pack.paths.ensure().traces)
            args = argparse.Namespace(seed=SEEDS[name], limit=None)
            for stage in ("generate", "migrate", "reconcile", "ontology", "agents"):
                STAGES[stage](pack, tracer, args)
        yield root
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


def _audit_hashes(client: TestClient) -> set[str]:
    rows = client.get("/audit", params={"limit": 1000}).json()
    return {r["hash"] for r in rows}


def test_nothing_in_one_workspace_is_observable_from_the_other(two_workspaces):
    with TestClient(create_app(A)) as ca, TestClient(create_app(B)) as cb:
        # The root report names its own pack and only its own pack.
        assert ca.get("/").json()["pack"] == A
        assert cb.get("/").json()["pack"] == B

        # Audit chains are fully disjoint. Hashes cover the whole row, so a
        # single shared hash would mean a shared or leaked store.
        ha, hb = _audit_hashes(ca), _audit_hashes(cb)
        assert ha and hb
        assert not (ha & hb)

        # An object type that exists only in B is not served by A, whatever
        # the id. Pick one from B's own ontology report.
        b_types = set(cb.get("/").json()["ontology"]["object_types"])
        a_types = set(ca.get("/").json()["ontology"]["object_types"])
        only_b = b_types - a_types
        assert only_b, "packs share every object type; pick better packs for this test"
        for t in sorted(only_b):
            resp = ca.get(f"/objects/{t}")
            assert resp.status_code in (404, 422) or resp.json() in ([], {}), (
                f"workspace {A} serves type {t} that belongs only to {B}"
            )


def test_workspace_data_stays_under_its_own_root(two_workspaces):
    root = two_workspaces
    for name in (A, B):
        pack = load_pack(name)
        assert str(pack.paths.root).startswith(str(root / name)), (
            f"{name} resolves paths outside its workspace root"
        )
    # No file of A's workspace lives under B's directory and vice versa.
    a_files = {p.name for p in (root / A).rglob("*") if p.is_file()}
    assert (root / B / "ontology.duckdb").exists()
    assert not any((root / B / n).exists() and n.endswith(".duckdb") and n != "ontology.duckdb" for n in a_files)
