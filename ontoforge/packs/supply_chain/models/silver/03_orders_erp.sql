select order_no, order_no as order_digits, customer_id, priority,
       cast(order_value_usd as double) as order_value_usd,
       cast(requested_date as date) as requested_date, cast(order_date as date) as order_date
from bronze_erp_orders
