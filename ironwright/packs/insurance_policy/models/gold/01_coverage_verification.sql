-- One row per claim: was this loss covered on the loss date, by which coverage, with what limit and deductible?
-- Two as-of joins, same valid time: what we believe now, and what we believed at the loss itself.
with status_now as (
  select c.claim_id, v.status from silver_claims c join silver_policy_versions v
    on v.policy_nbr = c.policy_nbr and v.valid_from <= c.loss_ts and c.loss_ts < v.valid_to and v.tx_to is null
), status_then as (
  select c.claim_id, v.status from silver_claims c join silver_policy_versions v
    on v.policy_nbr = c.policy_nbr and v.valid_from <= c.loss_ts and c.loss_ts < v.valid_to
   and v.tx_from <= c.loss_ts and c.loss_ts < coalesce(v.tx_to, timestamp '9999-12-31')
), cov as (
  select c.claim_id, count(*) as matched_coverages, min(v.cov_id) as cov_id,
         arg_min(v.limit_dollars, v.cov_id) as limit_dollars, arg_min(v.deductible, v.cov_id) as deductible
  from silver_claims c join silver_coverage_versions v
    on v.policy_nbr = c.policy_nbr and v.coverage_code = c.peril and v.policy_known
   and v.valid_from <= c.loss_ts and c.loss_ts < v.valid_to and v.tx_to is null
  group by 1
), lapsed as (
  select distinct c.claim_id from silver_claims c join silver_lapse_windows w on w.policy_nbr = c.policy_nbr
   and w.lapse_date <= cast(c.loss_ts as date) and (w.reinstate_date is null or cast(c.loss_ts as date) < w.reinstate_date)
), facts as (
  select c.claim_id, c.policy_nbr, c.peril, c.loss_ts, c.est_amount, c.paid_amount, c.reported_ts,
         n.status as status_now, t.status as status_then,
         coalesce(t.status = 'ACTIVE', false) as in_force_known_at_loss,
         coalesce(n.status = 'ACTIVE', false) as in_force_known_now,
         l.claim_id is not null as lapsed_on_loss,
         coalesce(v.matched_coverages, 0) as matched_coverages, v.cov_id, v.limit_dollars, v.deductible
  from silver_claims c
  left join status_now n on n.claim_id = c.claim_id
  left join status_then t on t.claim_id = c.claim_id
  left join cov v on v.claim_id = c.claim_id
  left join lapsed l on l.claim_id = c.claim_id
), judged as (
  select *, case
      when lapsed_on_loss then 'lapsed_on_loss_date'
      when status_now = 'CANCELLED' and status_then = 'ACTIVE' then 'retro_cancelled'
      when status_now = 'CANCELLED' then 'cancelled'
      when status_now is null or matched_coverages = 0 then 'no_coverage_in_force'
      when matched_coverages > 1 then 'overlapping_coverage'
      when est_amount > limit_dollars then 'over_limit'
      else 'covered' end as reason
  from facts
)
select *, reason = 'covered' as covered,
       case when reason = 'covered' then greatest(est_amount - deductible, 0) else 0 end as net_payable
from judged
