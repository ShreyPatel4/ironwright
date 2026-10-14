select legacy_claim_no, policy_nbr, cast(loss_date as date) as loss_date, cast(paid_amount as double) as paid_amount, peril
from bronze_prior_claims
