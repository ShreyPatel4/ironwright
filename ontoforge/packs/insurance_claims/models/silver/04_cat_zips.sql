select cat_code, peril, cast(start_date as date) as start_date, cast(end_date as date) as end_date, unnest(string_split(zips, ',')) as zip
from bronze_cat_events
