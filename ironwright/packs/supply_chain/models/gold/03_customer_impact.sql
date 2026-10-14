select customer_id, count(*) as open_shipments,
       sum(case when delay_hours > 0 and not delivered then 1 else 0 end) as late_open_shipments,
       round(sum(order_value_usd), 2) as open_value_usd
from gold_shipment_status where not delivered group by 1
