-- one row per alert with the behavioral features an investigator would pull by hand
select a.alert_id, a.acct_nbr, c.cust_nbr, a.rule_code, a.score, a.alert_date,
       c.cust_type, c.industry, c.risk_rating, date_diff('day', c.kyc_date, a.alert_date) as tenure_days,
       (select count(*) from silver_transactions t where t.acct_nbr = a.acct_nbr and t.channel = 'CASH' and t.direction = 'CR'
          and t.amount between 8000 and 9999.99 and t.ts between a.alert_date - interval 14 day and a.alert_date + interval 1 day) as structuring_recent_count,
       (select coalesce(sum(t.amount), 0) from silver_transactions t where t.acct_nbr = a.acct_nbr and t.channel = 'CASH' and t.direction = 'CR'
          and t.amount between 8000 and 9999.99 and t.ts between a.alert_date - interval 14 day and a.alert_date + interval 1 day) as structuring_recent_total,
       (select count(*) from silver_transactions t where t.acct_nbr = a.acct_nbr and t.channel = 'CASH' and t.direction = 'CR'
          and t.amount between 8000 and 9999.99 and t.ts between a.alert_date - interval 90 day and a.alert_date - interval 15 day) as structuring_history_count,
       (select coalesce(sum(t.amount), 0) from silver_transactions t where t.acct_nbr = a.acct_nbr and t.direction = 'CR' and t.channel = 'WIRE'
          and t.ts between a.alert_date - interval 7 day and a.alert_date + interval 1 day) as inbound_wire_total,
       (select coalesce(sum(t.amount), 0) from silver_transactions t where t.acct_nbr = a.acct_nbr and t.direction = 'DR' and t.channel in ('WIRE', 'ACH')
          and t.ts between a.alert_date - interval 7 day and a.alert_date + interval 2 day) as outbound_total,
       (select count(distinct t.counterparty_name) from silver_transactions t where t.acct_nbr = a.acct_nbr and t.direction = 'DR' and t.channel in ('WIRE', 'ACH')
          and t.ts between a.alert_date - interval 7 day and a.alert_date + interval 2 day) as distinct_out_counterparties,
       (select coalesce(sum(t.amount), 0) from silver_transactions t where t.acct_nbr = a.acct_nbr and t.direction = 'DR'
          and t.counterparty_country in ('XR', 'XQ') and t.ts > a.alert_date - interval 90 day) as high_risk_amount
from silver_alerts a
join silver_accounts ac on ac.acct_nbr = a.acct_nbr
join silver_customers_core c on c.cust_nbr = ac.cust_nbr
