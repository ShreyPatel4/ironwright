"""A second dataset joins a workspace that already exists, and the first one is not touched."""
from __future__ import annotations

import time
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from ironwright import config
from ironwright import workspaces_tenant as wt
from ironwright.platform_api import NOT_FOUND, create_platform_app
from ironwright.tenancy import DEV_ORG_ID, service

SLUG = "sales"
PASSWORD = "correct-horse-9"
ORDERS = b"order_id,customer,amount\n1001,Ann,10.50\n1002,Bo,11.50\n1003,Cy,12.50\n"
REGIONS = b"region_id,region_name\nR1,North\nR2,South\n"
SILVER_ORDERS = "select order_id, customer, amount from bronze_orders"
SILVER_REGIONS = 'select "region_id" as "region_id", "region_name" as "region_name"\nfrom bronze_regions\n'


def _source(name: str, ref: str, columns: list[dict]) -> dict:
    return {"name": name, "upload": ref, "format": "csv",
            "layout": {"delimiter": ",", "header": True, "encoding": "utf-8"}, "columns": columns}


ORDER_COLUMNS = [{"name": "order_id", "type": "int", "key": True}, {"name": "customer", "type": "string"},
                 {"name": "amount", "type": "decimal(12,2)"}]
REGION_COLUMNS = [{"name": "region_id", "type": "string", "key": True}, {"name": "region_name", "type": "string"}]
MODELS = {"silver": {"regions.sql": SILVER_REGIONS}}


def _spec(ref: str) -> dict:
    return {"kind": "workspace", "version": 1, "title": "Sales", "quality": [], "reconcile": [],
            "sources": [_source("orders", ref, ORDER_COLUMNS)]}


@pytest.fixture()
def root(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(d))
    return d.resolve()


def _put(data: bytes, name: str, org: str = DEV_ORG_ID) -> tuple[str, str]:
    upload_id = uuid.uuid4().hex
    d = config.uploads_root(org) / upload_id
    d.mkdir(parents=True)
    (d / name).write_bytes(data)
    return upload_id, f"{upload_id}/{name}"


def _make() -> None:
    _, ref = _put(ORDERS, "orders.csv")
    wt.create_workspace(DEV_ORG_ID, SLUG, _spec(ref), models={"silver": {"01_orders.sql": SILVER_ORDERS}})


def _regions() -> dict:
    _, ref = _put(REGIONS, "regions.csv")
    return _source("regions", ref, REGION_COLUMNS)


def test_a_new_source_starts_at_version_one_and_the_old_one_stays_as_it_was(root):
    _make()
    before = wt.source_versions(DEV_ORG_ID, SLUG, "orders")
    made = wt.add_sources(DEV_ORG_ID, SLUG, [_regions()], models=MODELS, created_by="user:a")
    assert [(r["source"], r["version"]) for r in made] == [("regions", 1)]
    assert wt.source_versions(DEV_ORG_ID, SLUG, "orders") == before
    assert [v["version"] for v in wt.source_versions(DEV_ORG_ID, SLUG, "regions")] == [1]
    assert [s["name"] for s in wt.load_workspace(DEV_ORG_ID, SLUG).spec["sources"]] == ["orders", "regions"]


def test_the_next_run_lands_both_datasets(root):
    _make()
    wt.run_workspace(DEV_ORG_ID, SLUG)
    wt.add_sources(DEV_ORG_ID, SLUG, [_regions()], models=MODELS)
    report = wt.run_workspace(DEV_ORG_ID, SLUG)
    assert report["passed"] is True, report["layers"]
    assert wt.read_source_version(DEV_ORG_ID, SLUG, "regions", 1).num_rows == 2
    assert wt.read_source_version(DEV_ORG_ID, SLUG, "orders", 1).num_rows == 3
    from ironwright.lake import Lakehouse
    con = Lakehouse(wt.load_workspace(DEV_ORG_ID, SLUG)).connect()
    assert con.execute("select count(*) from silver_regions").fetchone()[0] == 2


def test_a_name_the_workspace_already_has_changes_nothing(root):
    _make()
    path = config.workspace_dir(DEV_ORG_ID, SLUG) / "workspace.yaml"
    before = path.read_text()
    _, ref = _put(REGIONS, "regions.csv")
    with pytest.raises(wt.WorkspaceError, match="already has a source orders"):
        wt.add_sources(DEV_ORG_ID, SLUG, [_source("orders", ref, REGION_COLUMNS)])
    assert path.read_text() == before
    assert len(wt.source_versions(DEV_ORG_ID, SLUG, "orders")) == 1


def test_a_model_may_not_replace_one_the_workspace_already_has(root):
    """Same table, spelled with another prefix too. It would silently rewrite the first dataset."""
    _make()
    model = config.workspace_dir(DEV_ORG_ID, SLUG) / "models" / "silver" / "01_orders.sql"
    for name in ("01_orders.sql", "orders.sql", "07_orders.sql"):
        with pytest.raises(wt.WorkspaceError, match="already has a model"):
            wt.add_sources(DEV_ORG_ID, SLUG, [_regions()], models={"silver": {name: "select 1 as x"}})
    assert model.read_text() == SILVER_ORDERS
    assert [s["name"] for s in wt.load_workspace(DEV_ORG_ID, SLUG).spec["sources"]] == ["orders"]


def test_a_bad_source_or_model_leaves_no_trace(root):
    _make()
    d = config.workspace_dir(DEV_ORG_ID, SLUG)
    before = (d / "workspace.yaml").read_text()
    with pytest.raises(wt.WorkspaceError):
        wt.add_sources(DEV_ORG_ID, SLUG, [{**_regions(), "columns": [{"name": "X; drop", "type": "string"}]}])
    with pytest.raises(wt.WorkspaceError):
        wt.add_sources(DEV_ORG_ID, SLUG, [_regions()], models={"silver": {"regions.sql": "drop table x"}})
    assert (d / "workspace.yaml").read_text() == before
    assert not (d / "models" / "silver" / "regions.sql").exists()
    assert not (d / wt.VERSIONS_DIR / "regions").exists()


def test_spec_stays_inside_contract_3(root):
    _make()
    wt.add_sources(DEV_ORG_ID, SLUG, [_regions()], models=MODELS)
    spec = yaml.safe_load((config.workspace_dir(DEV_ORG_ID, SLUG) / "workspace.yaml").read_text())
    assert set(spec["sources"][1]) == {"name", "upload", "format", "layout", "columns"}


# ---------------------------------------------------------------- HTTP

def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


@pytest.fixture()
def http(meta_db):
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        a = c.post("/auth/signup", json={"email": "a@a.example", "password": PASSWORD, "org_name": "Alpha org"}).json()
        b = c.post("/auth/signup", json={"email": "b@b.example", "password": PASSWORD, "org_name": "Beta org"}).json()
        yield c, a, b


def _upload(c, session, data: bytes, name: str) -> str:
    r = c.post("/uploads", headers=bearer(session), files=[("files", (name, data))])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _workspace(c, a) -> None:
    upload = _upload(c, a, ORDERS, "orders.csv")
    wt.create_workspace(a["org_id"], SLUG, _spec(f"{upload}/orders.csv"), models={"silver": {"01_orders.sql": SILVER_ORDERS}})


def _body(upload: str) -> dict:
    return {"from_upload": upload, "sources": [_source("regions", f"{upload}/regions.csv", REGION_COLUMNS)], "models": MODELS}


def test_the_route_adds_the_dataset_and_drift_lists_it(http):
    c, a, _ = http
    _workspace(c, a)
    upload = _upload(c, a, REGIONS, "regions.csv")
    r = c.post(f"/workspaces/{SLUG}/sources", headers=bearer(a), json=_body(upload))
    assert r.status_code == 201, r.text
    assert [(s["source"], s["version"]) for s in r.json()["sources"]] == [("regions", 1)]
    overview = c.get(f"/workspaces/{SLUG}/drift", headers=bearer(a)).json()
    assert set(overview["sources"]) == {"orders", "regions"}


def test_every_source_has_to_come_from_the_upload_named(http):
    c, a, _ = http
    _workspace(c, a)
    one, other = _upload(c, a, REGIONS, "regions.csv"), _upload(c, a, REGIONS, "regions.csv")
    r = c.post(f"/workspaces/{SLUG}/sources", headers=bearer(a),
               json={"from_upload": one, "sources": [_source("regions", f"{other}/regions.csv", REGION_COLUMNS)]})
    assert r.status_code == 422


def test_another_org_cannot_reach_the_route_or_use_its_upload_here(http):
    c, a, b = http
    _workspace(c, a)
    mine, theirs = _upload(c, a, REGIONS, "regions.csv"), _upload(c, b, REGIONS, "regions.csv")
    r = c.post(f"/workspaces/{SLUG}/sources", headers=bearer(b), json=_body(theirs))
    assert (r.status_code, r.json()["detail"]) == (404, NOT_FOUND)
    r = c.post(f"/workspaces/{SLUG}/sources", headers=bearer(a), json=_body(theirs))
    assert (r.status_code, r.json()["detail"]) == (404, NOT_FOUND)
    assert set(c.get(f"/workspaces/{SLUG}/drift", headers=bearer(a)).json()["sources"]) == {"orders"}
    assert mine


@pytest.mark.parametrize(("role", "version", "spec", "dataset"), [
    ("viewer", 403, 403, 403),
    ("member", 201, 403, 403),
    ("admin", 201, 200, 201),
])
def test_who_may_refresh(http, role, version, spec, dataset):
    """A member refreshes a file, as a member can upload and run. Changing what a
    workspace reads is an admin's call, the same one that creates a workspace."""
    c, a, _ = http
    _workspace(c, a)
    token = service.invite(a["org_slug"], "m@a.example", role, time.time())
    who = bearer(c.post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).json())
    v2 = _upload(c, a, ORDERS + b"1004,Di,13.50\n", "orders.csv")
    assert c.post(f"/workspaces/{SLUG}/sources/orders/versions", headers=who,
                  json={"upload": f"{v2}/orders.csv"}).status_code == version
    assert c.post(f"/workspaces/{SLUG}/sources/orders/spec", headers=who,
                  json={"columns": ORDER_COLUMNS}).status_code == spec
    upload = _upload(c, a, REGIONS, "regions.csv")
    assert c.post(f"/workspaces/{SLUG}/sources", headers=who, json=_body(upload)).status_code == dataset
