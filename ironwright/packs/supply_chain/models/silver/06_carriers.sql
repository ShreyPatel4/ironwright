select scac, carrier_name, cast(on_time_rate as double) as on_time_rate,
       cast(expedite_cost_per_lb as double) as expedite_cost_per_lb
from bronze_tms_carriers
