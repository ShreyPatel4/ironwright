"""CDC rung contract tests. The live half proves postgres logical decoding
through the built-in test_decoding plugin against a real server named by
POSTGRES_URI (the shared fixtures skip honestly without one). The parser
half runs offline against change lines captured verbatim from a real
server, so the format is real even when the live tests skip. Every
assertion is against a planted value, never re-derived from the code
under test."""
import re
import uuid

import pyarrow as pa
import pytest

from ironwright import cdc
from ironwright.legacy import read_source

# ------------------------------------------------- offline: the line parser

# Captured verbatim from postgres 16 test_decoding output on this machine.
INSERT_LINE = ('table probe_e1b29b33.t: INSERT: id[integer]:1 '
               "\"unit price\"[text]:'it''s ''quoted''' note[text]:null "
               'qty[integer]:7')
KEYCHANGE_LINE = ('table probe_e1b29b33.t: UPDATE: old-key: id[integer]:1 '
                  "new-tuple: id[integer]:2 \"unit price\"[text]:'x' "
                  "note[text]:'hello world' qty[integer]:null")
DELETE_LINE = 'table probe_e1b29b33.t: DELETE: id[integer]:2'


def test_parse_insert_quoted_identifier_escaped_quotes_and_null():
    row = cdc.parse_change("0/1561A28", INSERT_LINE)
    assert row == {
        "_op": "insert",
        "_table": "probe_e1b29b33.t",
        "_lsn": "0/1561A28",
        "id": "1",
        "unit price": "it's 'quoted'",
        "note": None,
        "qty": "7",
    }


def test_parse_key_change_update_lands_the_new_tuple():
    row = cdc.parse_change("0/1561BD8", KEYCHANGE_LINE)
    assert row["_op"] == "update"
    assert row["id"] == "2"            # the new key, not the old one
    assert row["note"] == "hello world"
    assert row["qty"] is None


def test_parse_delete_carries_the_key_columns():
    row = cdc.parse_change("0/1561CB8", DELETE_LINE)
    assert row == {"_op": "delete", "_table": "probe_e1b29b33.t",
                   "_lsn": "0/1561CB8", "id": "2"}


@pytest.mark.parametrize("bad", [
    "nonsense that is not a change line",
    "table s.t: TRUNCATE: whatever",                 # op I do not claim
    "table s.t: INSERT: id[integer:1",               # unterminated type
    "table s.t: INSERT: id[integer]:'unterminated",  # unterminated value
    "table s.t: UPDATE: old-key: id[integer]:1",     # old-key, no new-tuple
])
def test_parse_failure_raises_with_the_offending_line(bad):
    with pytest.raises(ValueError) as exc:
        cdc.parse_change("0/0", bad)
    assert bad in str(exc.value)


def test_cdc_spec_without_the_named_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("IW_CDC_TEST_URI", raising=False)
    with pytest.raises(ValueError, match="IW_CDC_TEST_URI"):
        read_source({"kind": "cdc", "uri_env": "IW_CDC_TEST_URI",
                     "slot": "never_reached"}, tmp_path)


# ------------------------------------------------ live: a real postgres slot

@pytest.fixture()
def cdc_slot(postgres_uri):
    """A throwaway slot per test, created before the test writes anything
    and dropped afterwards even if the test already dropped it."""
    import psycopg

    slot = f"iw_cdc_{uuid.uuid4().hex[:10]}"
    cdc.create_slot(postgres_uri, slot)
    try:
        yield slot
    finally:
        try:
            cdc.drop_slot(postgres_uri, slot)
        except psycopg.errors.UndefinedObject:
            pass


def _exec(uri: str, *statements):
    import psycopg

    with psycopg.connect(uri, autocommit=True) as con:
        for stmt in statements:
            con.execute(stmt)


def _mine(rows, schema):
    """A slot sees every table in the database, so scope assertions to this
    test's throwaway schema. Keeps the suite honest under parallel runs."""
    return [r for r in rows if r["_table"].startswith(schema + ".")]


def test_insert_update_delete_captured_with_exact_values(postgres_uri, pg_schema, cdc_slot):
    _exec(postgres_uri,
          f"create table {pg_schema}.orders (order_id integer primary key, "
          "customer text, total numeric)",
          f"insert into {pg_schema}.orders values (7001, 'Mira Vale', 149.50)",
          f"update {pg_schema}.orders set total = 98.25 where order_id = 7001",
          f"delete from {pg_schema}.orders where order_id = 7001")
    t = cdc.capture(postgres_uri, cdc_slot)
    rows = _mine(t.to_pylist(), pg_schema)

    assert [r["_op"] for r in rows] == ["insert", "update", "delete"]
    assert all(r["_table"] == f"{pg_schema}.orders" for r in rows)

    ins, upd, dele = rows
    assert ins["order_id"] == "7001"
    assert ins["customer"] == "Mira Vale"
    assert ins["total"] == "149.50"
    assert upd["total"] == "98.25"
    assert upd["customer"] == "Mira Vale"
    # default replica identity: a delete carries the key, nothing else
    assert dele["order_id"] == "7001"
    assert dele["customer"] is None and dele["total"] is None

    # bronze contract: strings only, and every row has an lsn in order
    assert all(f.type == pa.string() for f in t.schema)
    lsns = [r["_lsn"] for r in rows]
    assert all(re.fullmatch(r"[0-9A-F]+/[0-9A-F]+", l) for l in lsns)
    assert lsns == sorted(lsns, key=lambda l: tuple(int(p, 16) for p in l.split("/")))


def test_capture_consumes_a_second_capture_sees_only_new_changes(postgres_uri, pg_schema, cdc_slot):
    _exec(postgres_uri,
          f"create table {pg_schema}.readings (id integer primary key, v text)",
          f"insert into {pg_schema}.readings values (1, 'first batch')")
    first = _mine(cdc.capture(postgres_uri, cdc_slot).to_pylist(), pg_schema)
    assert [r["v"] for r in first] == ["first batch"]

    _exec(postgres_uri, f"insert into {pg_schema}.readings values (2, 'second batch')")
    second = _mine(cdc.capture(postgres_uri, cdc_slot).to_pylist(), pg_schema)
    assert [r["v"] for r in second] == ["second batch"]
    assert [r["id"] for r in second] == ["2"]

    third = _mine(cdc.capture(postgres_uri, cdc_slot).to_pylist(), pg_schema)
    assert third == []


def test_null_quoted_values_and_quoted_identifiers_survive_capture(postgres_uri, pg_schema, cdc_slot):
    import psycopg

    with psycopg.connect(postgres_uri, autocommit=True) as con:
        con.execute(f'create table {pg_schema}.items '
                    '(id integer primary key, "unit price" text, note text)')
        con.execute(f"insert into {pg_schema}.items values (1, %s, null)",
                    ("it's 'quoted'",))
    rows = _mine(cdc.capture(postgres_uri, cdc_slot).to_pylist(), pg_schema)
    assert len(rows) == 1
    row = rows[0]
    assert row["unit price"] == "it's 'quoted'"   # identifier unquoted, value unescaped
    assert row["note"] is None
    assert row["id"] == "1"


def test_delete_without_replica_identity_lands_metadata_only(postgres_uri, pg_schema, cdc_slot):
    _exec(postgres_uri,
          f"create table {pg_schema}.nokey (v text)",  # no primary key
          f"insert into {pg_schema}.nokey values ('ghost')",
          f"delete from {pg_schema}.nokey")
    rows = _mine(cdc.capture(postgres_uri, cdc_slot).to_pylist(), pg_schema)
    assert [r["_op"] for r in rows] == ["insert", "delete"]
    dele = rows[1]
    assert dele["_table"] == f"{pg_schema}.nokey"
    assert dele["v"] is None  # (no-tuple-data): the key is not knowable


def test_dropped_slot_errors_loudly_on_capture(postgres_uri, cdc_slot):
    import psycopg

    cdc.drop_slot(postgres_uri, cdc_slot)
    with pytest.raises(psycopg.errors.UndefinedObject, match=cdc_slot):
        cdc.capture(postgres_uri, cdc_slot)


def test_connector_dispatch_lands_one_table_end_to_end(postgres_uri, pg_schema, cdc_slot,
                                                       tmp_path, monkeypatch):
    _exec(postgres_uri,
          f"create table {pg_schema}.orders (id integer primary key, sku text)",
          f"create table {pg_schema}.other (id integer primary key)",
          f"insert into {pg_schema}.orders values (1, 'SYN-SKU-001')",
          f"insert into {pg_schema}.other values (99)",
          f"insert into {pg_schema}.orders values (2, 'SYN-SKU-002')")
    # the uri reaches the connector through the env var the spec names
    monkeypatch.setenv("IW_CDC_TEST_URI", postgres_uri)
    t = read_source({"kind": "cdc", "uri_env": "IW_CDC_TEST_URI",
                     "slot": cdc_slot, "table": f"{pg_schema}.orders"}, tmp_path)
    rows = t.to_pylist()
    assert [(r["_op"], r["id"], r["sku"]) for r in rows] == \
        [("insert", "1", "SYN-SKU-001"), ("insert", "2", "SYN-SKU-002")]
    assert all(r["_table"] == f"{pg_schema}.orders" for r in rows)
    assert all(f.type == pa.string() for f in t.schema)
