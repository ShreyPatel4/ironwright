-- Same bitemporal shape as policy_versions, one contract (cov_id) at a time.
-- Two different contracts may cover the same days. That is a defect, and it stays visible here.
-- A limit that is not a filed amount but is one times 100 was stored in cents.
with filed(amount) as (values (100000), (200000), (250000), (300000), (500000), (1000000)),
pol as (select distinct policy_nbr, state from bronze_policy_admin),
raw as (
  select c.cov_id, c.policy_nbr, cast(c.seq as integer) as seq, c.txn_type, c.coverage_code,
         cast(c.limit_amt as bigint) as limit_recorded, cast(c.deductible as integer) as deductible, c.eff_tz,
         strptime(c.valid_from, '%Y%m%d%H%M') as valid_from, strptime(c.valid_to, '%Y%m%d%H%M') as valid_to,
         strptime(c.booked_ts, '%Y%m%d%H%M') as tx_from,
         p.policy_nbr is not null as policy_known,
         case p.state when 'CO' then 'MT' when 'TX' then 'CT' when 'OH' then 'ET' end as state_tz
  from bronze_coverage_admin c left join pol p on p.policy_nbr = c.policy_nbr
), bounds as (
  select cov_id, valid_from as b from raw union select cov_id, valid_to from raw
), slices as (
  select cov_id, b as slice_from, lead(b) over (partition by cov_id order by b) as slice_to from bounds
), covered as (
  select s.slice_from, s.slice_to, r.* exclude (valid_from, valid_to)
  from slices s join raw r on r.cov_id = s.cov_id and r.valid_from <= s.slice_from and s.slice_to <= r.valid_to
  where s.slice_to is not null
), ranked as (
  select *, coalesce(max(seq) over (partition by cov_id, slice_from order by tx_from, seq
                                    rows between unbounded preceding and 1 preceding), -1) as best_before
  from covered
), winners as (select * from ranked where seq > best_before)
select row_number() over (order by cov_id, slice_from, tx_from) as version_id, cov_id, policy_nbr, coverage_code,
       slice_from as valid_from, slice_to as valid_to, tx_from,
       lead(tx_from) over (partition by cov_id, slice_from order by tx_from, seq) as tx_to,
       seq, txn_type, limit_recorded,
       limit_recorded not in (select amount from filed) and limit_recorded / 100 in (select amount from filed) as limit_was_cents,
       case when limit_recorded not in (select amount from filed) and limit_recorded / 100 in (select amount from filed)
            then limit_recorded // 100 else limit_recorded end as limit_dollars,
       deductible, eff_tz, state_tz, policy_known, coalesce(eff_tz <> state_tz, false) as tz_mismatch,
       slice_from + to_hours(
         coalesce(case state_tz when 'MT' then -7 when 'CT' then -6 when 'ET' then -5 end, 0)
         - (case eff_tz when 'MT' then -7 when 'CT' then -6 when 'ET' then -5 else 0 end)) as recorded_valid_from
from winners
