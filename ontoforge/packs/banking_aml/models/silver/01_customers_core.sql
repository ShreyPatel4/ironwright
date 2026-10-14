-- core writes "LAST, FIRST M"; build a comparable full name and a last-name+DOB match key
with parsed as (
  select *,
         trim(split_part(trim(split_part(cust_name, ',', 2)), ' ', 1)) as first_raw,
         trim(split_part(cust_name, ',', 1)) as last_raw
  from bronze_customer_master
)
select cust_nbr,
       case when cust_type = 'I'
            then upper(left(first_raw, 1)) || lower(substr(first_raw, 2)) || ' ' || upper(left(last_raw, 1)) || lower(substr(last_raw, 2))
            else cust_name end as full_name,
       lower(last_raw) as last_name,
       strptime(dob, '%Y%m%d')::date as dob,
       address, cust_type, nullif(industry, '') as industry, risk_rating,
       strptime(kyc_date, '%Y%m%d')::date as kyc_date,
       case when cust_type = 'I' then lower(last_raw) || '|' || dob end as match_key
from parsed
