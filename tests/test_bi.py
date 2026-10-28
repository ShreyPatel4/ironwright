"""BI mirror and embed contract tests.

The mirror is proven against a real postgres named by POSTGRES_URI; the
conftest fixtures skip honestly without one, never pass vacuously. The
embed config and its URL guardrails need no server and always run. Like
test_orgpolicy.py gate 4, a packs-tree snapshot brackets the module:
nothing here may ever write under ironwright/packs/.
"""
import argparse
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

GOOD_URL = "https://metabase.example.com/public/dashboard/3f9a"


def _packs_tree() -> list[str]:
    """Every path under ironwright/packs/, bytecode caches excluded: the
    generate stage imports each pack's generator.py, and the interpreter
    writes __pycache__ beside it. That is an import artifact, not a data
    write; any other new path still fails the gate."""
    from ironwright.config import PACKS_DIR

    return sorted(str(p) for p in PACKS_DIR.rglob("*")
                  if "__pycache__" not in p.parts and p.suffix != ".pyc")


@pytest.fixture(scope="module")
def packs_before():
    return _packs_tree()


@pytest.fixture(scope="module")
def retail(tmp_path_factory, packs_before):
    """A tmp retail workspace run through generate and migrate, the two
    stages that fill the lake the mirror reads."""
    root = tmp_path_factory.mktemp("bi")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    from ironwright.cli import STAGES
    from ironwright.packs import load_pack
    from ironwright.tracing import Tracer

    pack = load_pack("retail")
    tracer = Tracer(pack.paths.ensure().traces)
    args = argparse.Namespace(seed=7, limit=None)
    for stage in ("generate", "migrate"):
        STAGES[stage](pack, tracer, args)
    try:
        yield pack
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


@pytest.fixture(scope="module")
def client(retail):
    from ironwright import bi
    from ironwright.platform_api import create_platform_app

    app = create_platform_app()
    app.include_router(bi.router)
    with TestClient(app) as c:
        yield c


def _lake_expectations(pack) -> dict[str, int]:
    from ironwright.lake import Lakehouse

    lake = Lakehouse(pack)
    out = {}
    for ident in lake.tables():
        layer, name = ident.split(".")
        if layer in ("silver", "gold"):
            out[f"{layer}_{name}"] = lake.read(ident).num_rows
    return out


# ----------------------------------------------------------------- mirror

def test_mirror_round_trip_matches_every_silver_and_gold_table(retail, postgres_uri, pg_schema):
    import psycopg

    from ironwright import bi

    expected = _lake_expectations(retail)
    assert any(t.startswith("gold_") for t in expected), "retail has gold tables"
    assert any(t.startswith("silver_") for t in expected), "retail has silver tables"

    report = bi.mirror_gold(retail, postgres_uri, pg_schema)
    got = {t["table"]: t for t in report["tables"]}
    assert set(got) == set(expected), "every silver and gold table mirrors, nothing else"
    for table, rows in expected.items():
        assert got[table]["rows"] == rows
        assert got[table]["match"] is True, table
    assert report["matched"] is True
    assert report["schema"] == pg_schema

    # Independent proof the bytes landed: psycopg counts the postgres side
    # without going through the module under test.
    with psycopg.connect(postgres_uri) as con:
        for table, rows in expected.items():
            n = con.execute(f'select count(*) from "{pg_schema}"."{table}"').fetchone()[0]
            assert n == rows, table


def test_planted_corruption_flips_match_to_false(retail, postgres_uri, pg_schema):
    """The comparison has teeth: a changed value, a deleted row and a
    dropped table each flip exactly their own table to match false."""
    import psycopg

    from ironwright import bi

    report = bi.mirror_gold(retail, postgres_uri, pg_schema)
    assert report["matched"] is True
    with_rows = [t["table"] for t in report["tables"] if t["rows"] > 0]
    assert len(with_rows) >= 3, "the corruption drill needs three non-empty tables"

    with psycopg.connect(postgres_uri, autocommit=True) as con:
        # a value flip, in the first non-empty table that has a text column
        flipped = None
        for table in with_rows:
            row = con.execute(
                "select column_name from information_schema.columns "
                "where table_schema = %s and table_name = %s "
                "and data_type in ('text', 'character varying') limit 1",
                (pg_schema, table)).fetchone()
            if row:
                flipped, col = table, row[0]
                break
        assert flipped, "no mirrored table with rows has a text column to corrupt"
        con.execute(
            f'update "{pg_schema}"."{flipped}" set "{col}" = %s '
            f'where ctid = (select ctid from "{pg_schema}"."{flipped}" limit 1)',
            ("corrupted by the eval",))

        # a deleted row, in a different table
        deleted = next(t for t in with_rows if t != flipped)
        con.execute(f'delete from "{pg_schema}"."{deleted}" '
                    f'where ctid = (select ctid from "{pg_schema}"."{deleted}" limit 1)')

        # a dropped table, in a third
        dropped = next(t for t in with_rows if t not in (flipped, deleted))
        con.execute(f'drop table "{pg_schema}"."{dropped}"')

    verdict = bi.verify_mirror(retail, postgres_uri, pg_schema)
    got = {t["table"]: t["match"] for t in verdict["tables"]}
    assert got[flipped] is False
    assert got[deleted] is False
    assert got[dropped] is False
    assert verdict["matched"] is False
    # corruption is localized: every untouched table still matches
    for table, match in got.items():
        if table not in (flipped, deleted, dropped):
            assert match is True, table


def test_mirror_endpoint_with_named_and_throwaway_schema(client, postgres_uri, pg_schema, monkeypatch):
    import psycopg

    monkeypatch.setenv("POSTGRES_URI", postgres_uri)

    r = client.post("/workspaces/retail/bi/mirror", json={"schema": pg_schema})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["schema"] == pg_schema
    assert report["matched"] is True
    assert report["tables"]

    # no body at all: a throwaway schema is minted and reported
    r2 = client.post("/workspaces/retail/bi/mirror")
    assert r2.status_code == 200, r2.text
    throwaway = r2.json()["schema"]
    try:
        assert throwaway.startswith("iw_bi_")
        assert r2.json()["matched"] is True
    finally:
        with psycopg.connect(postgres_uri, autocommit=True) as con:
            con.execute(f'drop schema if exists "{throwaway}" cascade')


def test_mirror_says_503_when_postgres_uri_is_unset(client, monkeypatch):
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    r = client.post("/workspaces/retail/bi/mirror")
    assert r.status_code == 503
    assert "POSTGRES_URI" in r.json()["detail"]


def test_mirror_schema_name_is_validated_before_any_connection(client, monkeypatch):
    # an unreachable URI on purpose: a rejected schema must answer 422
    # from validation, never 502 from a connection attempt
    monkeypatch.setenv("POSTGRES_URI", "postgresql://nobody:nothing@127.0.0.1:1/nowhere")
    for bad in ("1starts_with_digit", "Capitals", "a" * 60, "x;drop", "pg catalog"):
        r = client.post("/workspaces/retail/bi/mirror", json={"schema": bad})
        assert r.status_code == 422, bad
        assert "schema" in r.json()["detail"]


def test_unknown_workspace_and_org_both_404_honestly(client):
    assert client.post("/workspaces/never-existed/bi/mirror").status_code == 404
    assert client.get("/workspaces/never-existed/bi").status_code == 404

    r = client.post("/workspaces", json={"name": "bi-probe-org", "size": 4, "seed": 3})
    assert r.status_code == 201, r.text
    for resp in (client.post("/workspaces/bi-probe-org/bi/mirror"),
                 client.get("/workspaces/bi-probe-org/bi"),
                 client.put("/workspaces/bi-probe-org/bi", json={"url": GOOD_URL})):
        assert resp.status_code == 404
        assert "organization" in resp.json()["detail"]
        assert "lakehouse" in resp.json()["detail"]


# ------------------------------------------------------------------ embed

def test_embed_config_round_trips_through_the_api(client):
    r = client.get("/workspaces/retail/bi")
    assert r.status_code == 200
    assert r.json()["url"] is None

    r = client.put("/workspaces/retail/bi", json={"url": GOOD_URL})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == GOOD_URL

    back = client.get("/workspaces/retail/bi").json()
    assert back["url"] == GOOD_URL
    assert back["saved_at"] > 0

    # persisted as bi.json under the workspace data dir, the boards pattern
    f = Path(os.environ["IRONWRIGHT_DATA"]) / "retail" / "bi.json"
    assert f.exists()
    assert json.loads(f.read_text())["url"] == GOOD_URL


def test_embed_rejects_everything_that_is_not_absolute_http(client):
    bad_urls = (
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "file:///etc/passwd",
        "ftp://warehouse.example.com/dash",
        "/relative/dashboard",
        "dashboard",
        "",
        "https://",                      # scheme but no host
        "https://x.example.com/a b",     # whitespace inside
        'https://x.example.com/"quoted"',
        "https://x.example.com/" + "a" * 3000,  # over the length cap
    )
    for bad in bad_urls:
        r = client.put("/workspaces/retail/bi", json={"url": bad})
        assert r.status_code == 422, repr(bad)

    # rejected writes never clobber the saved config
    assert client.get("/workspaces/retail/bi").json()["url"] == GOOD_URL


def test_embed_rejection_is_loud_at_the_module_level(retail):
    from ironwright import bi

    with pytest.raises(bi.BIError, match="javascript"):
        bi.check_url("javascript:alert(1)")
    with pytest.raises(bi.BIError, match="http"):
        bi.check_url("data:text/html;base64,PHNjcmlwdD4=")


# -------------------------------------------------------------- packs gate

def test_nothing_in_this_module_wrote_under_packs(client, packs_before):
    assert _packs_tree() == packs_before
