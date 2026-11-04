-- One row per coverage contract: what we believe today, with its defects named.
-- This layer still sees the rows quarantine pulled aside, so orphans are dropped by hand.
with v as (select * from silver_coverage_versions where policy_known and tx_to is null),
latest as (
  select cov_id, any_value(policy_nbr) as policy_nbr, any_value(coverage_code) as coverage_code,
         min(valid_from) as valid_from, max(valid_to) as valid_to,
         arg_max(limit_dollars, seq) as limit_dollars, arg_max(deductible, seq) as deductible,
         bool_or(limit_was_cents) as limit_was_cents, bool_or(tz_mismatch) as tz_mismatch
  from v group by 1
), overlap as (
  select a.cov_id from latest a join latest b
    on a.policy_nbr = b.policy_nbr and a.coverage_code = b.coverage_code and a.cov_id <> b.cov_id
   and a.valid_from < b.valid_to and b.valid_from < a.valid_to
  group by 1
), booked as (
  select cov_id, cast(seq as integer) as seq,
         max(cast(seq as integer)) over (partition by cov_id order by booked_ts, cast(seq as integer)
                                         rows between unbounded preceding and 1 preceding) as best_before
  from bronze_coverage_admin
), late as (select distinct cov_id from booked where seq < best_before)
select l.*, o.cov_id is not null as overlaps_other, x.cov_id is not null as endorsed_out_of_order
from latest l left join overlap o on o.cov_id = l.cov_id left join late x on x.cov_id = l.cov_id
