select cat_code, peril, cast(start_date as date) as start_date, cast(end_date as date) as end_date,
       len(string_split(zips, ',')) as zip_count
from bronze_cat_events
