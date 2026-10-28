-- one row per store; status decoded, dates parsed
-- portable date parse: an 8 digit string becomes ISO text, then a date; anything else is null
select store_nbr,
       trim(store_name) as store_name,
       region,
       cluster,
       case status when 'O' then 'OPEN' when 'C' then 'CLOSED' end as status,
       cast(case when open_date similar to '[0-9]{8}'
                 then substr(open_date, 1, 4) || '-' || substr(open_date, 5, 2) || '-' || substr(open_date, 7, 2)
            end as date) as open_date
from bronze_store_master
