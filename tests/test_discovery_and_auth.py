"""Discovery's gate (sketch 01) and the invite gate.

The discovery derivation never reads ground truth; THIS TEST reads it and
proves the derivation found the planted facts without being told they
were planted. The auth tests prove the gate rejects wrong credentials and
stores no plaintext anywhere.
"""
import argparse
import json

import pytest
from fastapi.testclient import TestClient

from ironwright.platform_api import create_platform_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os

    root = tmp_path_factory.mktemp("disc")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        with TestClient(create_platform_app()) as c:
            c.post("/workspaces/retail/runs", json={"seed": 7})
            yield c
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


def test_discovery_finds_the_planted_facts_blind(client):
    from ironwright.packs import load_pack

    d = client.get("/workspaces/retail/discovery").json()
    identity = [f for f in d["findings"] if f["class"] == "identity" and "members" in f["evidence"]]
    found_sets = [frozenset(f["evidence"]["members"]) for f in identity]

    gt = json.loads(load_pack("retail").paths.ground_truth.read_text())
    planted_interesting = []
    for groups in gt["entities"].values():
        for members in groups:
            systems = [m.split(":", 1)[0] for m in members]
            if len(members) > 2 or len(systems) != len(set(systems)):
                planted_interesting.append(frozenset(members))
    assert planted_interesting, "retail should plant at least one interesting identity"
    for planted in planted_interesting:
        assert planted in found_sets, f"planted identity not discovered: {sorted(planted)}"

    # The flagship reads like a finding, not a log line.
    assert any("exists 3 times" in f["title"] and f["severity"] == "high" for f in identity)
    # Routine resolution is summarized, not itemized.
    assert sum(1 for f in d["findings"] if f["class"] == "identity") <= 6


def test_discovery_never_invents(client):
    d = client.get("/workspaces/retail/discovery").json()
    for f in d["findings"]:
        assert f["method"], f
        assert f["evidence"], f
    assert set(d["counts"]) <= {"identity", "contradiction", "orphan"}


def test_invite_gate_accepts_and_rejects(client, tmp_path):
    from ironwright import auth

    assert client.get("/auth/status").json()["seeded"] is False
    auth.set_account("tester@example.com", "correct-horse-9")
    assert client.get("/auth/status").json()["seeded"] is True

    bad = client.post("/auth/login", json={"email": "tester@example.com", "password": "wrong"})
    assert bad.status_code == 401
    good = client.post("/auth/login",
                       json={"email": "tester@example.com", "password": "correct-horse-9"}).json()
    assert good["token"] and good["principal"] == "user:tester"

    me = client.get("/auth/me", headers={"x-session": good["token"]}).json()
    assert me["email"] == "tester@example.com"
    client.post("/auth/logout", headers={"x-session": good["token"]})
    assert client.get("/auth/me", headers={"x-session": good["token"]}).status_code == 401

    # No plaintext anywhere in the credentials file.
    raw = (auth._path()).read_text()
    assert "correct-horse-9" not in raw
