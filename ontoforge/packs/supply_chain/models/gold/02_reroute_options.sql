select s.tms_shipment_id,
       round(s.weight_lb * c.expedite_cost_per_lb, 2) as expedite_cost_usd,
       (select count(*) from silver_carriers a where a.scac <> s.carrier_scac and a.on_time_rate >= 0.9) as alternatives,
       (select arg_max(a.scac, a.on_time_rate) from silver_carriers a where a.scac <> s.carrier_scac) as best_alternative_scac,
       (select max(a.on_time_rate) from silver_carriers a where a.scac <> s.carrier_scac) as best_alternative_on_time
from silver_shipments_tms s join silver_carriers c on c.scac = s.carrier_scac
