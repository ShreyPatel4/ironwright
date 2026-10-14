-- every region should send POS every day; a quiet feed shows up here as pos_rows = 0
with days as (select distinct snapshot_date as d from silver_inventory),
regions as (select distinct region from silver_stores where status = 'OPEN')
select r.region, d.d, count(p.upc) as pos_rows
from regions r cross join days d
left join silver_stores s on s.region = r.region
left join silver_pos_sales p on p.store_nbr = s.store_nbr and p.sale_date = d.d
group by 1, 2
