select policy_nbr, ltrim(policy_nbr, '0') as policy_digits, holder_name, zip, perils,
       strptime(eff_date, '%Y%m%d')::date as eff_date, strptime(exp_date, '%Y%m%d')::date as exp_date,
       cast(coverage_limit as integer) as coverage_limit, cast(deductible as integer) as deductible, status
from bronze_policy_master
