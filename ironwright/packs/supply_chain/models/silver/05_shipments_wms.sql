-- the WMS writes 000012345 and still contains test orders (9999xxxxx); those never leave silver
select wms_ref, ltrim(order_nbr, '0') as order_digits, dc_code,
       strptime(ship_date, '%Y%m%d')::date as ship_date,
       cast(cartons as integer) as cartons, status
from bronze_wms_confirmations
where order_nbr not like '9999%'
