-- POS files can be re-sent; keep exactly one row per store, item and day
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
