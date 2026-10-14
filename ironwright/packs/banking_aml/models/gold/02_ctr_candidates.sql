-- CTR rule: cash in over $10,000 per customer per DAY, aggregated (not per transaction)
select cust_nbr, cast(ts as date) as d, round(sum(amount), 2) as cash_in_total, count(*) as deposits
from silver_transactions where channel = 'CASH' and direction = 'CR'
group by 1, 2 having sum(amount) > 10000
