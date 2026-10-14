"""Every pack, end to end, must pass every eval layer. This is the real regression test:
if a change breaks identity, a quality gate, reconciliation, a policy or an agent decision, it fails here."""
import argparse
import json

import pytest

from ironwright.cli import STAGES
from ironwright.packs import list_packs, load_pack
from ironwright.tracing import Tracer


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))


@pytest.mark.parametrize("name", list_packs())
def test_pack_passes_all_eval_layers(name):
    pack = load_pack(name)
    tracer = Tracer(pack.paths.ensure().traces)
    args = argparse.Namespace(seed=None, limit=None)
    args.seed = {"retail": 7, "supply_chain": 11, "banking_aml": 23, "insurance_claims": 31}.get(name, 7)
    for stage in ("generate", "migrate", "reconcile", "ontology", "agents", "evals"):
        STAGES[stage](pack, tracer, args)
    rep = json.loads((pack.paths.reports / "evals.json").read_text())
    failed = [layer["layer"] + ": " + layer["headline"] for layer in rep["layers"] if not layer["passed"]]
    assert not failed, failed


def test_api_masks_sensitive_fields_and_scopes_vendors(tmp_path):
    from fastapi.testclient import TestClient
    from ironwright.api import create_app

    pack = load_pack("banking_aml")
    tracer = Tracer(pack.paths.ensure().traces)
    args = argparse.Namespace(seed=23, limit=3)
    for stage in ("generate", "migrate", "ontology"):
        STAGES[stage](pack, tracer, args)
    c = TestClient(create_app("banking_aml"))
    cust = c.get("/objects/Customer", params={"limit": 1}).json()[0]
    assert cust["dob"] == "***"
    seen = c.get(f"/objects/Customer/{cust['_id']}", headers={"X-Principal": "user:aml.analyst.queue1"}).json()
    assert seen["dob"] != "***"
    r = c.post("/actions/close_alert", json={"params": {"alert_id": "x"}}, headers={"X-Principal": "agent:aml-triage"})
    assert r.json()["status"] == "denied"
