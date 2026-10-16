"""Slice A gate (sketch 08, sem_asof): object-level as-of reads.

object_history already versions every object. get_asof resolves an object to
its state at time ts, and the /asof endpoint serves that read masked like
every other object read. Function-level as-of is out of this slice.
"""
import datetime
import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("asof")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        from ironwright.asof import router
        from ironwright.platform_api import create_platform_app

        app = create_platform_app()
        app.include_router(router)
        with TestClient(app) as c:
            r = c.post("/workspaces/retail/runs", json={"seed": 7})
            assert r.status_code == 200, r.text
            yield c
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


def _versioned_object() -> tuple[str, str, list[tuple]]:
    """A (type, id) with 2+ history versions whose props differ, plus its
    full history. Agents mutate objects; if this run left none with two
    differing versions, drive one mutation through Ontology.act with a
    principal the pack's policy permits (category managers may act)."""
    from ironwright.ontology.runtime import Ontology
    from ironwright.packs import load_pack
    from ironwright.tracing import Tracer

    pack = load_pack("retail")
    ont = Ontology(pack, Tracer(None), attach_lake=False)
    try:
        row = ont.store.con.execute(
            "select a.type, a.id from object_history a join object_history b "
            "using (type, id) where a.version <> b.version and a.props <> b.props "
            "order by a.type, a.id limit 1").fetchone()
        if row is None:
            item = ont.store.con.execute(
                "select id from objects where type = 'Item' order by id limit 1"
            ).fetchone()[0]
            res = ont.act("delist_item", {"item_id": item},
                          actor="user:category.manager.1")
            assert res["status"] == "applied", res
            row = ("Item", item)
        history = ont.store.con.execute(
            "select version, props, ts from object_history where type = ? and id = ? "
            "order by version", [row[0], row[1]]).fetchall()
        return row[0], row[1], history
    finally:
        ont.store.con.close()


META = {"_id", "_type", "_version", "_title", "_asof_ts"}


def _props(obj: dict) -> dict:
    """The comparable payload: the stored props without the derived _meta
    keys. _prov and _pending_sync live in the stored props, so they stay."""
    return {k: v for k, v in obj.items() if k not in META}


def test_get_asof_resolves_each_version_from_history(client):
    from ironwright.ontology.store import OntologyStore
    from ironwright.packs import load_pack

    type_, id_, history = _versioned_object()
    assert len(history) >= 2
    (v1, p1, t1), (v2, p2, t2) = history[0], history[-1]
    assert t2 > t1, "two versions written at the same instant; cannot bisect"
    mid = (t1 + t2) / 2

    store = OntologyStore(load_pack("retail").paths.ontology_db, read_only=True)
    try:
        early = store.get_asof(type_, id_, mid)
        late = store.get_asof(type_, id_, t2 + 1.0)
        before_birth = store.get_asof(type_, id_, t1 - 1.0)
        never = store.get_asof(type_, "no-such-id", t2)
    finally:
        store.con.close()

    assert early is not None and late is not None
    assert early["_version"] < late["_version"]
    assert late["_version"] == v2
    assert _props(early) == json.loads(p1)
    assert {k: late[k] for k in json.loads(p2)} == json.loads(p2)
    assert _props(early) != _props(late), "the change must be visible across versions"
    assert before_birth is None
    assert never is None


def test_asof_endpoint_reads_before_and_after_the_change(client):
    type_, id_, history = _versioned_object()
    (v1, p1, t1), (v2, p2, t2) = history[0], history[-1]
    mid = (t1 + t2) / 2

    early = client.get(f"/workspaces/retail/objects/{type_}/{id_}/asof",
                       params={"ts": str(mid)})
    assert early.status_code == 200, early.text
    body = early.json()
    assert body["version"] == v1
    assert _props(body["object"]) == json.loads(p1)
    assert body["_as"] == "user:console"

    late = client.get(f"/workspaces/retail/objects/{type_}/{id_}/asof",
                      params={"ts": str(t2 + 1.0)})
    assert late.status_code == 200
    assert late.json()["version"] == v2
    assert _props(late.json()["object"]) != _props(body["object"])


def test_asof_endpoint_accepts_iso_timestamps_and_a_principal(client):
    type_, id_, history = _versioned_object()
    _, _, t_last = history[-1]
    iso = datetime.datetime.fromtimestamp(t_last + 1.0,
                                          tz=datetime.timezone.utc).isoformat()
    r = client.get(f"/workspaces/retail/objects/{type_}/{id_}/asof",
                   params={"ts": iso}, headers={"x-principal": "vendor:V-00001"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == history[-1][0]
    assert r.json()["_as"] == "vendor:V-00001"

    bad = client.get(f"/workspaces/retail/objects/{type_}/{id_}/asof",
                     params={"ts": "not-a-time"})
    assert bad.status_code == 422


def test_asof_before_the_object_existed_is_a_404(client):
    type_, id_, history = _versioned_object()
    t_first = history[0][2]
    r = client.get(f"/workspaces/retail/objects/{type_}/{id_}/asof",
                   params={"ts": str(t_first - 60.0)})
    assert r.status_code == 404
    missing = client.get(f"/workspaces/retail/objects/{type_}/never-existed/asof",
                         params={"ts": str(t_first)})
    assert missing.status_code == 404
    nows = client.get("/workspaces/nonsense/objects/Item/x/asof",
                      params={"ts": "1"})
    assert nows.status_code == 404
