-- the mainframe stores cost in cents; everything downstream uses dollars
select item_nbr,
       upc,
       trim(description) as description,
       vendor_nbr,
       dept,
       cast(cost_cents as integer) / 100.0 as unit_cost_usd,
       status
from bronze_item_master
