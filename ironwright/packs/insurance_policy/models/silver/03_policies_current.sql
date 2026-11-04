-- One row per policy: what we believe today. A cancellation booked more than a day late is retro-dated.
with latest as (
  select policy_nbr, min(valid_from) as term_from, max(valid_to) as term_to,
         arg_max(status, valid_from) as status, any_value(state) as state, bool_or(tz_mismatch) as tz_mismatch
  from silver_policy_versions where tx_to is null group by 1
), raw as (
  select policy_nbr, txn_type, strptime(valid_from, '%Y%m%d%H%M') as valid_from, strptime(booked_ts, '%Y%m%d%H%M') as booked,
         insured_id, seq
  from bronze_policy_admin
), retro as (
  select distinct policy_nbr from raw where txn_type = 'CAN' and booked > valid_from + interval 1 day
), who as (select policy_nbr, any_value(insured_id) as insured_id from raw group by 1)
select l.policy_nbr, ltrim(l.policy_nbr, '0') as policy_digits, l.state, l.status, l.term_from, l.term_to, w.insured_id,
       l.tz_mismatch, r.policy_nbr is not null as retro_cancelled
from latest l left join retro r on r.policy_nbr = l.policy_nbr left join who w on w.policy_nbr = l.policy_nbr
