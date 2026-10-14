select pog_id, cluster, cast(version as integer) as version, cast(effective_date as date) as effective_date, status
from bronze_planograms
