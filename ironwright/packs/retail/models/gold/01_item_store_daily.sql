-- One row per store, item and day on which the item was on the planogram IN EFFECT that day.
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
