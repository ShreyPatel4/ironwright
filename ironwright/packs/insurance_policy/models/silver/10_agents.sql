select agent_id, any_value(agent_name) as agent_name, any_value(agency_code) as agency_code
from bronze_agency_sheet group by 1
