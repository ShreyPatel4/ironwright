select t.txn_id, t.acct_nbr, a.cust_nbr, strptime(t.ts, '%Y-%m-%d %H:%M:%S') as ts,
       cast(t.amount as double) as amount, t.direction, t.channel,
       nullif(t.counterparty_name, '') as counterparty_name, t.counterparty_country
from bronze_transactions t left join silver_accounts a on a.acct_nbr = t.acct_nbr
