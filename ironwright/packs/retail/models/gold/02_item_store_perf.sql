-- Last 28 days vs an expectation built from the item's chain-wide baseline rate (Jul-Aug, in-stock
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
