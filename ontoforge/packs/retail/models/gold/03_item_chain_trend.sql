-- chain-wide: is the item weak everywhere (demand) or only in some stores (execution)?
with bounds as (select max(d) - interval 27 day as start_d from gold_item_store_daily)
select item_nbr,
       round(avg(case when d >= start_d and on_hand > 0 then units end), 3) as window_daily,
       round(avg(case when d < start_d and on_hand > 0 then units end), 3) as baseline_daily,
       round(avg(case when d >= start_d and on_hand > 0 then units end)
             / nullif(avg(case when d < start_d and on_hand > 0 then units end), 0), 3) as chain_ratio,
       count(distinct store_nbr) as stores
from gold_item_store_daily, bounds
group by item_nbr
