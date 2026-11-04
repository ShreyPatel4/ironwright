-- Spreadsheet dates are M/D/YYYY and identifiers are masked to the last four.
select agency_code, agent_id, agent_name, client_key, client_name, policy_nbr,
       cast(strptime(dob, '%-m/%-d/%Y') as date)::varchar as dob,
       right(gov_id, 4) as gov_last4,
       md5(cast(strptime(dob, '%-m/%-d/%Y') as date)::varchar || '|' || right(gov_id, 4)) as match_key
from bronze_agency_sheet
