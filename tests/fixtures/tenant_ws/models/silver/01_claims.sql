select claim_id,
       upper(trim(policy_ref)) as policy_ref,
       try_cast(replace(replace(amount, '$', ''), ',', '') as double) as amount,
       coalesce(try_strptime(loss_date, '%Y-%m-%d'), try_strptime(loss_date, '%m/%d/%Y'))::date as loss_date,
       lower(trim(status)) as status
from bronze_claims
qualify row_number() over (partition by claim_id order by status) = 1
