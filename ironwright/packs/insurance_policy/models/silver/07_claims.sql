select f.claim_id, p.policy_nbr, f.policy_ref, cast(f.loss_ts as timestamp) as loss_ts, f.peril,
       cast(f.est_amount as double) as est_amount, cast(f.paid_amount as double) as paid_amount,
       cast(f.reported_ts as timestamp) as reported_ts
from bronze_claims_feed f
left join silver_policies_current p on p.policy_digits = regexp_replace(f.policy_ref, '[^0-9]', '', 'g')
