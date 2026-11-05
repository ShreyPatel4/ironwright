-- match_key is a digest so the raw values never sit in a join key. It is small enough to brute force, so it is masked too.
select insured_id, any_value(insured_name) as name,
       cast(strptime(any_value(insured_dob), '%Y%m%d') as date)::varchar as dob,
       any_value(insured_last4) as gov_last4,
       md5(cast(strptime(any_value(insured_dob), '%Y%m%d') as date)::varchar || '|' || any_value(insured_last4)) as match_key
from bronze_policy_admin group by 1
