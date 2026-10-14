-- free-text counterparty names, one row per distinct spelling (resolution happens in the ontology)
select counterparty_name, any_value(counterparty_country) as country, count(*) as txns,
       list_contains(list(distinct counterparty_country), 'XR') or list_contains(list(distinct counterparty_country), 'XQ') as high_risk_jurisdiction
from silver_transactions where counterparty_name is not null and direction = 'DR' and channel = 'WIRE'
group by 1
