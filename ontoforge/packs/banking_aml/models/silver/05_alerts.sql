select alert_id, acct_nbr, rule_code, cast(alert_date as date) as alert_date, cast(score as integer) as score, status
from bronze_alerts
