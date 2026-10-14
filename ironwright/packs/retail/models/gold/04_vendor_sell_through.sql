with bounds as (select max(d) - interval 27 day as start_d from gold_item_store_daily)
select m.vendor_nbr, count(distinct g.item_nbr) as items, count(distinct g.store_nbr) as stores,
       sum(g.units) as units_28d, round(sum(g.net_sales), 2) as sales_28d,
       sum(case when g.on_hand = 0 then 1 else 0 end) as oos_store_days
from gold_item_store_daily g join silver_items_mainframe m using (item_nbr), bounds
where g.d >= bounds.start_d
group by 1
