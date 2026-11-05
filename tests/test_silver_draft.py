"""The silver drafter. Rules decide, a human unticks, and the SQL runs on real bronze.

Every test here runs the drafted SQL on the rows the run would land, because a draft
that only parses proves nothing. The numbers come from reading the fixture by hand:
claims.csv has 6 rows and 5 claims, policies.csv has 4 rows and 3 policies.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from ironwright.ingest import silver_draft as sd
from ironwright.ingest.profiler import profile_files
from ironwright.ingest.propose import propose
from ironwright.legacy import read_upload
from ironwright.sqlguard import guard_select

FIXTURES = Path(__file__).parent / "fixtures"
TENANT = FIXTURES / "tenant_ws" / "uploads" / "fixture"
MESSY = FIXTURES / "ingest" / "messy"
MAINFRAME = FIXTURES / "ingest" / "mainframe"
UPLOAD = "u" * 32


def build(*paths: Path) -> tuple[dict, dict]:
    """(workspace, draft) from real files, the way the profile job does it."""
    by_name = {p.name: p for p in paths}
    profile = profile_files([(p.name, p.read_bytes()) for p in paths])
    workspace = propose(profile, UPLOAD)["workspace"]

    def read(source: dict):
        return read_upload({**source, "kind": "upload", "upload_path": str(by_name[source["upload"].split("/")[1]])})

    return workspace, sd.draft(profile, workspace, read)


def text_build(tmp_path: Path, **files: str) -> tuple[dict, dict]:
    paths = []
    for stem, body in files.items():
        p = tmp_path / f"{stem}.csv"
        p.write_text(body)
        paths.append(p)
    return build(*paths)


def bronze(workspace: dict, name: str, folder: Path) -> duckdb.DuckDBPyConnection:
    src = next(s for s in workspace["sources"] if s["name"] == name)
    table = read_upload({**src, "kind": "upload", "upload_path": str(folder / src["upload"].split("/")[1])})
    con = duckdb.connect()
    con.register("_b", table)
    con.execute(f"create view bronze_{name} as select * from _b")
    return con


def run(workspace: dict, made: dict, name: str, folder: Path, *declined: str, renames: dict | None = None) -> list[dict]:
    spec = next(s for s in workspace["sources"] if s["name"] == name)
    source = next(s for s in made["sources"] if s["name"] == name)
    sql = sd.assemble(source, spec, set(declined), (renames or {}))
    guard_select(sql)
    cur = bronze(workspace, name, folder).execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def decision(made: dict, name: str, decision_id: str) -> dict:
    source = next(s for s in made["sources"] if s["name"] == name)
    return next(d for d in source["decisions"] if d["id"] == decision_id)


def ids(made: dict, name: str) -> set[str]:
    return {d["id"] for d in next(s for s in made["sources"] if s["name"] == name)["decisions"]}


@pytest.fixture(scope="module")
def tenant() -> tuple[dict, dict]:
    return build(TENANT / "claims.csv", TENANT / "policies.csv")


# ---------------------------------------------------------------- what comes out

def test_claims_come_out_clean(tenant):
    workspace, made = tenant
    rows = sorted(run(workspace, made, "claims", TENANT), key=lambda r: r["claim_id"])
    assert [r["claim_id"] for r in rows] == ["C-1001", "C-1002", "C-1003", "C-1004", "C-1005"]
    assert [r["policy_ref"] for r in rows] == ["P-100", "P-100", "P-200", "P-300", "P-999"]
    assert rows[0]["amount"] == Decimal("1200.50")
    assert rows[1]["amount"] == Decimal("300.00")
    assert rows[3]["amount"] is None
    assert rows[0]["loss_date"] == dt.date(2026, 1, 3)
    assert rows[1]["loss_date"] == dt.date(2026, 1, 5)


def test_engine_columns_and_the_overflow_never_reach_silver(tenant):
    workspace, made = tenant
    rows = run(workspace, made, "claims", TENANT)
    assert set(rows[0]) == {"claim_id", "policy_ref", "amount", "loss_date", "status"}


def test_policies_lose_their_exact_duplicate(tenant):
    workspace, made = tenant
    rows = run(workspace, made, "policies", TENANT)
    assert sorted(r["policy_no"] for r in rows) == ["P-100", "P-200", "P-300"]
    assert decision(made, "policies", "policies.drop_exact_duplicates")["rows"] == 1


# ---------------------------------------------------------------- the decision list

def test_every_decision_has_a_confidence_a_reason_and_the_rows_it_touches(tenant):
    _, made = tenant
    for source in made["sources"]:
        assert source["decisions"]
        for d in source["decisions"]:
            assert d["id"].startswith(f"{source['name']}.")
            assert 0 < d["confidence"] <= 1
            assert d["reason"] and "—" not in d["reason"]
            assert isinstance(d["rows"], int)
            assert d["of"] == source["rows_in"]


def test_the_counts_are_the_ones_in_the_file(tenant):
    _, made = tenant
    assert decision(made, "claims", "claims.normalize_key.policy_ref")["rows"] == 1  # 'p-100 '
    assert decision(made, "claims", "claims.collapse_on_key")["rows"] == 1  # C-1002 twice
    assert decision(made, "claims", "claims.drop_exact_duplicates")["rows"] == 0
    assert decision(made, "claims", "claims.cast.amount")["rows"] == 5
    assert decision(made, "claims", "claims.cast.amount")["lost"] == 0
    assert decision(made, "claims", "claims.cast.loss_date")["rows"] == 6


def test_the_date_decision_names_the_formats_it_saw(tenant):
    _, made = tenant
    d = decision(made, "claims", "claims.cast.loss_date")
    assert d["formats"] == ["%Y-%m-%d", "%m/%d/%Y"]


def test_the_key_comes_from_the_name_when_duplicates_hide_it_from_the_profile(tenant):
    _, made = tenant
    by_name = {s["name"]: s for s in made["sources"]}
    assert by_name["claims"]["key"] == "claim_id"
    assert by_name["policies"]["key"] == "policy_no"
    assert (by_name["claims"]["rows_in"], by_name["claims"]["rows_out"]) == (6, 5)
    assert (by_name["policies"]["rows_in"], by_name["policies"]["rows_out"]) == (4, 3)


def test_the_same_files_give_the_same_draft():
    a = build(TENANT / "claims.csv", TENANT / "policies.csv")[1]
    b = build(TENANT / "claims.csv", TENANT / "policies.csv")[1]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------- a human unticks

def test_unticking_the_collapse_keeps_both_rows(tenant):
    workspace, made = tenant
    rows = run(workspace, made, "claims", TENANT, "claims.collapse_on_key")
    assert [r["claim_id"] for r in rows].count("C-1002") == 2


def test_unticking_a_cast_leaves_the_text(tenant):
    workspace, made = tenant
    rows = run(workspace, made, "claims", TENANT, "claims.cast.amount")
    assert "$1,200.50" in [r["amount"] for r in rows]


def test_unticking_a_key_normalize_leaves_the_key_as_it_came(tenant):
    workspace, made = tenant
    rows = run(workspace, made, "claims", TENANT, "claims.normalize_key.policy_ref", "claims.collapse_on_key")
    assert "p-100 " in [r["policy_ref"] for r in rows]


def test_every_decision_can_be_unticked_and_the_sql_still_runs(tenant):
    workspace, made = tenant
    for name in ("claims", "policies"):
        for one in ids(made, name):
            assert run(workspace, made, name, TENANT, one)
        assert run(workspace, made, name, TENANT, *ids(made, name))


def test_an_unknown_decision_id_is_refused(tenant):
    workspace, made = tenant
    with pytest.raises(sd.DraftError):
        sd.models(made, workspace, ["claims.no_such_thing"], {})


# ---------------------------------------------------------------- the map step's edits

def test_a_rename_reaches_the_output_and_the_key_still_collapses(tenant):
    workspace, made = tenant
    rows = run(workspace, made, "claims", TENANT, renames={"claim_id": "claim_number"})
    assert sorted(r["claim_number"] for r in rows)[1] == "C-1002"
    assert len(rows) == 5
    assert "claim_id" not in rows[0]


@pytest.mark.parametrize("renames", [{"claim_id": "bad name"}, {"claim_id": 'x" from y; --'}, {"claim_id": "status"},
                                     {"no_such_column": "x"}])
def test_a_bad_rename_is_refused(tenant, renames):
    workspace, made = tenant
    source = next(s for s in made["sources"] if s["name"] == "claims")
    spec = next(s for s in workspace["sources"] if s["name"] == "claims")
    with pytest.raises(sd.DraftError):
        sd.assemble(source, spec, set(), renames)


def test_a_column_left_out_of_the_spec_is_left_out_of_silver(tenant):
    workspace, made = tenant
    spec = next(s for s in workspace["sources"] if s["name"] == "claims")
    fewer = {**spec, "columns": [c for c in spec["columns"] if c["name"] != "status"]}
    source = next(s for s in made["sources"] if s["name"] == "claims")
    cur = bronze(workspace, "claims", TENANT).execute(sd.assemble(source, fewer, set(), {}))
    assert "status" not in [d[0] for d in cur.description]


def test_a_type_chosen_in_the_map_step_wins(tenant):
    workspace, made = tenant
    spec = next(s for s in workspace["sources"] if s["name"] == "policies")
    changed = {**spec, "columns": [{**c, "type": "double"} if c["name"] == "premium" else c for c in spec["columns"]]}
    source = next(s for s in made["sources"] if s["name"] == "policies")
    rows = bronze(workspace, "policies", TENANT).execute(sd.assemble(source, changed, set(), {})).fetchall()
    assert all(isinstance(r[2], float) for r in rows)


def test_a_key_ticked_in_the_map_step_is_the_one_it_collapses_on(tmp_path):
    workspace, made = text_build(tmp_path, things="thing_id,serial,note\nT1,A,x\nT2,A,y\nT3,B,z\n")
    spec = workspace["sources"][0]
    marked = {**spec, "columns": [{**c, "key": c["name"] == "serial"} for c in spec["columns"]]}
    con = bronze(workspace, "things", tmp_path)
    rows = con.execute(sd.assemble(made["sources"][0], marked, set(), {})).fetchall()
    assert len(rows) == 2


# ---------------------------------------------------------------- the rules

def test_duplicates_keep_the_latest_by_a_date_column(tmp_path):
    workspace, made = text_build(tmp_path, items="item_id,updated_on,note\nA,2026-01-01,old\nA,2026-02-01,new\nB,2026-01-01,x\n")
    rows = {r["item_id"]: r["note"] for r in run(workspace, made, "items", tmp_path)}
    assert rows == {"A": "new", "B": "x"}
    assert "updated_on" in decision(made, "items", "items.collapse_on_key")["reason"]


def test_the_latest_sorts_on_the_cleaned_date_not_the_text(tmp_path):
    """As text, 12/31/2025 sorts after 2026-01-02."""
    workspace, made = text_build(tmp_path, items="item_id,updated_on,note\nA,12/31/2025,old\nA,2026-01-02,new\nB,2026-03-01,x\n")
    rows = {r["item_id"]: r["note"] for r in run(workspace, made, "items", tmp_path)}
    assert rows["A"] == "new"


def test_with_no_date_the_pick_does_not_depend_on_file_order(tmp_path):
    first, made = text_build(tmp_path, items="item_id,note\nA,one\nA,two\nB,x\n")
    a = {r["item_id"]: r["note"] for r in run(first, made, "items", tmp_path)}
    second, made2 = text_build(tmp_path, items="item_id,note\nB,x\nA,two\nA,one\n")
    b = {r["item_id"]: r["note"] for r in run(second, made2, "items", tmp_path)}
    assert a == b


def test_a_named_key_with_duplicates_beats_a_note_that_is_unique_by_luck(tmp_path):
    workspace, made = text_build(tmp_path, items="item_id,note\nA,one\nA,two\nB,three\n")
    assert made["sources"][0]["key"] == "item_id"
    assert sorted(r["item_id"] for r in run(workspace, made, "items", tmp_path)) == ["A", "B"]
    assert {r["note"] for r in run(workspace, made, "items", tmp_path)} <= {"one", "two", "three"}


def test_a_whole_number_key_with_duplicates_is_still_found(tmp_path):
    workspace, made = text_build(tmp_path, items="item_id,note\n1,x\n1,y\n2,z\n")
    assert made["sources"][0]["key"] == "item_id"
    assert sorted(r["item_id"] for r in run(workspace, made, "items", tmp_path)) == [1, 2]


def test_a_case_sensitive_key_is_trimmed_and_not_folded(tmp_path):
    workspace, made = text_build(tmp_path, tokens="token_id,note\naZ3f,x\nAz3F,y\n aZ3k,z\n")
    rows = run(workspace, made, "tokens", tmp_path)
    assert sorted(r["token_id"] for r in rows) == ["Az3F", "aZ3f", "aZ3k"]
    assert "tokens.normalize_key.token_id" not in ids(made, "tokens")
    assert decision(made, "tokens", "tokens.trim.token_id")["rows"] == 1


def test_a_key_named_for_another_table_does_not_collapse_this_one(tmp_path):
    workspace, made = text_build(tmp_path, lines="order_id,sku\n1,a\n1,b\n2,a\n")
    assert made["sources"][0]["key"] is None
    assert len(run(workspace, made, "lines", tmp_path)) == 3
    assert "lines.collapse_on_key" not in ids(made, "lines")


def test_a_name_that_mostly_repeats_is_no_key(tmp_path):
    body = "order_id,sku\n" + "".join(f"1,{c}\n" for c in "aabbccdd") + "2,z\n"
    workspace, made = text_build(tmp_path, orders=body)
    assert made["sources"][0]["key"] is None
    assert "no key" in made["sources"][0]["key_reason"]
    # Five different rows survive. Only the exact copies went.
    assert len(run(workspace, made, "orders", tmp_path)) == 5


def test_rows_with_no_key_are_dropped_and_counted(tmp_path):
    workspace, made = text_build(tmp_path, items="item_id,note\nA,x\nN/A,y\nB,z\nA,q\n")
    rows = run(workspace, made, "items", tmp_path)
    assert sorted(r["item_id"] for r in rows) == ["A", "B"]
    assert decision(made, "items", "items.drop_null_keys")["rows"] == 1


def test_null_tokens_become_null_with_a_count(tmp_path):
    workspace, made = text_build(tmp_path, items="item_id,note\nA,N/A\nB,null\nC,fine\n")
    rows = {r["item_id"]: r["note"] for r in run(workspace, made, "items", tmp_path)}
    assert rows == {"A": None, "B": None, "C": "fine"}
    assert decision(made, "items", "items.null_tokens.note")["rows"] == 2


def test_a_value_a_cast_cannot_read_is_counted_as_lost(tmp_path):
    body = "item_id,amount\n" + "".join(f"I{i},\"${i}.00\"\n" for i in range(1, 20)) + "I99,call me\n"
    workspace, made = text_build(tmp_path, items=body)
    d = decision(made, "items", "items.cast.amount")
    assert (d["rows"], d["lost"]) == (20, 1)
    rows = {r["item_id"]: r["amount"] for r in run(workspace, made, "items", tmp_path)}
    assert rows["I99"] is None
    assert rows["I5"] == Decimal("5.00")


def test_yes_and_no_cast_to_booleans(tmp_path):
    workspace, made = text_build(tmp_path, items="item_id,active\nA,Y\nB,N\n")
    rows = {r["item_id"]: r["active"] for r in run(workspace, made, "items", tmp_path)}
    assert rows == {"A": True, "B": False}


def test_a_one_format_date_that_a_plain_cast_cannot_read_still_parses(tmp_path):
    workspace, made = text_build(tmp_path, items="item_id,due_date\nA,01/25/2026\nB,02/26/2026\n")
    rows = {r["item_id"]: r["due_date"] for r in run(workspace, made, "items", tmp_path)}
    assert rows == {"A": dt.date(2026, 1, 25), "B": dt.date(2026, 2, 26)}


# ---------------------------------------------------------------- the other fixtures

def test_the_messy_fixture_gets_a_valid_draft_and_loses_its_trailer():
    workspace, made = build(MESSY / "claims_export.csv")
    rows = run(workspace, made, "claims_export", MESSY)
    assert len(rows) == 8
    assert all(r["claim_id"].startswith("CL-") for r in rows)
    assert all(isinstance(r["claim_date"], dt.date) for r in rows)
    assert all(isinstance(r["closed_at"], dt.datetime) or r["closed_at"] is None for r in rows)
    assert decision(made, "claims_export", "claims_export.drop_trailer")["rows"] == 1


def test_the_mainframe_fixture_gets_a_valid_draft_and_loses_its_trailer():
    workspace, made = build(MAINFRAME / "ITEMMAST.DAT")
    name = workspace["sources"][0]["name"]
    rows = run(workspace, made, name, MAINFRAME)
    assert len(rows) == 8
    assert decision(made, name, f"{name}.drop_trailer")["rows"] == 1
    assert "TRL" not in json.dumps(rows, default=str)


def test_the_join_fixture_gets_a_valid_draft():
    folder = FIXTURES / "ingest" / "join"
    workspace, made = build(folder / "customers.csv", folder / "orders.csv")
    assert run(workspace, made, "customers", folder)
    assert run(workspace, made, "orders", folder)


# ---------------------------------------------------------------- tenant bytes are untrusted

def test_no_cell_value_lands_in_the_draft(tmp_path):
    canary = "secret-canary-value"
    _, made = text_build(tmp_path, items=f"item_id,note\nA,{canary}\nB,{canary}2\nA,{canary}3\n")
    assert canary not in json.dumps(made)


def test_a_hostile_cell_is_data(tmp_path):
    hostile = "'); drop table x; --"
    workspace, made = text_build(tmp_path, items=f'item_id,note\nA,"{hostile}"\nB,ok\n')
    rows = {r["item_id"]: r["note"] for r in run(workspace, made, "items", tmp_path)}
    assert rows["A"] == hostile.strip()


@pytest.mark.parametrize("tamper", [
    {"formats": ["%Y'); drop table x; --"]},
    {"cast": "'); drop table x; --"},
])
def test_a_literal_outside_the_allow_list_never_reaches_sql(tenant, tamper):
    workspace, made = tenant
    source = json.loads(json.dumps(next(s for s in made["sources"] if s["name"] == "claims")))
    column = next(c for c in source["columns"] if c["name"] == "loss_date")
    column.update(tamper)
    spec = next(s for s in workspace["sources"] if s["name"] == "claims")
    with pytest.raises(sd.DraftError):
        sd.assemble(source, spec, set(), {})


def test_a_bad_type_never_reaches_sql(tenant):
    workspace, made = tenant
    spec = next(s for s in workspace["sources"] if s["name"] == "claims")
    bad = {**spec, "columns": [{"name": "amount", "type": "int); drop table x; --"}]}
    source = next(s for s in made["sources"] if s["name"] == "claims")
    with pytest.raises(sd.DraftError):
        sd.assemble(source, bad, set(), {})


def test_a_source_it_cannot_read_gets_an_error_and_the_others_still_draft(tenant):
    profile = profile_files([(p.name, p.read_bytes()) for p in (TENANT / "claims.csv", TENANT / "policies.csv")])
    workspace = propose(profile, UPLOAD)["workspace"]

    def read(source: dict):
        if source["name"] == "claims":
            raise sd.IngestError("source claims: the file has no data rows")
        return read_upload({**source, "kind": "upload", "upload_path": str(TENANT / "policies.csv")})

    made = sd.draft(profile, workspace, read)
    by_name = {s["name"]: s for s in made["sources"]}
    assert by_name["claims"]["error"]
    assert "decisions" not in by_name["claims"]
    assert by_name["policies"]["decisions"]
    assert list(sd.models(made, workspace, [], {})) == ["02_policies.sql"]
