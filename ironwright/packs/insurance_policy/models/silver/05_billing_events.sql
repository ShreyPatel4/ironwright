select invoice_id, policy_nbr, event_type, cast(event_date as date) as event_date, cast(amount as double) as amount
from bronze_billing
