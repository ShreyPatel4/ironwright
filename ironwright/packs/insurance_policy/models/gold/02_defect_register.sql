-- Every data defect the platform can see, one row each, named so a person can go and fix it.
with v as (select * from silver_coverages_current)
select 'overlapping_coverage' as defect, a.cov_id || '|' || b.cov_id as subject
from v a join v b on a.policy_nbr = b.policy_nbr and a.coverage_code = b.coverage_code and a.cov_id < b.cov_id
 and a.valid_from < b.valid_to and b.valid_from < a.valid_to
union all select 'endorsement_out_of_order', cov_id from v where endorsed_out_of_order
union all select 'lapsed_policy_paid_claim', claim_id from gold_coverage_verification where lapsed_on_loss and paid_amount > 0
union all
select 'duplicate_insured', string_agg(client_key, '|' order by client_key)
from silver_agency_clients group by match_key having count(distinct agency_code) > 1
union all select 'limit_cents_vs_dollars', cov_id from v where limit_was_cents
union all select 'retro_dated_cancellation', policy_nbr from silver_policies_current where retro_cancelled
union all
select distinct 'orphan_coverage_rows', c.cov_id from bronze_coverage_admin c
where c.policy_nbr not in (select policy_nbr from silver_policies_current)
union all select 'wrong_effective_timezone', policy_nbr from silver_policies_current where tz_mismatch
