-- carrier events beat the TMS for ETA: the TMS value is often stale after a delay
with last_issue as (
  select shipment_ref, event_type, reason_code, new_eta, ts,
         row_number() over (partition by shipment_ref order by ts desc) rn
  from silver_carrier_events where event_type in ('DELAY', 'EXCEPTION')
),
delivered as (select distinct shipment_ref from silver_carrier_events where event_type = 'DELIVERED'),
last_any as (
  select shipment_ref, arg_max(event_type, ts) as latest_event_type, max(ts) as latest_event_ts
  from silver_carrier_events group by 1
)
select s.tms_shipment_id, s.order_digits, s.carrier_scac, s.origin_dc, s.dest_city, s.ship_ts,
       s.promised_eta, s.tms_eta,
       coalesce(li.new_eta, s.tms_eta) as latest_eta,
       round(date_diff('minute', s.promised_eta, coalesce(li.new_eta, s.tms_eta)) / 60.0, 1) as delay_hours,
       d.shipment_ref is not null as delivered,
       la.latest_event_type, li.event_type as issue_type, li.reason_code as latest_reason,
       li.new_eta is not null and s.tms_eta < li.new_eta - interval 1 hour as tms_eta_stale,
       o.priority, o.order_value_usd, o.customer_id, s.weight_lb, s.mode
from silver_shipments_tms s
left join last_issue li on li.shipment_ref = s.tms_shipment_id and li.rn = 1
left join delivered d on d.shipment_ref = s.tms_shipment_id
left join last_any la on la.shipment_ref = s.tms_shipment_id
left join silver_orders_erp o on o.order_digits = s.order_digits
