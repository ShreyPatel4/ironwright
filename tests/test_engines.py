"""Engine swap rung (lake_engines): the same model SQL on DuckDB and postgres.

Three claims, each with a test that can fail:
1. Every retail silver and gold model, run unchanged on postgres over the
   same bronze frames DuckDB sees, returns identical results under the
   documented normalization. No incompatibilities are hidden: the report's
   mismatch list must be empty, not ignored.
2. The comparison is a real tripwire: one value corrupted in the postgres
   copy flips exactly that model's match to false.
3. The models rewritten into portable SQL still produce the same DuckDB
   output as the original DuckDB-only SQL, captured verbatim below.

Postgres-backed tests use the conftest fixtures and skip honestly without
POSTGRES_URI. The 404/503 and DuckDB-identity tests always run.
"""
import os

import pytest
from fastapi.testclient import TestClient

# The retail model files as they were before the portable rewrite, verbatim.
# The identity test runs this pipeline and the current files on the same
# bronze frames and demands equal outputs, so the rewrite cannot have
# changed what DuckDB computes.
ORIGINAL_SQL = {
    "silver.stores": r"""-- one row per store; status decoded, dates parsed
select store_nbr,
       trim(store_name) as store_name,
       region,
       cluster,
       case status when 'O' then 'OPEN' when 'C' then 'CLOSED' end as status,
       try_strptime(open_date, '%Y%m%d')::date as open_date
from bronze_store_master
""",
    "silver.items_portal": r"""select gtin14,
       vendor_code,
       item_name,
       cast(list_price_usd as double) as list_price_usd,
       cast(case_pack as integer) as case_pack
from bronze_portal_catalog
""",
    "silver.pos_sales": r"""-- POS files can be re-sent; keep exactly one row per store, item and day
with typed as (
  select store_nbr, upc, cast(sale_date as date) as sale_date,
         cast(units as integer) as units, cast(net_sales as double) as net_sales,
         cast(returns_units as integer) as returns_units, _source_file,
         row_number() over (partition by store_nbr, upc, sale_date order by _source_file) as rn
  from bronze_pos_sales
)
select t.store_nbr, t.upc, m.item_nbr, t.sale_date, t.units, t.net_sales, t.returns_units, t._source_file
from typed t
left join silver_items_mainframe m on m.upc = t.upc
where t.rn = 1
""",
    "silver.inventory": r"""select STORE_NBR as store_nbr, ITEM_NBR as item_nbr,
       strptime(regexp_extract(_source_file, '(\d{8})', 1), '%Y%m%d')::date as snapshot_date,
       cast(ON_HAND_QTY as integer) as on_hand
from bronze_inventory
""",
    "gold.item_store_daily": r"""-- One row per store, item and day on which the item was on the planogram IN EFFECT that day.
-- Step 1: ASOF join each store-day to the planogram version in effect for its cluster.
-- Step 2: join positions on that exact version. An item dropped in v2 gets no row after the change,
--         which is the point: joining to "today's" planogram would misjudge history.
with days as (
  select distinct snapshot_date as d from silver_inventory
),
store_days as (
  select s.store_nbr, s.cluster, s.region, d.d
  from silver_stores s cross join days d
),
in_effect as (
  select sd.*, v.version as pog_version, v.pog_id
  from store_days sd
  asof join silver_planogram_versions v
    on sd.cluster = v.cluster and sd.d >= v.effective_date
),
on_pog as (
  select e.store_nbr, e.cluster, e.region, e.d, e.pog_id, e.pog_version, m.item_nbr, p.upc, p.facings
  from in_effect e
  join silver_planogram_positions p on p.pog_id = e.pog_id and p.version = e.pog_version
  join silver_items_mainframe m on m.upc = p.upc
)
select o.store_nbr, o.cluster, o.region, o.item_nbr, o.upc, o.d, o.pog_version, o.facings,
       i.on_hand,
       coalesce(s.units, 0) as units,
       coalesce(s.net_sales, 0.0) as net_sales
from on_pog o
join silver_inventory i on i.store_nbr = o.store_nbr and i.item_nbr = o.item_nbr and i.snapshot_date = o.d
left join silver_pos_sales s on s.store_nbr = o.store_nbr and s.item_nbr = o.item_nbr and s.sale_date = o.d
""",
    "gold.item_store_perf": r"""-- Last 28 days vs an expectation built from the item's chain-wide baseline rate (Jul-Aug, in-stock
-- days only) and the store's traffic index. Expected units only count days the item was on the
-- planogram in effect, so planogram changes never look like underperformance.
with bounds as (
  select max(d) as end_d, max(d) - interval 27 day as start_d from gold_item_store_daily
),
base as (
  select * from gold_item_store_daily, bounds where d < start_d and on_hand > 0
),
item_rate as (
  select item_nbr, avg(units) as base_daily from base group by 1
),
store_idx as (
  select store_nbr, avg(units) / (select avg(units) from base) as traffic_index from base group by 1
),
win as (
  select g.* from gold_item_store_daily g, bounds b where g.d >= b.start_d
),
streak as (
  select store_nbr, item_nbr, count(*) as zero_streak
  from (
    select store_nbr, item_nbr, d, units,
           max(case when units > 0 then d end) over (partition by store_nbr, item_nbr) as last_sale
    from win
  ) where last_sale is null or d > last_sale
  group by 1, 2
)
select w.store_nbr, w.item_nbr, any_value(w.cluster) as cluster, any_value(w.region) as region,
       count(*) as days_on_pog,
       sum(w.units) as units_28d,
       round(sum(w.net_sales), 2) as sales_28d,
       round(coalesce(r.base_daily, 0) * coalesce(si.traffic_index, 1) * count(*), 1) as expected_units,
       round(sum(w.units) / nullif(coalesce(r.base_daily, 0) * coalesce(si.traffic_index, 1) * count(*), 0), 3) as ratio,
       sum(case when w.on_hand = 0 then 1 else 0 end) as oos_days,
       coalesce(any_value(st.zero_streak), 0) as zero_sales_streak,
       arg_max(w.on_hand, w.d) as last_on_hand,
       arg_max(w.facings, w.d) as facings_now,
       arg_max(w.pog_version, w.d) as pog_version_now,
       round(sum(w.units) / count(*), 2) as avg_daily_units
from win w
left join item_rate r on r.item_nbr = w.item_nbr
left join store_idx si on si.store_nbr = w.store_nbr
left join streak st on st.store_nbr = w.store_nbr and st.item_nbr = w.item_nbr
group by w.store_nbr, w.item_nbr, r.base_daily, si.traffic_index
""",
    "gold.item_chain_trend": r"""-- chain-wide: is the item weak everywhere (demand) or only in some stores (execution)?
with bounds as (select max(d) - interval 27 day as start_d from gold_item_store_daily)
select item_nbr,
       round(avg(case when d >= start_d and on_hand > 0 then units end), 3) as window_daily,
       round(avg(case when d < start_d and on_hand > 0 then units end), 3) as baseline_daily,
       round(avg(case when d >= start_d and on_hand > 0 then units end)
             / nullif(avg(case when d < start_d and on_hand > 0 then units end), 0), 3) as chain_ratio,
       count(distinct store_nbr) as stores
from gold_item_store_daily, bounds
group by item_nbr
""",
    "gold.vendor_sell_through": r"""with bounds as (select max(d) - interval 27 day as start_d from gold_item_store_daily)
select m.vendor_nbr, count(distinct g.item_nbr) as items, count(distinct g.store_nbr) as stores,
       sum(g.units) as units_28d, round(sum(g.net_sales), 2) as sales_28d,
       sum(case when g.on_hand = 0 then 1 else 0 end) as oos_store_days
from gold_item_store_daily g join silver_items_mainframe m using (item_nbr), bounds
where g.d >= bounds.start_d
group by 1
""",
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("engines")
    old = os.environ.get("IRONWRIGHT_DATA")
    os.environ["IRONWRIGHT_DATA"] = str(root)
    try:
        from ironwright.engines import router
        from ironwright.platform_api import create_platform_app

        app = create_platform_app()
        app.include_router(router)
        with TestClient(app) as c:
            yield c
    finally:
        if old is None:
            os.environ.pop("IRONWRIGHT_DATA", None)
        else:
            os.environ["IRONWRIGHT_DATA"] = old


@pytest.fixture(scope="module")
def built(client):
    """Retail generated and migrated into the module's data root: the lake
    the engine check reads."""
    from ironwright.lake import migrate
    from ironwright.packs import load_pack
    from ironwright.tracing import Tracer

    pack = load_pack("retail")
    pack.generator().generate(pack.paths.legacy, seed=7)
    report = migrate(pack, Tracer(pack.paths.ensure().traces))
    assert all(w["published"] for w in report["writes"])
    return pack


# ---------- routing honesty: these run with or without a postgres ----------

def test_unknown_workspace_is_404(client):
    r = client.post("/workspaces/nonsense/engines/check", json={})
    assert r.status_code == 404
    assert "no workspace named nonsense" in r.json()["detail"]


def test_organization_gets_a_clear_404(client):
    r = client.post("/workspaces", json={"name": "engines_org"})
    assert r.status_code == 201, r.text
    r = client.post("/workspaces/engines_org/engines/check", json={})
    assert r.status_code == 404
    assert "no lake yet" in r.json()["detail"]


def test_missing_uri_is_an_honest_503(client, monkeypatch):
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    r = client.post("/workspaces/retail/engines/check", json={})
    assert r.status_code == 503
    assert "POSTGRES_URI" in r.json()["detail"]


def test_schema_prefix_is_validated(client, built, monkeypatch):
    monkeypatch.setenv("POSTGRES_URI", "postgresql://nobody@127.0.0.1:1/x")
    r = client.post("/workspaces/retail/engines/check",
                    json={"schema_prefix": "x; drop schema public"})
    assert r.status_code == 422


# ---------- the rewrite kept DuckDB outputs identical (no postgres needed) ----------

def _run_duckdb_pipeline(frames, models):
    import duckdb

    con = duckdb.connect()
    try:
        for name, frame in frames.items():
            con.register(f"_bronze_{name}", frame)
            con.execute(f"create view bronze_{name} as select * from _bronze_{name}")
        out = {}
        for layer, name, sql in models:
            data = con.sql(sql).to_arrow_table()
            con.register(f"_{layer}_{name}", data)
            con.execute(f"create or replace view {layer}_{name} as select * from _{layer}_{name}")
            out[f"{layer}.{name}"] = data
        return out
    finally:
        con.close()


def test_rewritten_models_leave_duckdb_output_unchanged(built):
    from ironwright.engines import bronze_frames, compare_frames

    frames = bronze_frames(built)
    current, original = [], []
    for layer in ("silver", "gold"):
        for name, sql in built.models(layer):
            current.append((layer, name, sql))
            original.append((layer, name, ORIGINAL_SQL.get(f"{layer}.{name}", sql)))
    now = _run_duckdb_pipeline(frames, current)
    before = _run_duckdb_pipeline(frames, original)
    assert set(ORIGINAL_SQL) <= set(now)
    for model in sorted(now):
        match, why = compare_frames(before[model], now[model])
        assert match, f"{model}: the portable rewrite changed DuckDB output: {why}"
        assert now[model].num_rows > 0, f"{model}: an empty frame proves nothing"


# ---------- the cross-engine run itself (needs a real postgres) ----------

def test_every_retail_model_matches_across_engines(client, built, postgres_uri, monkeypatch):
    import psycopg

    monkeypatch.setenv("POSTGRES_URI", postgres_uri)
    r = client.post("/workspaces/retail/engines/check",
                    json={"schema_prefix": "iw_swaptest"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["engines"] == ["duckdb", "postgresql"]

    expected = [f"{layer}.{name}" for layer in ("silver", "gold")
                for name, _ in built.models(layer)]
    assert [m["model"] for m in rep["models"]] == expected

    # the honest incompatibility list is exactly empty: every model ran
    # unchanged on postgres and returned identical rows
    mismatches = [m for m in rep["models"] if not m["match"]]
    assert mismatches == [], mismatches
    assert all(m["error"] is None for m in rep["models"])
    assert all(m["rows"] > 0 for m in rep["models"])
    assert rep["match"] is True

    # the throwaway schema is gone
    assert rep["schema"].startswith("iw_swaptest_")
    assert rep["schema_dropped"] is True
    with psycopg.connect(postgres_uri) as pc:
        left = pc.execute("select nspname from pg_namespace "
                          "where nspname like 'iw\\_swaptest\\_%'").fetchall()
    assert left == []


def test_planted_divergence_is_caught(built, postgres_uri, pg_schema):
    import psycopg

    from ironwright import engines

    frames = engines.bronze_frames(built)
    engines.load_bronze_to_postgres(frames, postgres_uri, pg_schema)
    # corrupt exactly one value in the postgres copy of bronze
    with psycopg.connect(postgres_uri, autocommit=True) as pc:
        pc.execute(f"set search_path to {pg_schema}")
        tampered = pc.execute(
            "update bronze_store_master set store_name = 'TAMPERED ROW' "
            "where ctid = (select ctid from bronze_store_master limit 1)").rowcount
    assert tampered == 1

    report = engines.compare_models(built, frames, postgres_uri, pg_schema)
    by = {m["model"]: m for m in report}
    assert by["silver.stores"]["match"] is False
    assert "differ" in by["silver.stores"]["error"]
    # the tripwire is precise: store_name feeds nothing downstream, so only
    # the tampered model flips
    assert [m["model"] for m in report if not m["match"]] == ["silver.stores"]
