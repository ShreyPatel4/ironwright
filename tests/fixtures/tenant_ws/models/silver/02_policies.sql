select policy_no, regexp_replace(trim(holder), '\s+', ' ', 'g') as holder, try_cast(premium as double) as premium
from bronze_policies
qualify row_number() over (partition by policy_no order by holder) = 1
