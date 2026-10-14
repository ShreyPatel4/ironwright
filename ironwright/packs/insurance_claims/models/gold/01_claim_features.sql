-- one row per real claim (policy + loss date + peril), however many times it was reported
with claims as (
  select dedupe_key, min(claim_ref) as first_ref, count(*) as reports, any_value(policy_nbr) as policy_nbr,
         any_value(loss_date) as loss_date, any_value(peril) as peril, any_value(loss_zip) as loss_zip,
         max(est_amount) as est_amount, min(reported_date) as first_reported
  from silver_claims_fnol group by 1
)
select c.*, p.eff_date, p.exp_date, p.deductible, p.coverage_limit, p.perils,
       c.loss_date between p.eff_date and p.exp_date as in_period,
       list_contains(string_split(p.perils, ','), c.peril) as peril_covered,
       date_diff('day', p.eff_date, c.loss_date) as days_since_policy_start,
       (select count(*) from silver_prior_claims pc where pc.policy_nbr = c.policy_nbr
          and pc.loss_date > c.loss_date - interval 3 year) as prior_claims_3y,
       z.cat_code,
       z.cat_code is not null as in_cat,
       greatest(c.est_amount - p.deductible, 0) as net_amount
from claims c
left join silver_policies p on p.policy_nbr = c.policy_nbr
left join silver_cat_zips z on z.zip = c.loss_zip and z.peril = c.peril and c.loss_date between z.start_date and z.end_date
