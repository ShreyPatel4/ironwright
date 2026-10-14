"""Ask evals: grounded answers, honest refusals, replayability.

The abstention contract is tested with a fixed question set: every in-scope
question must answer with citations that replay to identical digests, and
every out-of-scope question must refuse with a named reason. The refusal
rate over this set is the published number; it is computed, not asserted
into existence.
"""
import argparse
import json

import pytest
from fastapi.testclient import TestClient

from ironwright.platform_api import create_platform_app

OUT_OF_SCOPE = [
    "what is the weather in boston",
    "delete every vendor and start over",
    "forecast revenue for next quarter",
]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os

    root = tmp_path_factory.mktemp("ask")
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


def _ids(client):
    from ironwright.ontology.store import OntologyStore
    from ironwright.packs import load_pack

    store = OntologyStore(load_pack("retail").paths.ontology_db)
    try:
        item = store.con.execute("select id from objects where type='Item' order by id limit 1").fetchone()[0]
        st = store.con.execute("select id from objects where type='Store' order by id limit 1").fetchone()[0]
        return item, st
    finally:
        store.con.close()


def test_in_scope_questions_answer_with_citations_and_replay(client):
    item, store = _ids(client)
    in_scope = [
        f"units and sales expectation for item {item} in store {store}",
        f"out of stock days and on-hand for item {item} in store {store}",
        f"which planogram version is in effect and the facings for item {item} in store {store}",
    ]
    for q in in_scope:
        a = client.post("/workspaces/retail/ask", json={"question": q}).json()
        assert a["status"] == "answered", (q, a.get("reason"))
        assert a["calls"] and a["calls"][0]["result_digest"]
        assert a["answer"]
        assert a["narrator"] == "offline-template"
        assert a["verification"]["level"] == "L3"

    log = client.get("/workspaces/retail/ask/log").json()
    answered_idx = [i for i, e in enumerate(log) if e["status"] == "answered"]
    for i in answered_idx:
        rep = client.post("/workspaces/retail/ask/replay", json={"index": i}).json()
        assert rep["replayable"] and rep["match"], log[i]["question"]


def test_out_of_scope_questions_refuse_with_named_reasons(client):
    for q in OUT_OF_SCOPE:
        a = client.post("/workspaces/retail/ask", json={"question": q}).json()
        assert a["status"] == "refused", q
        assert a["reason_code"] in ("out_of_scope", "ambiguous", "unresolved_params")
        assert a["reason"]


def test_refusal_rate_is_published_from_the_log(client):
    m = client.get("/workspaces/retail/ask/metrics").json()
    assert m["asked"] == m["answered"] + m["refused"]
    assert m["answered"] >= 3 and m["refused"] >= 3
    assert 0 < m["refusal_rate"] < 1


def test_made_up_ids_are_refused_not_guessed(client):
    a = client.post("/workspaces/retail/ask",
                    json={"question": "units and sales for item ZZZ999 in store QQ1"}).json()
    assert a["status"] == "refused"
    assert a["reason_code"] == "unresolved_params"
