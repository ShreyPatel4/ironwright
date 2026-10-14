-- every report is kept; duplicates are resolved into one Claim in the ontology, not deleted here
select f.claim_ref, ltrim(regexp_replace(f.policy_ref, '[^0-9]', '', 'g'), '0') as policy_digits, p.policy_nbr,
       cast(f.loss_date as date) as loss_date, f.peril, f.loss_zip, cast(f.est_amount as double) as est_amount,
       f.channel, cast(f.reported_date as date) as reported_date, f.description,
       ltrim(regexp_replace(f.policy_ref, '[^0-9]', '', 'g'), '0') || '|' || f.loss_date || '|' || f.peril as dedupe_key
from bronze_fnol f
left join (select policy_nbr, ltrim(policy_nbr, '0') d from bronze_policy_master) p
  on p.d = ltrim(regexp_replace(f.policy_ref, '[^0-9]', '', 'g'), '0')
