"""The SQL surface under attack: /lake/query and board tiles.

Two callers run user SQL. Each row below must fail closed through both, and
the engine lockdown must hold on its own with the shape guard out of the way.
A failure tells the client a short category and an error id, never DuckDB's
text; the full text goes to the server log under the same id.
"""
import json
import logging
import os
import re
import time

import duckdb
import pytest
from fastapi.testclient import TestClient

from ironwright import sqlguard
from ironwright.lake import Lakehouse
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app

ERROR_ID = re.compile(r"\(error ([0-9a-f]{8})\)$")
# Anything DuckDB's own message would carry, or a path the attack named.
LEAKS = ("Error:", "LINE 1", "/etc", "/Users", "/tmp", "/private", "passwd", "root:", "duckdb")

CANARY = "/tmp/ironwright-sql-surface-canary"

# The shape guard stops these before any connection opens.
SHAPE_ATTACKS = {
    "copy": f"copy (select 1) to '{CANARY}.csv'",
    "copy_from": f"copy gold_item_store_perf from '{CANARY}.csv'",
    "with_copy": f"with x as (select 1) copy x to '{CANARY}.csv'",
    "with_insert": "with x as (select 1) insert into gold_item_store_perf select * from x",
    "with_update": "with x as (select 1) update gold_item_store_perf set store_nbr = 1",
    "with_delete": "with x as (select 1) delete from gold_item_store_perf",
    "attach": f"attach '{CANARY}.db' as evil",
    "install": "install httpfs",
    "load": "load httpfs",
    "pragma": "pragma version",
    "pragma_write": "pragma enable_profiling",
    "set": "set threads = 64",
    "reset": "reset memory_limit",
    "create": "create table evil as select 1",
    "drop": "drop view gold_item_store_perf",
    "comment_smuggle": "select 1 --;drop view gold_item_store_perf",
    "block_comment_smuggle": "select 1 /*;*/ ; drop view gold_item_store_perf",
    "two_statements": "select 1; select 2",
    "fullwidth_semicolon": "select 1； drop view gold_item_store_perf",
    "small_semicolon": "select 1﹔ drop view gold_item_store_perf",
    "greek_question_mark": "select 1; drop view gold_item_store_perf",
    "trailing_lookalike": "select 1;",
    "nul_byte": "select 1\x00; drop view gold_item_store_perf",
    "bare_nul": "select 1\x00",
    "does_not_parse": "select from from from",
    "empty": "   ",
}

# These look like SELECTs, so only the engine lockdown stands in the way.
ENGINE_ATTACKS = {
    "read_text": "select * from read_text('/etc/passwd')",
    "read_blob": "select * from read_blob('/etc/passwd')",
    "read_csv": "select * from read_csv('/etc/passwd')",
    "read_csv_auto": "select * from read_csv_auto('/etc/hosts')",
    "read_parquet": "select * from read_parquet('/etc/passwd')",
    "sniff_csv": "select * from sniff_csv('/etc/passwd')",
    "glob": "select * from glob('/etc/*')",
    "http": "select * from read_text('http://127.0.0.1:1/x')",
    "via_cte": "with x as (select * from read_text('/etc/passwd')) select * from x",
    "via_query": "select * from query('select * from read_text(''/etc/passwd'')')",
}

# Query() evaluates a string, so it must not be a way around the SELECT rule.
QUERY_FN_ATTACKS = {
    "query_attach": f"select * from query('attach ''{CANARY}.db'' as m')",
    "query_copy": f"select * from query('copy (select 1) to ''{CANARY}.csv''')",
    "query_set": "select * from query('set threads = 64')",
    "query_create": "select * from query('create table evil as select 1')",
    "query_bare_path": "select * from '/etc/passwd'",
}

# With the guard out of the way entirely, the connection alone must refuse.
UNGUARDED = {
    "copy": f"copy (select 1) to '{CANARY}.csv'",
    "attach": f"attach '{CANARY}.db' as evil",
    "install": "install httpfs",
    "load": "load httpfs",
    "set_external": "set enable_external_access = true",
    "set_memory": "set memory_limit = '100GB'",
    "set_threads": "set threads = 64",
    "read_text": "select * from read_text('/etc/passwd')",
    "glob": "select * from glob('/etc/*')",
}

# Small inputs and no buffering, so these burn CPU until the timer interrupts.
# The second produces no rows, so its work all happens on fetch, not execute.
CPU_BOMBS = {
    "count_cross_join": "select count(*) from range(100000) a, range(100000) b, range(1000000) c",
    "computes_on_fetch": "select a.range from range(100000) a, range(100000) b "
                         "where a.range + b.range < 0",
    "recursive": "with recursive r(n) as (select 1 union all select n + 1 from r) "
                 "select count(*) from r",
}


def ids(table):
    return {"argvalues": list(table.values()), "ids": list(table.keys())}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("sql-surface")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        with TestClient(create_platform_app()) as c:
            r = c.post("/workspaces/retail/runs",
                       json={"seed": 7, "stages": ["generate", "migrate"]})
            assert r.status_code == 200, r.text
            yield c
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


@pytest.fixture(autouse=True)
def no_canary_left_behind():
    """Whatever a test does, none of the write attacks may land on disk."""
    yield
    for suffix in (".csv", ".db"):
        assert not os.path.exists(CANARY + suffix), f"{CANARY}{suffix} was written"


def query(client, sql, **extra):
    return client.post("/workspaces/retail/lake/query", json={"sql": sql, **extra})


def tamper(slug, sql, viz="table"):
    """Write a tile straight to disk, past the save-time guard."""
    d = load_pack("retail").paths.root / "boards"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(json.dumps(
        {"title": "tampered", "tiles": [{"id": "t1", "title": "t", "sql": sql, "viz": viz}]}))


def run_tile(client, sql, slug="probe"):
    tamper(slug, sql)
    r = client.post(f"/workspaces/retail/boards/{slug}/run")
    assert r.status_code == 200, r.text
    return r.json()["tiles"][0]


def assert_safe(text):
    for leak in LEAKS:
        assert leak not in text, f"{leak!r} leaked to the client: {text}"


def assert_safe_with_id(text):
    assert_safe(text)
    assert ERROR_ID.search(text), f"no error id in: {text}"


# ----- shape attacks: rejected by the guard, through every door

@pytest.mark.parametrize("sql", **ids(SHAPE_ATTACKS))
def test_shape_attack_rejected_by_explorer(client, sql):
    r = query(client, sql)
    assert r.status_code == 422, r.text
    assert_safe(r.json()["detail"])


@pytest.mark.parametrize("sql", **ids(SHAPE_ATTACKS))
def test_shape_attack_rejected_at_board_save(client, sql):
    r = client.put("/workspaces/retail/boards/evil",
                   json={"title": "x", "tiles": [{"sql": sql}]})
    assert r.status_code == 422, r.text
    assert_safe(r.json()["detail"])


@pytest.mark.parametrize("sql", **ids(SHAPE_ATTACKS))
def test_shape_attack_rejected_at_board_run_when_file_was_tampered(client, sql):
    tile = run_tile(client, sql)
    assert tile["ok"] is False, tile
    assert "rows" not in tile
    assert_safe(tile["error"])


# ----- engine attacks: pass the shape check, die at the connection

@pytest.mark.parametrize("sql", **ids(ENGINE_ATTACKS))
def test_engine_attack_fails_closed_in_explorer(client, sql):
    r = query(client, sql)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "disabled" in detail
    assert_safe_with_id(detail)


@pytest.mark.parametrize("sql", **ids(ENGINE_ATTACKS))
def test_engine_attack_fails_closed_in_board_tile(client, sql):
    saved = client.put("/workspaces/retail/boards/looks-fine",
                       json={"title": "x", "tiles": [{"sql": sql}]})
    assert saved.status_code == 200, saved.text  # it is a SELECT, so it saves
    r = client.post("/workspaces/retail/boards/looks-fine/run").json()
    tile = r["tiles"][0]
    assert tile["ok"] is False, tile
    assert "rows" not in tile
    assert "disabled" in tile["error"]
    assert_safe_with_id(tile["error"])


@pytest.mark.parametrize("sql", **ids(QUERY_FN_ATTACKS))
def test_query_function_is_no_way_around_the_guard(client, sql):
    r = query(client, sql)
    assert r.status_code == 400, r.text
    assert_safe_with_id(r.json()["detail"])
    tile = run_tile(client, sql)
    assert tile["ok"] is False, tile
    assert_safe_with_id(tile["error"])


@pytest.mark.parametrize("sql", **ids(UNGUARDED))
@pytest.mark.parametrize("capped", [False, True], ids=["pipeline", "capped"])
def test_engine_lockdown_holds_with_no_guard_at_all(client, sql, capped):
    con = Lakehouse(load_pack("retail")).connect(capped=capped)
    try:
        with pytest.raises(duckdb.Error):
            con.execute(sql).fetchall()
    finally:
        con.close()


# ----- resource caps

def _capped_values():
    con = duckdb.connect()
    try:
        con.execute(f"set memory_limit = '{sqlguard.MEMORY_LIMIT}'")
        return con.execute("select current_setting('memory_limit')").fetchone()[0], \
            str(sqlguard.THREADS)
    finally:
        con.close()


SETTINGS = ("select current_setting('memory_limit'), current_setting('threads'), "
            "current_setting('temp_directory')")


def test_explorer_connection_carries_the_caps(client):
    memory, threads = _capped_values()
    r = query(client, SETTINGS)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row[:2] == [memory, threads]
    assert row[2] in (None, "")


def test_board_connection_carries_the_caps(client):
    memory, threads = _capped_values()
    tile = run_tile(client, SETTINGS)
    assert tile["ok"] is True, tile
    assert tile["rows"][0][:2] == [memory, threads]
    assert tile["rows"][0][2] in (None, "")


def test_pipeline_connection_is_not_capped(client):
    memory, _ = _capped_values()
    con = Lakehouse(load_pack("retail")).connect()
    try:
        assert con.execute("select current_setting('memory_limit')").fetchone()[0] != memory
    finally:
        con.close()


@pytest.mark.parametrize("sql", **ids(CPU_BOMBS))
def test_cpu_bomb_hits_the_timeout_in_explorer(client, sql, monkeypatch):
    monkeypatch.setattr(sqlguard, "TIMEOUT_SECONDS", 1)
    t0 = time.perf_counter()
    r = query(client, sql)
    assert time.perf_counter() - t0 < 6
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "time limit" in detail
    assert_safe_with_id(detail)


@pytest.mark.parametrize("sql", **ids(CPU_BOMBS))
def test_cpu_bomb_hits_the_timeout_in_board_tile(client, sql, monkeypatch):
    monkeypatch.setattr(sqlguard, "TIMEOUT_SECONDS", 1)
    t0 = time.perf_counter()
    tile = run_tile(client, sql)
    assert time.perf_counter() - t0 < 6
    assert tile["ok"] is False, tile
    assert "time limit" in tile["error"]
    assert_safe_with_id(tile["error"])


def test_a_timed_out_tile_does_not_poison_the_next_one(client, monkeypatch):
    monkeypatch.setattr(sqlguard, "TIMEOUT_SECONDS", 1)
    d = load_pack("retail").paths.root / "boards"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mixed.json").write_text(json.dumps({"title": "mixed", "tiles": [
        {"id": "bomb", "title": "b", "sql": CPU_BOMBS["count_cross_join"], "viz": "table"},
        {"id": "good", "title": "g", "sql": "select 42 as n", "viz": "number"},
    ]}))
    tiles = client.post("/workspaces/retail/boards/mixed/run").json()["tiles"]
    assert tiles[0]["ok"] is False
    assert tiles[1]["ok"] is True, tiles[1]
    assert tiles[1]["rows"] == [["42"]]


def test_memory_bomb_fails_as_an_error_not_a_dead_process(client):
    # The big side of a cross join is buffered, so this one meets the memory
    # limit long before the clock. With spill off it never touches the disk.
    sql = "select count(*) from range(100000000000) a, range(100000000000) b"
    r = query(client, sql)
    assert r.status_code == 400, r.text
    assert "memory limit" in r.json()["detail"]
    assert_safe_with_id(r.json()["detail"])
    assert query(client, "select 42 as n").json()["rows"] == [["42"]]


def test_row_cap_stays_500(client):
    body = query(client, "select * from range(10000)", limit=100000).json()
    assert len(body["rows"]) == 500
    assert body["truncated_at"] == 500


# ----- errors

@pytest.mark.parametrize("sql, category", [
    ("select nope from gold_item_store_perf", "unknown table, column or function"),
    ("select * from no_such_table", "unknown table, column or function"),
    ("select no_such_fn(1)", "unknown table, column or function"),
    ("select cast('abc' as integer)", "the query failed"),
])
def test_engine_errors_are_short_and_carry_an_id(client, sql, category):
    r = query(client, sql)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail.startswith(category)
    assert_safe_with_id(detail)
    tile = run_tile(client, sql)
    assert tile["ok"] is False
    assert tile["error"].startswith(category)
    assert_safe_with_id(tile["error"])


def test_full_error_text_is_logged_under_the_id_sent_to_the_client(client, caplog):
    with caplog.at_level(logging.ERROR, logger="ironwright.sql"):
        detail = query(client, "select * from read_text('/etc/passwd')").json()["detail"]
    error_id = ERROR_ID.search(detail).group(1)
    logged = [rec.getMessage() for rec in caplog.records if error_id in rec.getMessage()]
    assert logged, caplog.text
    assert "/etc/passwd" in logged[0]


def test_each_failure_gets_its_own_error_id(client):
    a = query(client, "select nope").json()["detail"]
    b = query(client, "select nope").json()["detail"]
    assert ERROR_ID.search(a).group(1) != ERROR_ID.search(b).group(1)


# ----- the guard keeps real questions working

@pytest.mark.parametrize("sql, expected", [
    ("select 1", "select 1"),
    ("  SELECT 1 ;  ", "SELECT 1"),
    ("select 1;;", "select 1"),
    ("with x as (select 1) select * from x", "with x as (select 1) select * from x"),
    ("select '--' as dashes", "select '--' as dashes"),
    ("select 1 -- a note", "select 1 -- a note"),
])
def test_guard_keeps_legitimate_selects(sql, expected):
    assert sqlguard.guard_select(sql) == expected


def test_both_callers_share_one_guard():
    from ironwright import boards
    with pytest.raises(sqlguard.SqlRejected):
        sqlguard.guard_select("drop table x")
    with pytest.raises(boards.BoardError) as e:
        boards.guard_sql("drop table x")
    assert isinstance(e.value, sqlguard.SqlRejected)
