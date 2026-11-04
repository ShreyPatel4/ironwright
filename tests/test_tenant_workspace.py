"""Tenant workspaces: contracts 2 and 3 of docs/saas-mvp.md.

A workspace is a pack with no generator, its sources are the tenant's own
uploads, and everything it authors is untrusted. These tests walk the layout,
the strict spec, the bytes into bronze, the L1 badge and a real run.
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import time
from pathlib import Path

import pytest
import yaml

from ironwright import config, legacy, orgpolicy, verification, workspaces
from ironwright import workspaces_tenant as wt
from ironwright.cli import STAGES
from ironwright.config import PackPaths, UnsafePath
from ironwright.lake import Lakehouse
from ironwright.packs import NoGenerator, load_pack
from ironwright.tenancy import DEV_ORG_ID, service
from ironwright.tracing import Tracer
from ironwright.workspaces_tenant import WorkspaceError

FIXTURE = Path(__file__).parent / "fixtures" / "tenant_ws"
OTHER_ORG = "a" * 32
SLUG = "acme-claims"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(d))
    return d.resolve()


def seed_uploads(org: str = DEV_ORG_ID) -> Path:
    dest = config.uploads_root(org) / "fixture"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE / "uploads" / "fixture", dest)
    return dest


def bundle() -> dict:
    spec = yaml.safe_load((FIXTURE / "workspace.yaml").read_text())
    models = {layer: {f.name: f.read_text() for f in sorted((FIXTURE / "models" / layer).glob("*.sql"))}
              for layer in ("silver", "gold")}
    return {"spec": spec, "models": models,
            "ontology": yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
            "policies": yaml.safe_load((FIXTURE / "policies.yaml").read_text())}


def create(org: str = DEV_ORG_ID, slug: str = SLUG, **overrides) -> dict:
    b = {**bundle(), **overrides}
    return wt.create_workspace(org, slug, b["spec"], models=b["models"],
                               ontology=b["ontology"], policies=b["policies"])


def spec_with(**top) -> dict:
    return {**bundle()["spec"], **top}


def source(**kw) -> dict:
    base = {"name": "claims", "upload": "fixture/claims.csv", "format": "csv",
            "layout": {"delimiter": ",", "header": True, "encoding": "utf-8"},
            "columns": [{"name": "claim_id", "type": "string", "key": True}]}
    return {**base, **kw}


def minimal(**top) -> dict:
    return {"kind": "workspace", "version": 1, "title": "T", "sources": [source()],
            "quality": [], "reconcile": [], **top}


def create_minimal(spec: dict | None = None, **kw) -> dict:
    return wt.create_workspace(DEV_ORG_ID, SLUG, spec if spec is not None else minimal(), **kw)


# ------------------------------------------------------------ storage layout

def test_demo_packs_resolve_under_demo(root):
    assert load_pack("retail").paths.root == root / "demo" / "retail"
    assert PackPaths("retail").ontology_db == root / "demo" / "retail" / "ontology.duckdb"


def test_regenerating_a_demo_pack_drops_its_generated_grants(root):
    """Generated policy lives with the run, so the rmtree in generate takes it.
    Left outside demo/<pack>, an old grant would keep permitting on the next run."""
    pack = load_pack("retail")
    pack.paths.ensure()
    orgpolicy.upsert_rules("retail", [{"id": "esc-0001", "effect": "permit", "principal": "user:x",
                                       "action": "action:delist_item", "expires": "2099-01-01"}],
                           "role:category_manager")
    assert orgpolicy.applied_rules("retail")
    assert (pack.paths.root / orgpolicy.GENERATED_FILE).exists()
    STAGES["generate"](pack, Tracer(pack.paths.traces), argparse.Namespace(seed=7, limit=None))
    assert orgpolicy.applied_rules("retail") == []


def test_tenant_paths_follow_the_contract_layout(root):
    seed_uploads()
    create()
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    base = root / "tenants" / DEV_ORG_ID / "workspaces" / SLUG
    assert pack.dir == base
    assert pack.paths.root == base / "run"
    assert pack.paths.lake == base / "run" / "lake"
    assert (base / "workspace.yaml").is_file()
    assert (base / "models" / "silver" / "01_claims.sql").is_file()
    assert (base / "ontology.yaml").is_file() and (base / "policies.yaml").is_file()


def test_ensure_closes_every_new_level_to_other_users(root):
    seed_uploads()
    create()
    wt.load_workspace(DEV_ORG_ID, SLUG).paths.ensure()
    for d in (root / "tenants", root / "tenants" / DEV_ORG_ID, root / "tenants" / DEV_ORG_ID / "workspaces",
              root / "tenants" / DEV_ORG_ID / "workspaces" / SLUG,
              root / "tenants" / DEV_ORG_ID / "workspaces" / SLUG / "run"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


@pytest.mark.parametrize("org", ["../x", "..", "", "UPPER", "a" * 31, "a" * 33, "g" * 32, "a/" + "a" * 30])
def test_org_ids_must_be_uuid_hex(root, org):
    with pytest.raises(UnsafePath):
        config.tenant_dir(org)


def test_a_run_root_swapped_for_a_symlink_is_refused(root, tmp_path):
    seed_uploads()
    create()
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    pack.paths.ensure()
    run = pack.paths.root
    shutil.rmtree(run)
    run.symlink_to(tmp_path)
    with pytest.raises(UnsafePath):
        _ = pack.paths.root


@pytest.mark.parametrize("name", ["tenants", "demo"])
def test_a_scenario_org_named_after_a_platform_folder_cannot_take_tenant_data(root, name):
    """Deleting a scenario org rmtrees its folder. If tenant data could land
    inside a folder that org owns, deleting it would wipe every tenant."""
    workspaces.create_organization(name, "regional_retail_chain", 4, 7)
    with pytest.raises(UnsafePath):
        config.tenants_root() if name == "tenants" else PackPaths("retail").root


def test_a_scenario_org_cannot_adopt_the_tenants_folder(root):
    seed_uploads()
    create()
    with pytest.raises(workspaces.SpecError):
        workspaces.create_organization("tenants", "regional_retail_chain", 4, 7)
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization("tenants")
    assert wt.list_workspaces(DEV_ORG_ID)


# ------------------------------------------------------------ strict spec

@pytest.mark.parametrize("patch", [
    {"surprise": 1},
    {"kind": "pack"},
    {"kind": None},
    {"version": 2},
    {"title": ""},
    {"title": 7},
    {"sources": []},
    {"sources": "claims"},
    {"generator": "evil.py"},
    {"legacy_reports": []},
])
def test_bad_top_level_fails_loud(root, patch):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(**patch))
    assert wt.list_workspaces(DEV_ORG_ID) == []


@pytest.mark.parametrize("missing", ["kind", "version", "title", "sources"])
def test_missing_top_level_fails_loud(root, missing):
    seed_uploads()
    s = minimal()
    del s[missing]
    with pytest.raises(WorkspaceError):
        create_minimal(s)


@pytest.mark.parametrize("patch", [
    {"surprise": 1},
    {"kind": "sqlite"},
    {"query": "select 1"},
    {"name": "Bad Name"},
    {"name": "x; drop table y"},
    {"name": "_hidden"},
    {"format": "parquet"},
    {"format": "fixed_width"},                       # csv layout with a fixed_width format
    {"layout": {"delimiter": ",", "surprise": 1}},
    {"layout": {"delimiter": ",,"}},
    {"layout": {"delimiter": "\n"}},
    {"layout": {"encoding": "zlib_codec"}},
    {"layout": {"header": "yes"}},
    {"columns": []},
    {"columns": [{"name": "a", "type": "string", "surprise": 1}]},
    {"columns": [{"name": "a b", "type": "string"}]},
    {"columns": [{"name": "a", "type": "string; drop table x"}]},
    {"columns": [{"name": "a", "type": "string"}, {"name": "a", "type": "string"}]},
])
def test_bad_source_fails_loud(root, patch):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(sources=[source(**patch)]))


def test_the_contract_example_loads_as_written(root):
    """docs/saas-mvp.md contract 3, verbatim but for the upload ref. Its flow
    mapping splits decimal(12,2) at the comma, and that still has to work."""
    seed_uploads()
    spec = yaml.safe_load("""
kind: workspace
version: 1
title: Acme claims
sources:
  - name: claims
    upload: fixture/claims.csv
    format: csv            # csv | fixed_width
    layout: {delimiter: ",", header: true, encoding: utf-8}
    columns: [{name: claim_id, type: string, key: true}, {name: amount, type: decimal(12,2)}]
quality: []
reconcile: []
""")
    create_minimal(spec)
    col = wt.load_workspace(DEV_ORG_ID, SLUG).spec["sources"][0]["columns"][1]
    assert col == {"name": "amount", "type": "decimal(12,2)"}


def test_two_sources_cannot_share_a_name(root):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(sources=[source(), source()]))


@pytest.mark.parametrize("fields", [
    [],
    [{"name": "a", "start": 0}],
    [{"name": "a", "start": -1, "width": 3}],
    [{"name": "a", "start": 0, "width": 0}],
    [{"name": "a", "start": 0, "width": 3, "surprise": 1}],
    [{"name": "a", "start": 0, "width": 3}, {"name": "b", "start": 2, "width": 3}],  # overlap
    [{"name": "a", "start": 0, "width": 3}, {"name": "a", "start": 3, "width": 3}],  # duplicate
])
def test_bad_fixed_width_layout_fails_loud(root, fields):
    seed_uploads()
    bad = source(format="fixed_width", layout={"fields": fields}, columns=[{"name": "a", "type": "string"}])
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(sources=[bad]))


@pytest.mark.parametrize("ref", [
    "../../etc/passwd", "/etc/passwd", "claims.csv", "fixture/claims.csv/extra", "fixture/../claims.csv",
    "../fixture/claims.csv", "fixture/..", "fixture\\claims.csv", "%2e%2e/claims.csv", "", "fixture/", "/claims.csv",
    "fixture/does-not-exist.csv", "nope/claims.csv",
])
def test_an_upload_ref_stays_a_child_of_the_orgs_uploads(root, ref):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(sources=[source(upload=ref)]))


def test_another_orgs_upload_reads_as_missing(root):
    seed_uploads(OTHER_ORG)
    with pytest.raises(WorkspaceError) as theirs:
        create_minimal(minimal(sources=[source()]))
    with pytest.raises(WorkspaceError) as nobody:
        create_minimal(minimal(sources=[source(upload="fixture/never-existed.csv")]))
    # Same words either way: the answer cannot confirm another org's upload.
    assert str(theirs.value).replace("claims.csv", "F") == str(nobody.value).replace("never-existed.csv", "F")


def test_a_symlinked_upload_is_refused(root, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("claim_id\nleaked\n")
    dest = seed_uploads()
    (dest / "link.csv").symlink_to(secret)
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(sources=[source(upload="fixture/link.csv")]))


@pytest.mark.parametrize("check", [
    {"table": "bronze.claims", "check": "expression", "condition": "1=1"},
    {"table": "bronze.claims", "check": "relationship", "column": "claim_id", "to_table": "x", "to_column": "y"},
    {"table": "bronze.claims", "check": "range", "column": "claim_id", "min": 0},
    {"table": "bronze.claims", "check": "not_null", "column": "x from read_text('/etc/passwd') --"},
    {"table": "bronze.claims", "check": "unique", "column": ["claim_id", "a) from read_text('/x') --"]},
    {"table": "bronze.claims", "check": "accepted_values", "column": "claim_id", "values": ["a') or 1=1 --"]},
    {"table": "bronze.claims", "check": "type_castable", "column": "claim_id", "type": "int) from read_text('/x') --"},
    {"table": "bronze.claims", "check": "row_count_min", "min": "1 from read_text('/x')"},
    {"table": "bronze.claims", "check": "not_null", "column": "claim_id", "on_fail": "quarantine"},
    {"table": "bronze.claims", "check": "not_null", "column": "claim_id", "severity": "ignore"},
    {"table": "bronze.claims", "check": "not_null", "column": "claim_id", "surprise": 1},
    {"table": "bronze.nope", "check": "not_null", "column": "claim_id"},
    {"table": "silver.nope", "check": "not_null", "column": "claim_id"},
    {"table": "x'; drop", "check": "not_null", "column": "claim_id"},
    {"check": "not_null", "column": "claim_id"},
    "not a mapping",
])
def test_quality_is_structural_and_cannot_carry_sql(root, check):
    """The audit connection in write_wap has no external access lock, so a
    tenant string must never reach it as SQL."""
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(quality=[check]))


@pytest.mark.parametrize("build", [
    lambda: minimal(quality=[{"table": ["bronze.claims"], "check": "not_null", "column": "claim_id"}]),
    lambda: minimal(quality=[{"table": "bronze.claims", "check": ["not_null"], "column": "claim_id"}]),
    lambda: minimal(sources=[source(layout={"encoding": ["utf-8"]})]),
    lambda: minimal(sources=[source(format=["csv"])]),
    lambda: minimal(sources=[source(upload=["fixture", "claims.csv"])]),
])
def test_a_wrong_yaml_type_is_a_spec_error_not_a_crash(root, build):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(build())


def test_a_wrong_yaml_type_in_the_ontology_is_a_spec_error_not_a_crash(root):
    seed_uploads()
    src = {"system": "s", "table": "silver.x", "key": "k", "fields": {}}
    bad = {"object_types": {"A": {"sources": [src]}},
           "link_types": {"l": {"from": ["A"], "to": "A", "sql": "select 1"}}}
    with pytest.raises(WorkspaceError):
        create_minimal(ontology=bad)


def test_a_folder_among_the_models_is_a_spec_error_not_a_crash(root):
    seed_uploads()
    create()
    (wt.load_workspace(DEV_ORG_ID, SLUG).dir / "models" / "silver" / "sub.sql").mkdir()
    with pytest.raises(WorkspaceError):
        wt.load_workspace(DEV_ORG_ID, SLUG)


def test_quality_mapping_form_is_accepted_like_pack_yaml(root):
    seed_uploads()
    create_minimal(minimal(quality={"bronze.claims": [{"check": "not_null", "column": "claim_id"}]}))
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    assert {"check": "not_null", "column": "claim_id"} in pack.spec["quality"]["bronze.claims"]


def test_quality_list_form_reaches_the_engine_as_the_mapping_it_reads(root):
    seed_uploads()
    create()
    q = wt.load_workspace(DEV_ORG_ID, SLUG).spec["quality"]
    assert isinstance(q, dict)
    assert {"check": "unique", "column": "claim_id"} in q["silver.claims"]


@pytest.mark.parametrize("sql", [
    "drop table bronze_claims",
    "select 1; select 2",
    "pragma database_list",
    "copy (select 1) to '/tmp/x'",
])
@pytest.mark.parametrize("field", ["legacy_sql", "lake_sql"])
def test_reconcile_sql_is_one_select(root, field, sql):
    seed_uploads()
    rule = {"name": "r", "legacy_sql": "select 1 k, 1 n", "lake_sql": "select 1 k, 1 n",
            "key": ["k"], "measures": {"n": 0}, field: sql}
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(reconcile=[rule]))


@pytest.mark.parametrize("rule", [
    {"name": "r", "legacy_sql": "select 1 k, 1 n", "lake_sql": "select 1 k, 1 n", "key": ["k"], "measures": {"n": 0}, "surprise": 1},
    {"name": "r", "legacy_sql": "select 1 k, 1 n", "lake_sql": "select 1 k, 1 n", "key": [], "measures": {"n": 0}},
    {"name": "r", "legacy_sql": "select 1 k, 1 n", "lake_sql": "select 1 k, 1 n", "key": ["k"], "measures": {}},
    {"name": "r", "legacy_sql": "select 1 k, 1 n", "lake_sql": "select 1 k, 1 n", "key": ["k"], "measures": {"n": "x"}},
    {"name": "r", "legacy_sql": "select 1 k, 1 n", "lake_sql": "select 1 k, 1 n", "key": ["k"], "measures": {"n": 0},
     "known_differences": [{"when": "True", "reason": "r", "surprise": 1}]},
])
def test_bad_reconcile_shape_fails_loud(root, rule):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(minimal(reconcile=[rule]))


@pytest.mark.parametrize("models", [
    {"silver": {"01_x.sql": "drop table y"}},
    {"silver": {"01_x.sql": "select 1; select 2"}},
    {"silver": {"../x.sql": "select 1"}},
    {"silver": {"x y.sql": "select 1"}},
    {"silver": {"x.py": "import os"}},
    {"bronze": {"x.sql": "select 1"}},
    {"platinum": {"x.sql": "select 1"}},
])
def test_bad_models_fail_loud(root, models):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(models=models)


@pytest.mark.parametrize("ontology", [
    {"surprise": {}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k", "fields": {}}]}},
     "functions": {"f": {"sql": "select * from read_text('/etc/passwd')"}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k", "fields": {}}]}},
     "actions": {"a": {}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "x; drop", "key": "k", "fields": {}}]}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k); drop", "fields": {}}]}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k", "fields": {"a": "b from x"}}]}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k", "fields": {}, "where": "1=1; drop table x"}]}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k", "fields": {}}], "surprise": 1}}},
    {"object_types": {"A": {"sources": []}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k", "fields": {}}]}},
     "link_types": {"l": {"from": "A", "to": "A", "sql": "drop table x"}}},
    {"object_types": {"A": {"sources": [{"system": "s", "table": "silver.x", "key": "k", "fields": {}}]}},
     "link_types": {"l": {"from": "A", "to": "Nope", "sql": "select 1"}}},
])
def test_bad_ontology_fails_loud(root, ontology):
    """functions run on the ontology store connection, which is not locked, so
    a tenant ontology carries objects and links only for now."""
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(ontology=ontology)


@pytest.mark.parametrize("policies", [
    {"surprise": 1},
    {"default": "allow"},
    {"rules": [{"id": "x", "effect": "maybe", "principal": "*", "action": "*"}]},
    {"rules": [{"id": "x", "effect": "permit", "principal": "*", "action": "*", "surprise": 1}]},
    {"rules": "all"},
])
def test_bad_policies_fail_loud(root, policies):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create_minimal(policies=policies)


# ------------------------------------------------------------ load

def test_load_returns_a_pack_the_engine_can_run(root):
    seed_uploads()
    create()
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    assert pack.name == SLUG and pack.title == "Acme claims"
    assert pack.usecases == {}
    assert [s["name"] for s in pack.spec["sources"]] == ["claims", "policies"]
    assert all(s["kind"] == "upload" for s in pack.spec["sources"])
    assert [n for n, _ in pack.models("silver")] == ["claims", "policies"]
    assert [n for n, _ in pack.models("gold")] == ["claims_by_policy"]


def test_a_tenant_pack_never_runs_a_generator_even_if_one_is_planted(root, tmp_path):
    seed_uploads()
    create()
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    canary = tmp_path / "ran"
    (pack.dir / "generator.py").write_text(f"open({str(canary)!r}, 'w').close()\n")
    with pytest.raises(NoGenerator):
        pack.generator()
    assert not canary.exists()


def test_a_demo_pack_still_has_its_generator(root):
    assert hasattr(load_pack("retail").generator(), "generate")


def test_load_rejects_a_workspace_yaml_edited_into_something_bad(root):
    seed_uploads()
    create()
    f = wt.load_workspace(DEV_ORG_ID, SLUG).dir / "workspace.yaml"
    spec = yaml.safe_load(f.read_text())
    spec["surprise"] = 1
    f.write_text(yaml.safe_dump(spec))
    with pytest.raises(WorkspaceError):
        wt.load_workspace(DEV_ORG_ID, SLUG)


def test_load_rejects_an_oversized_workspace_yaml(root):
    seed_uploads()
    create()
    f = wt.load_workspace(DEV_ORG_ID, SLUG).dir / "workspace.yaml"
    f.write_text("title: " + "x" * (2 * 1024 * 1024))
    with pytest.raises(WorkspaceError):
        wt.load_workspace(DEV_ORG_ID, SLUG)


def test_load_rejects_a_symlinked_workspace_yaml(root, tmp_path):
    seed_uploads()
    create()
    d = wt.load_workspace(DEV_ORG_ID, SLUG).dir
    outside = tmp_path / "outside.yaml"
    outside.write_text((d / "workspace.yaml").read_text())
    (d / "workspace.yaml").unlink()
    (d / "workspace.yaml").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        wt.load_workspace(DEV_ORG_ID, SLUG)


def test_a_workspace_with_no_ontology_yet_still_loads(root):
    """Run comes before ontology in the flow: the ontology is proposed later."""
    seed_uploads()
    create_minimal()
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    assert pack.ontology == {} and pack.policies == {}


@pytest.mark.parametrize("slug", ["../x", "a", "UPPER", "a/b", "..", "", "x" * 41, "-abc"])
def test_bad_slugs_fail_on_every_road(root, slug):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        wt.create_workspace(DEV_ORG_ID, slug, minimal())
    for fn in (wt.load_workspace, wt.read_workspace, wt.delete_workspace):
        with pytest.raises(FileNotFoundError):
            fn(DEV_ORG_ID, slug)


# ------------------------------------------------------------ CRUD

def test_crud_round_trip(root):
    seed_uploads()
    assert wt.list_workspaces(DEV_ORG_ID) == []
    create()
    assert [w["slug"] for w in wt.list_workspaces(DEV_ORG_ID)] == [SLUG]
    assert wt.list_workspaces(DEV_ORG_ID)[0]["title"] == "Acme claims"
    got = wt.read_workspace(DEV_ORG_ID, SLUG)
    assert got["slug"] == SLUG and got["title"] == "Acme claims"
    assert got["spec"]["sources"][0]["upload"] == "fixture/claims.csv"
    wt.delete_workspace(DEV_ORG_ID, SLUG)
    assert wt.list_workspaces(DEV_ORG_ID) == []
    with pytest.raises(FileNotFoundError):
        wt.read_workspace(DEV_ORG_ID, SLUG)
    assert (root / "tenants" / DEV_ORG_ID / "uploads" / "fixture" / "claims.csv").exists()


def test_what_a_reader_gets_never_names_a_server_path(root):
    seed_uploads()
    create()
    assert str(root) not in json.dumps([wt.read_workspace(DEV_ORG_ID, SLUG), wt.list_workspaces(DEV_ORG_ID)])


def test_create_refuses_to_adopt_an_existing_slug(root):
    seed_uploads()
    create()
    with pytest.raises(WorkspaceError):
        create()


def test_create_refuses_to_adopt_a_foreign_folder(root):
    seed_uploads()
    d = config.workspaces_root(DEV_ORG_ID) / SLUG
    d.mkdir(parents=True)
    (d / "precious.txt").write_text("keep")
    with pytest.raises(WorkspaceError):
        create()
    assert (d / "precious.txt").read_text() == "keep"


def test_a_failed_create_leaves_nothing_behind(root):
    seed_uploads()
    with pytest.raises(WorkspaceError):
        create(models={"silver": {"x.sql": "drop table y"}})
    assert not (config.workspaces_root(DEV_ORG_ID) / SLUG).exists()


def test_delete_refuses_a_folder_without_our_marker(root):
    seed_uploads()
    d = config.workspaces_root(DEV_ORG_ID) / SLUG
    d.mkdir(parents=True)
    (d / "workspace.yaml").write_text("kind: workspace\n")
    with pytest.raises(FileNotFoundError):
        wt.delete_workspace(DEV_ORG_ID, SLUG)
    assert (d / "workspace.yaml").exists()
    assert wt.list_workspaces(DEV_ORG_ID) == []


def test_delete_refuses_a_marker_copied_from_another_workspace_or_org(root):
    seed_uploads()
    create()
    src = config.workspaces_root(DEV_ORG_ID) / SLUG / wt.MARKER
    for org, slug in ((DEV_ORG_ID, "copied"), (OTHER_ORG, SLUG)):
        d = config.workspaces_root(org) / slug
        d.mkdir(parents=True)
        shutil.copy(src, d / wt.MARKER)
        (d / "precious.txt").write_text("keep")
        with pytest.raises(FileNotFoundError):
            wt.delete_workspace(org, slug)
        assert (d / "precious.txt").exists()


def test_delete_refuses_a_symlinked_marker_and_a_symlinked_workspace(root, tmp_path):
    seed_uploads()
    create()
    real = config.workspaces_root(DEV_ORG_ID) / SLUG
    (config.workspaces_root(DEV_ORG_ID) / "a-link").symlink_to(real)
    with pytest.raises(FileNotFoundError):
        wt.delete_workspace(DEV_ORG_ID, "a-link")
    assert [w["slug"] for w in wt.list_workspaces(DEV_ORG_ID)] == [SLUG]
    marker = real / wt.MARKER
    outside = tmp_path / "m"
    outside.write_text(marker.read_text())
    marker.unlink()
    marker.symlink_to(outside)
    with pytest.raises(FileNotFoundError):
        wt.delete_workspace(DEV_ORG_ID, SLUG)
    assert real.exists()


def test_one_org_cannot_see_or_touch_anothers_workspace(root):
    seed_uploads()
    create()
    assert wt.list_workspaces(OTHER_ORG) == []
    for fn in (wt.load_workspace, wt.read_workspace, wt.delete_workspace):
        with pytest.raises(FileNotFoundError):
            fn(OTHER_ORG, SLUG)
    assert wt.read_workspace(DEV_ORG_ID, SLUG)


def test_an_org_from_a_real_signup_owns_its_workspace_and_nobody_else_sees_it(root):
    now = time.time()
    a = service.resolve(service.signup("a@a.example", "correct horse battery", "Acme", now)["token"], now)
    b = service.resolve(service.signup("b@b.example", "correct horse battery", "Birch", now)["token"], now)
    seed_uploads(a.org_id)
    create(a.org_id)

    assert service.can_see_workspace(a, SLUG)
    assert not service.can_see_workspace(b, SLUG)
    assert [w["slug"] for w in wt.list_workspaces(a.org_id)] == [SLUG]
    assert wt.list_workspaces(b.org_id) == []
    with pytest.raises(FileNotFoundError):
        wt.load_workspace(b.org_id, SLUG)


def test_list_for_an_org_id_that_is_not_one_is_empty(root):
    assert wt.list_workspaces("../..") == []


# ------------------------------------------------------------ ingest

def write_upload(name: str, data: bytes) -> str:
    d = config.uploads_root(DEV_ORG_ID) / "u1"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return f"u1/{name}"


def rows_of(src: dict) -> list[dict]:
    wt.create_workspace(DEV_ORG_ID, SLUG, minimal(sources=[src]))
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    return legacy.read_source(pack.spec["sources"][0], pack.paths.legacy).to_pylist()


def test_csv_with_bom_messy_headers_and_blank_lines(root):
    ref = write_upload("c.csv", b"\xef\xbb\xbf Claim ID ,Loss-Date\r\n\r\nC1, x \r\n\r\n")
    rows = rows_of(source(upload=ref, columns=[{"name": "claim_id", "type": "string"}, {"name": "loss_date", "type": "string"}]))
    assert rows == [{"claim_id": "C1", "loss_date": " x ", "_source_file": "c.csv"}]


def test_csv_keeps_extra_cells_and_nulls_short_rows(root):
    ref = write_upload("c.csv", b"claim_id,b\nC1,x,over,flow\nC2\n")
    rows = rows_of(source(upload=ref))
    assert rows[0]["_extra"] == "over,flow"
    assert rows[1]["claim_id"] == "C2" and rows[1]["b"] is None


def test_csv_quoted_commas_and_newlines_survive(root):
    ref = write_upload("c.csv", b'claim_id,note\nC1,"a,b\nc"\n')
    assert rows_of(source(upload=ref))[0]["note"] == "a,b\nc"


def test_csv_with_a_delimiter_and_an_encoding(root):
    ref = write_upload("c.csv", "claim_id|name\nC1|José\n".encode("latin-1"))
    rows = rows_of(source(upload=ref, layout={"delimiter": "|", "header": True, "encoding": "latin-1"}))
    assert rows[0]["name"] == "José"


def test_csv_without_a_header_takes_the_declared_columns(root):
    ref = write_upload("c.csv", b"C1,10\nC2,20\n")
    rows = rows_of(source(upload=ref, layout={"delimiter": ",", "header": False, "encoding": "utf-8"},
                          columns=[{"name": "claim_id", "type": "string"}, {"name": "amount", "type": "double"}]))
    assert [(r["claim_id"], r["amount"]) for r in rows] == [("C1", "10"), ("C2", "20")]


def test_a_header_cannot_spell_an_engine_column(root):
    ref = write_upload("c.csv", b"claim_id,_run_id,_source_system\nC1,forged,forged\n")
    row = rows_of(source(upload=ref))[0]
    assert "_run_id" not in row and "_source_system" not in row
    assert row["run_id"] == "forged"


def test_fixed_width_honors_start_and_width(root):
    # 0-based start, a gap at column 5, a 0x85 byte that must not split the record.
    ref = write_upload("p.dat", b"P0001 Jos\x85 \r\n\r\nP0002 Bo\n")
    fields = [{"name": "policy", "start": 0, "width": 5}, {"name": "holder", "start": 6, "width": 8}]
    rows = rows_of(source(upload=ref, format="fixed_width", layout={"fields": fields, "encoding": "cp1252"},
                          columns=[{"name": "policy", "type": "string"}]))
    assert [(r["policy"], r["holder"]) for r in rows] == [("P0001", "Jos…"), ("P0002", "Bo")]


@pytest.mark.parametrize("data,cols", [
    (b"claim_id,b\n", None),                                   # a header and no rows
    (b"", None),
    (b"other\nx\n", None),                                     # declared column absent
    (b"claim id,Claim_ID\na,b\n", None),                       # two headers, one name
    (b"claim_id\n\xff\xfe\n", None),                           # not utf-8
])
def test_a_file_that_is_not_what_the_spec_says_fails_loud(root, data, cols):
    ref = write_upload("c.csv", data)
    with pytest.raises(legacy.IngestError):
        rows_of(source(upload=ref))


def test_ingest_errors_name_the_source_not_the_server_path(root):
    ref = write_upload("c.csv", b"other\nx\n")
    with pytest.raises(legacy.IngestError) as e:
        rows_of(source(upload=ref))
    assert "claims" in str(e.value) and str(root) not in str(e.value)


def test_read_refuses_an_upload_swapped_for_a_symlink_after_create(root, tmp_path):
    ref = write_upload("c.csv", b"claim_id\nC1\n")
    wt.create_workspace(DEV_ORG_ID, SLUG, minimal(sources=[source(upload=ref)]))
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    f = config.uploads_root(DEV_ORG_ID) / ref
    secret = tmp_path / "s.csv"
    secret.write_text("claim_id\nleaked\n")
    f.unlink()
    f.symlink_to(secret)
    with pytest.raises(legacy.IngestError) as e:
        legacy.read_source(pack.spec["sources"][0], pack.paths.legacy)
    assert "leaked" not in str(e.value)


def test_existing_demo_readers_are_untouched(tmp_path):
    (tmp_path / "x.csv").write_text("a,b\n1,2\n")
    t = legacy.read_source({"kind": "csv", "glob": "x.csv"}, tmp_path)
    assert t.to_pylist() == [{"a": "1", "b": "2", "_source_file": "x.csv"}]


# ------------------------------------------------------------ verification

def test_a_tenant_workspace_is_l1_and_says_why(root):
    seed_uploads()
    create()
    v = verification.derive(wt.load_workspace(DEV_ORG_ID, SLUG))
    assert v["level"] == "L1" and v["ground_truth"] is False
    assert "structural and reconciliation" in v["meaning"]


def test_planted_artifacts_cannot_raise_a_tenant_workspace_above_l1(root):
    seed_uploads()
    create()
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    pack.paths.ensure()
    pack.paths.ground_truth.write_text("{}")
    (pack.paths.reports / "evals.json").write_text(json.dumps({"passed": True}))
    (pack.paths.root / "scenario.yaml").write_text("name: x\n")
    v = verification.derive(pack)
    assert v["level"] == "L1"
    assert v["ground_truth"] is False and v["scenario_provenance"] is False


def test_a_demo_pack_derivation_is_unchanged(root):
    pack = load_pack("retail")
    pack.paths.ensure()
    assert verification.derive(pack)["level"] == "L1"
    pack.paths.ground_truth.write_text("{}")
    (pack.paths.reports / "evals.json").write_text(json.dumps({"passed": True}))
    assert verification.derive(pack)["level"] == "L3"


# ------------------------------------------------------------ end to end

@pytest.fixture()
def ran(root):
    seed_uploads()
    create()
    return wt.run_workspace(DEV_ORG_ID, SLUG)


def test_the_fixture_runs_green_end_to_end_on_real_rows(ran):
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    assert ran["passed"] is True
    layers = {la["layer"]: la for la in ran["layers"]}
    assert set(layers) == {"pipeline", "migration", "structure"}
    lake = Lakehouse(pack)
    # Raw stays raw: seven data rows with the duplicate, in the file's own casing.
    bronze = lake.read("bronze.claims").to_pylist()
    assert len(bronze) == 6 and sum(r["claim_id"] == "C-1002" for r in bronze) == 2
    assert {r["_source_system"] for r in bronze} == {"claims"}
    # Cleaned one level up.
    silver = {r["claim_id"]: r for r in lake.read("silver.claims").to_pylist()}
    assert len(silver) == 5 and silver["C-1001"]["amount"] == 1200.5
    assert silver["C-1002"]["policy_ref"] == "P-100"
    assert lake.read("silver.policies").num_rows == 3
    assert "José Nuñez" in {r["holder"] for r in lake.read("silver.policies").to_pylist()}
    gold = {r["policy_no"]: r for r in lake.read("gold.claims_by_policy").to_pylist()}
    assert gold["P-100"]["claims"] == 2
    # The reconciliation rules really ran and really matched.
    rec = json.loads((pack.paths.reports / "reconcile.json").read_text())
    assert len(rec["rules"]) == 2 and all(r["counts"]["match"] == 1 for r in rec["rules"])
    # The ontology holds objects and a link, and the orphan claim is counted.
    onto = json.loads((pack.paths.reports / "ontology.json").read_text())
    assert onto["object_types"]["Claim"]["objects"] == 5
    assert onto["object_types"]["Policy"]["objects"] == 3
    assert onto["links"]["claim_on_policy"] == {"links": 4, "orphans": 1}
    # The flagged messy rows were flagged, not published around the gate.
    mig = json.loads((pack.paths.reports / "migrate.json").read_text())
    bronze_claims = next(w for w in mig["writes"] if w["table"] == "bronze.claims")
    assert bronze_claims["published"] is True
    flagged = {(c["check"], c["severity"]) for c in bronze_claims["checks"] if not c["passed"]}
    assert ("unique", "warn") in flagged and ("type_castable", "warn") in flagged


def test_the_report_is_on_disk_and_claims_no_ground_truth(ran):
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    saved = json.loads((pack.paths.reports / "evals.json").read_text())
    assert saved["passed"] is True and saved["planted"] == []
    assert not pack.paths.ground_truth.exists()
    assert verification.derive(pack)["level"] == "L1"
    assert verification.derive(pack)["evals_green"] is True


def test_everything_the_run_writes_stays_under_the_orgs_prefix(root, ran):
    tenant = root / "tenants" / DEV_ORG_ID
    outside = [p for p in root.rglob("*") if p.is_file() and tenant not in p.parents]
    assert outside == []
    export = wt.load_workspace(DEV_ORG_ID, SLUG).paths.reports / f"{SLUG}.json"
    assert json.loads(export.read_text())["pack"] == SLUG


def test_two_orgs_with_one_slug_share_nothing(root):
    seed_uploads()
    create()
    wt.run_workspace(DEV_ORG_ID, SLUG)
    seed_uploads(OTHER_ORG)
    other = config.uploads_root(OTHER_ORG) / "fixture" / "claims.csv"
    other.write_bytes(b"Claim ID,Policy Ref,Amount,Loss Date,Status\nZ-1,P-100,1,2026-01-01,open\n")
    create(OTHER_ORG)
    wt.run_workspace(OTHER_ORG, SLUG)
    mine = Lakehouse(wt.load_workspace(DEV_ORG_ID, SLUG)).read("silver.claims").column("claim_id").to_pylist()
    theirs = Lakehouse(wt.load_workspace(OTHER_ORG, SLUG)).read("silver.claims").column("claim_id").to_pylist()
    assert "Z-1" not in mine and theirs == ["Z-1"]


def test_a_second_run_stays_green(ran):
    again = wt.run_workspace(DEV_ORG_ID, SLUG)
    assert again["passed"] is True
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    assert Lakehouse(pack).read("silver.claims").num_rows == 5


def test_an_unexplained_reconcile_difference_is_not_green(root):
    seed_uploads()
    b = bundle()
    b["spec"]["reconcile"][0]["lake_sql"] = "select 'all' k, count(*) + 1 n from silver_claims"
    create(spec=b["spec"])
    rep = wt.run_workspace(DEV_ORG_ID, SLUG)
    layers = {la["layer"]: la["passed"] for la in rep["layers"]}
    assert rep["passed"] is False and layers["migration"] is False


def test_a_blocked_model_is_not_green(root):
    """The demo pipeline layer only asks if a block leaked. A tenant run also
    has to deliver its tables, or green would mean nothing landed."""
    seed_uploads()
    b = bundle()
    b["spec"]["quality"].append({"table": "silver.claims", "check": "row_count_min", "min": 100})
    create(spec=b["spec"])
    rep = wt.run_workspace(DEV_ORG_ID, SLUG)
    layers = {la["layer"]: la for la in rep["layers"]}
    assert rep["passed"] is False and layers["structure"]["passed"] is False
    assert "silver.claims" in layers["structure"]["headline"]


def test_a_model_with_no_rows_is_not_green(root):
    seed_uploads()
    b = bundle()
    b["models"]["gold"] = {"01_claims_by_policy.sql": "select 1 as policy_no where false"}
    b["spec"]["quality"] = [q for q in b["spec"]["quality"] if q["table"] != "gold.claims_by_policy"]
    create(spec=b["spec"], models=b["models"])
    rep = wt.run_workspace(DEV_ORG_ID, SLUG)
    assert rep["passed"] is False


def test_an_object_type_with_no_objects_is_not_green(root):
    seed_uploads()
    b = bundle()
    b["ontology"]["object_types"]["Claim"]["sources"][0]["where"] = "claim_id = 'none'"
    create(ontology=b["ontology"])
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is False


def test_a_run_with_no_ontology_yet_is_green_and_builds_none(root):
    seed_uploads()
    b = bundle()
    create(ontology=None, policies=None, spec=b["spec"])
    pack = wt.load_workspace(DEV_ORG_ID, SLUG)
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True
    assert not pack.paths.ontology_db.exists()


def test_tenant_sql_cannot_read_the_disk_during_a_run(root, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("leaked")
    seed_uploads()
    b = bundle()
    b["models"]["silver"]["03_steal.sql"] = f"select content from read_text('{secret}')"
    create(models=b["models"])
    with pytest.raises(Exception, match="(?i)external|disabled|permission"):
        wt.run_workspace(DEV_ORG_ID, SLUG)
