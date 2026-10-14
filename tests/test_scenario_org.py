"""Scenario engine slice 1: spec validation, org generation, determinism.

These tests cover only the ORG layer for the regional_retail_chain archetype.
Systems estate, problem catalog, governance and migration waves come later.
"""
import copy
import json
from pathlib import Path

import pytest

from ironwright.scenario import (
    ARCHETYPES,
    ScenarioSpec,
    SpecError,
    generate_org,
    load_spec,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "ironwright" / "scenario" / "examples" / "regional_retail.yaml"

GOOD = {"name": "harborview-pilot", "archetype": "regional_retail_chain", "size": 16, "seed": 7}


def spec_from(**over) -> ScenarioSpec:
    d = {**GOOD, **over}
    return ScenarioSpec.from_dict(d)


# ---------------------------------------------------------------- spec schema

def test_example_file_loads():
    spec = load_spec(EXAMPLE)
    assert spec.archetype == "regional_retail_chain"
    assert spec.size == 16
    assert isinstance(spec.seed, int)
    assert spec.name


def test_archetype_registry_has_exactly_the_shipped_one():
    assert set(ARCHETYPES) == {"regional_retail_chain"}


def test_unknown_key_fails_loud():
    d = dict(GOOD, stores=16)
    with pytest.raises(SpecError, match="stores"):
        ScenarioSpec.from_dict(d)


def test_missing_key_fails_loud():
    for key in GOOD:
        d = {k: v for k, v in GOOD.items() if k != key}
        with pytest.raises(SpecError, match=key):
            ScenarioSpec.from_dict(d)


def test_not_a_mapping_fails():
    with pytest.raises(SpecError):
        ScenarioSpec.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_unknown_archetype_fails():
    with pytest.raises(SpecError, match="archetype"):
        spec_from(archetype="orbital_bakery")


@pytest.mark.parametrize("bad", [0, 3, 65, -1, "16", 16.0, True, None])
def test_bad_size_fails(bad):
    with pytest.raises(SpecError, match="size"):
        spec_from(size=bad)


@pytest.mark.parametrize("bad", [-1, "7", 7.5, True, None, 2**63])
def test_bad_seed_fails(bad):
    with pytest.raises(SpecError, match="seed"):
        spec_from(seed=bad)


@pytest.mark.parametrize("bad", ["", "   ", 7, None, "a" * 65, "bad/name"])
def test_bad_name_fails(bad):
    with pytest.raises(SpecError, match="name"):
        spec_from(name=bad)


def test_spec_yaml_must_be_a_mapping(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(SpecError):
        load_spec(p)


# ---------------------------------------------------------------- determinism

def dumps(org) -> str:
    return json.dumps(org.to_dict(), sort_keys=True)


def test_same_spec_same_seed_byte_identical():
    spec = load_spec(EXAMPLE)
    a = dumps(generate_org(spec)).encode("utf-8")
    b = dumps(generate_org(spec)).encode("utf-8")
    assert a == b


def test_generate_never_touches_global_random():
    import random as global_random

    spec = spec_from(seed=11)
    state_before = global_random.getstate()
    generate_org(spec)
    assert global_random.getstate() == state_before


def test_different_seeds_differ_but_stay_valid():
    orgs = [generate_org(spec_from(seed=s)) for s in (1, 2, 3)]
    blobs = {dumps(o) for o in orgs}
    assert len(blobs) == 3
    for org in orgs:
        assert_org_invariants(org, size=16)


def test_spec_identity_is_recorded():
    org = generate_org(spec_from(seed=7))
    d = org.to_dict()
    assert d["scenario"] == "harborview-pilot"
    assert d["archetype"] == "regional_retail_chain"
    assert d["seed"] == 7


def test_to_dict_is_plain_json_data():
    org = generate_org(spec_from(seed=5))
    d = org.to_dict()
    assert d == json.loads(json.dumps(d))
    assert d == copy.deepcopy(d)


# ---------------------------------------------------- structural invariants

def assert_org_invariants(org, size: int):
    d = org.to_dict()
    roles = {r["id"]: r for r in d["roles"]}
    assert len(roles) == len(d["roles"]), "role ids must be unique"

    # one root (the CEO), every other role reports to an existing role
    roots = [r for r in d["roles"] if r["reports_to"] is None]
    assert len(roots) == 1
    assert roots[0]["id"] == "ceo"
    for r in d["roles"]:
        if r["reports_to"] is not None:
            assert r["reports_to"] in roles, f"{r['id']} reports to a ghost"

    # the hierarchy reaches the CEO from every role (no cycles, no islands)
    for r in d["roles"]:
        seen, cur = set(), r
        while cur["reports_to"] is not None:
            assert cur["id"] not in seen, f"cycle at {cur['id']}"
            seen.add(cur["id"])
            cur = roles[cur["reports_to"]]
        assert cur["id"] == "ceo"

    # every department has an existing owner, every role sits in a department
    depts = {dep["id"]: dep for dep in d["departments"]}
    for dep in d["departments"]:
        assert dep["owner_role"] in roles, f"department {dep['id']} owner missing"
    for r in d["roles"]:
        assert r["department"] in depts, f"role {r['id']} has no department"

    # every data object has exactly one owner and the owner exists
    assert d["data_owners"], "org must assign data ownership"
    for obj, owner in d["data_owners"].items():
        assert owner in roles, f"{obj} owned by ghost {owner}"
    assert d["data_owners"]["customer"] == "cdo"

    # approver chains are non-empty and every link is an existing role
    assert d["approver_chains"], "org must have approver chains"
    for ch in d["approver_chains"]:
        assert ch["chain"], f"empty chain for {ch['action']}"
        for role_id in ch["chain"]:
            assert role_id in roles, f"chain {ch['action']} names ghost {role_id}"
    actions = {ch["action"] for ch in d["approver_chains"]}
    assert "propose_facing_change" in actions

    # a role's approves list mirrors the chains it appears in
    for ch in d["approver_chains"]:
        for role_id in ch["chain"]:
            assert ch["action"] in roles[role_id]["approves"]

    # size means stores: size stores, each with its own manager role in a real region
    assert len(d["stores"]) == size
    regions = {rg["code"] for rg in d["regions"]}
    assert 2 <= len(regions) <= 4
    manager_ids = set()
    for st in d["stores"]:
        assert st["region"] in regions
        assert st["manager_role"] in roles
        manager_ids.add(st["manager_role"])
    assert len(manager_ids) == size, "each store gets its own manager role"
    # every region is actually used and has a regional manager role
    assert {st["region"] for st in d["stores"]} == regions
    for rg in d["regions"]:
        assert rg["manager_role"] in roles

    # store numbers and names are unique
    assert len({st["number"] for st in d["stores"]}) == size
    assert len({st["name"] for st in d["stores"]}) == size


@pytest.mark.parametrize("seed", [0, 7, 42, 1234, 2**62])
def test_invariants_hold_across_seeds(seed):
    assert_org_invariants(generate_org(spec_from(seed=seed)), size=16)


@pytest.mark.parametrize("size", [4, 9, 16, 33, 64])
def test_size_parameter_honored(size):
    assert_org_invariants(generate_org(spec_from(size=size, seed=7)), size=size)


def test_company_name_is_synthetic_and_seed_dependent():
    names = {generate_org(spec_from(seed=s)).company for s in range(8)}
    assert len(names) > 1, "company name should vary with the seed"
    for n in names:
        assert n and n == n.strip()
