-- carriers deliver at least once: keep one row per event_id
select event_id, scac, shipment_ref, event_type,
       strptime(ts, '%Y-%m-%dT%H:%M:%SZ') as ts, reason_code,
       try_strptime(new_eta, '%Y-%m-%dT%H:%M:%SZ') as new_eta, location
from (select *, row_number() over (partition by event_id order by ts) rn from bronze_carrier_events)
where rn = 1
