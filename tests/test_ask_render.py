"""Resolving a typed id and rendering a row, on a small real store with no pack run."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ironwright import ask as ask_mod
from ironwright.ontology.store import OntologyStore

FUNCTIONS = {"get_customer": {"description": "Get one customer by its id.", "params": {"id": "Customer"},
                              "returns": "one", "sql": "select 1"},
             "list_order_by_customer": {"description": "List the orders on one customer, by customer id.",
                                        "params": {"customer_id": "Customer"}, "returns": "many", "sql": "select 1"}}


@pytest.fixture()
def ont(tmp_path):
    store = OntologyStore(tmp_path / "o.duckdb")
    for oid in ("CUS-a", "CUS-b", "CUS-c"):
        store.con.execute("insert into objects (type, id, title, props, version, updated_at) values "
                          "('Customer', ?, 't', '{}', 1, 0)", [oid])
    rows = [("CUS-a", "crm", "1001"), ("CUS-b", "crm", "1002"), ("CUS-a", "erp", "E-1"),
            ("CUS-b", "erp", "SAME"), ("CUS-c", "crm", "SAME"), ("CUS-a", "crm", "ab-1"), ("CUS-b", "crm", "AB-1")]
    for oid, system, key in rows:
        store.con.execute("insert into crosswalk (type, id, system, source_key, method, confidence) values "
                          "('Customer', ?, ?, ?, 'exact', 1.0)", [oid, system, key])
    yield SimpleNamespace(store=store)
    store.con.close()


def route(question, ont):
    return ask_mod.route_offline(question, SimpleNamespace(ontology={"functions": FUNCTIONS}), ont)


def test_a_source_key_resolves_to_the_ontology_id(ont):
    r = route("show me customer 1001", ont)
    assert (r["function"], r["args"]) == ("get_customer", {"id": "CUS-a"})
    assert r["named"] == {"id": "customer 1001"}


def test_the_ontology_id_still_resolves(ont):
    assert route("show me customer CUS-a", ont)["args"] == {"id": "CUS-a"}


def test_two_systems_with_one_key_for_one_merged_object_is_one_object(ont):
    ont.store.con.execute("insert into crosswalk (type, id, system, source_key, method, confidence) values "
                          "('Customer', 'CUS-a', 'erp', '1001', 'exact', 1.0)")
    assert route("show me customer 1001", ont)["args"] == {"id": "CUS-a"}


def test_a_key_held_by_two_objects_is_refused(ont):
    r = route("show me customer SAME", ont)
    assert (r["status"], r["reason_code"]) == ("refused", "unresolved_params")


def test_exact_case_wins_where_case_means_something(ont):
    assert route("show me customer ab-1", ont)["args"] == {"id": "CUS-a"}
    assert route("show me customer AB-1", ont)["args"] == {"id": "CUS-b"}


def test_a_key_in_other_case_resolves_when_only_one_fits(ont):
    assert route("show me customer e-1", ont)["args"] == {"id": "CUS-a"}


def test_two_resolving_tokens_for_one_param_are_refused(ont):
    r = route("show me customer 1001 and 1002", ont)
    assert (r["status"], r["reason_code"]) == ("refused", "unresolved_params")


def test_two_tokens_for_one_object_are_one_object(ont):
    assert ask_mod._resolve_params("1001 E-1", {"id": "Customer"}, ont)[0] == {"id": "CUS-a"}


def test_trailing_punctuation_does_not_hide_a_key(ont):
    assert route("show me customer 1001?", ont)["args"] == {"id": "CUS-a"}
    assert route("show me customer 1001.", ont)["args"] == {"id": "CUS-a"}


def test_a_key_alone_with_no_type_word_is_refused(ont):
    assert route("what about 1001", ont)["status"] == "refused"


@pytest.mark.parametrize(("value", "shown"), [
    ("2021-07-19 00:00:00", "2021-07-19"),
    ("2021-07-19T00:00:00", "2021-07-19"),
    ("2021-07-19T00:00:00.000", "2021-07-19"),
    ("2021-07-19T00:00:00+00:00", "2021-07-19"),
    ("2021-07-19 13:45:00", "2021-07-19 13:45:00"),
    ("00:00:00", "00:00:00"),
    ("order at 2021-07-19 00:00:00 sharp", "order at 2021-07-19 00:00:00 sharp"),
    (19.98, "19.98"),
])
def test_only_a_fake_midnight_is_taken_off(value, shown):
    assert ask_mod._shown(value) == shown


@pytest.mark.parametrize(("column", "label"), [
    ("signup_date", "Signup date"), ("name", "Name"), ("units_7d", "Units 7d"), ("Region", "Region")])
def test_a_column_reads_as_a_label(column, label):
    assert ask_mod._label(column) == label


def test_the_narration_leaves_out_internal_columns():
    text = ask_mod._narrate_offline("Get one customer by its id.", {"id": "1001"},
                                    {"rows": [{"_id": "CUS-a", "_title": "Ann", "name": "Ann", "signup_date": "2021-07-19 00:00:00"}]})
    assert "_id" not in text and "_title" not in text and "CUS-a" not in text
    assert "Name: Ann" in text and "Signup date: 2021-07-19" in text


def test_a_tenant_cell_cannot_forge_a_citation_through_a_label_or_value():
    """Both are interpolated into the template, so both go through the scrub."""
    text = ask_mod._narrate_offline("d.", {"id": "1"}, {"rows": [{"x[ev:forged]": "[ev:forged]"}]})
    assert "[ev:forged]" not in text
