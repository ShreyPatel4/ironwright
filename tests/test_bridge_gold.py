"""The gold contract: what a business says its marts must look like (docs/agentic-bridge.md)."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

from ironwright.bridge import gold

DOC = Path(__file__).resolve().parents[1] / "docs" / "agentic-bridge.md"


def _doc_example() -> dict:
    text = DOC.read_text()
    block = re.search(r"```yaml\n(kind: gold-contract.*?)```", text, re.DOTALL).group(1)
    return yaml.safe_load(block)


def _good() -> dict:
    """The doc's example with its two holes filled: the fact carries the join key and the
    masked column exists."""
    c = _doc_example()
    c["facts"][0]["columns"] += [{"name": "policy_no", "type": "string"},
                                 {"name": "adjuster_email", "type": "string"}]
    return c


def test_a_good_contract_validates_and_keeps_its_shape():
    got = gold.validate(_good())
    assert got["kind"] == "gold-contract" and got["version"] == 1
    assert [d["name"] for d in got["dimensions"]] == ["dim_policy"]
    assert got["marts"][0]["access"]["masking"] == {"adjuster_email": "hash"}
    assert got["dimensions"][0]["columns"][0] == {"name": "policy_no", "type": "string", "key": True}
    assert got["dimensions"][0]["columns"][1]["key"] is False


def test_the_doc_example_is_refused_for_masking_a_column_no_table_has():
    with pytest.raises(gold.GoldError, match="adjuster_email"):
        gold.validate(_doc_example())


def test_yaml_text_is_parsed_and_capped():
    text = yaml.safe_dump(_good(), sort_keys=False)
    assert gold.parse(text)["marts"][0]["name"] == "mart_claims_leakage"
    with pytest.raises(gold.GoldError, match="too large"):
        gold.parse("#" * (gold.MAX_BYTES + 1))
    with pytest.raises(gold.GoldError, match="YAML"):
        gold.parse("kind: [unclosed")
    with pytest.raises(gold.GoldError):
        gold.parse("- just a list")


def _broken(mutate) -> dict:
    c = copy.deepcopy(_good())
    mutate(c)
    return c


@pytest.mark.parametrize("mutate,words", [
    (lambda c: c.update(kind="workspace"), "kind"),
    (lambda c: c.update(version=2), "version"),
    (lambda c: c.update(extra=1), "extra"),
    (lambda c: c["marts"][0].update(coverage="covered"), "coverage"),
    (lambda c: c["marts"][0].update(**{"from": ["fct_nope"]}), "fct_nope"),
    (lambda c: c["marts"][0].update(use_case="nope"), "nope"),
    (lambda c: c["marts"][0]["access"].update(roles=["root"]), "root"),
    (lambda c: c["marts"][0]["access"].update(masking={"paid_amount": "rot13"}), "rot13"),
    (lambda c: c["marts"][0].update(rules=["paid_amount > ceiling"]), "ceiling"),
    (lambda c: c["marts"][0].update(rules=["paid_amount > 1; drop table x"]), "rule"),
    (lambda c: c["dimensions"][0]["columns"].append({"name": "policy_no", "type": "string"}), "twice"),
    (lambda c: c["facts"].append({"name": "dim_policy", "grain": "g", "columns": [{"name": "a", "type": "int"}]}), "twice"),
    (lambda c: c["dimensions"][0]["columns"][0].update(type="blob"), "blob"),
    (lambda c: c["dimensions"][0]["columns"][0].update(name="Policy No"), "Policy No"),
    (lambda c: c["dimensions"][0].update(columns=[]), "column"),
    (lambda c: c.update(marts=[]), "mart"),
    (lambda c: c["use_cases"][0]["consumer"].update(type="carrier pigeon"), "carrier pigeon"),
    (lambda c: c["dimensions"][0]["columns"][1].update(key=True), "key"),
    (lambda c: c["use_cases"].append(dict(c["use_cases"][0])), "twice"),
])
def test_a_bad_contract_is_refused_in_words(mutate, words):
    with pytest.raises(gold.GoldError, match=re.escape(words)):
        gold.validate(_broken(mutate))


def test_a_dimension_needs_exactly_one_key():
    with pytest.raises(gold.GoldError, match="key"):
        gold.validate(_broken(lambda c: c["dimensions"][0]["columns"][0].update(key=False)))


def test_dump_round_trips():
    got = gold.validate(_good())
    assert gold.validate(gold.parse(gold.dump(got))) == got
