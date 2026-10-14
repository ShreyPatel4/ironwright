select adjuster_id, name, region, cast(open_claims as integer) as open_claims, cast(authority_limit as integer) as authority_limit
from bronze_adjusters
