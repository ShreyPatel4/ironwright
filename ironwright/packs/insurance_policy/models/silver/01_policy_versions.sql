-- Bitemporal: valid_from/valid_to is when it was true, tx_from/tx_to is when we believed it.
-- Each row is one rectangle. For any pair of times an as-of join finds at most one row per policy:
--   valid_from <= V and V < valid_to and tx_from <= T and T < coalesce(tx_to, timestamp '9999-12-31')
-- Inside a slice of valid time the highest sequence booked so far wins, so a late, stale row never takes over.
-- Valid time is wall clock at the insured's location. A policy form starts at 12:01 AM local whatever
-- zone the row claims, and recorded_valid_from keeps what reading that label literally would say.
with raw as (
  select policy_nbr, cast(seq as integer) as seq, txn_type, state, eff_tz,
         strptime(valid_from, '%Y%m%d%H%M') as valid_from, strptime(valid_to, '%Y%m%d%H%M') as valid_to,
         strptime(booked_ts, '%Y%m%d%H%M') as tx_from,
         case status when 'C' then 'CANCELLED' else 'ACTIVE' end as status,
         case state when 'CO' then 'MT' when 'TX' then 'CT' when 'OH' then 'ET' end as state_tz
  from bronze_policy_admin
), bounds as (
  select policy_nbr, valid_from as b from raw union select policy_nbr, valid_to from raw
), slices as (
  select policy_nbr, b as slice_from, lead(b) over (partition by policy_nbr order by b) as slice_to from bounds
), covered as (
  select s.slice_from, s.slice_to, r.* exclude (valid_from, valid_to)
  from slices s join raw r on r.policy_nbr = s.policy_nbr and r.valid_from <= s.slice_from and s.slice_to <= r.valid_to
  where s.slice_to is not null
), ranked as (
  select *, coalesce(max(seq) over (partition by policy_nbr, slice_from order by tx_from, seq
                                    rows between unbounded preceding and 1 preceding), -1) as best_before
  from covered
), winners as (select * from ranked where seq > best_before)
select row_number() over (order by policy_nbr, slice_from, tx_from) as version_id, policy_nbr,
       slice_from as valid_from, slice_to as valid_to, tx_from,
       lead(tx_from) over (partition by policy_nbr, slice_from order by tx_from, seq) as tx_to,
       seq, txn_type, status, state, eff_tz, state_tz, eff_tz <> state_tz as tz_mismatch,
       slice_from + to_hours(
         (case state_tz when 'MT' then -7 when 'CT' then -6 when 'ET' then -5 end)
         - (case eff_tz when 'MT' then -7 when 'CT' then -6 when 'ET' then -5 else 0 end)) as recorded_valid_from
from winners
