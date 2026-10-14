select acct_nbr, count(*) as txns_90d, round(sum(case when direction = 'CR' then amount else 0 end), 2) as credits_90d,
       round(sum(case when direction = 'DR' then amount else 0 end), 2) as debits_90d,
       round(sum(case when channel = 'CASH' and direction = 'CR' then amount else 0 end), 2) as cash_in_90d
from silver_transactions group by 1
