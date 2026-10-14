-- online banking writes "First Last" (sometimes "First M. Last", sometimes uppercase) and MM/DD/YYYY
select ob_user_id, full_name,
       lower(list_last(string_split(trim(full_name), ' '))) as last_name,
       strptime(dob, '%m/%d/%Y')::date as dob, email,
       lower(list_last(string_split(trim(full_name), ' '))) || '|' || strftime(strptime(dob, '%m/%d/%Y'), '%Y%m%d') as match_key
from bronze_olb_users
