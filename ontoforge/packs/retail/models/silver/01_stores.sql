-- one row per store; status decoded, dates parsed
select store_nbr,
       trim(store_name) as store_name,
       region,
       cluster,
       case status when 'O' then 'OPEN' when 'C' then 'CLOSED' end as status,
       try_strptime(open_date, '%Y%m%d')::date as open_date
from bronze_store_master
