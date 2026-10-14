-- the TMS writes SO-0012345; strip to the digits every system shares
select tms_shipment_id,
       ltrim(regexp_replace(order_ref, '[^0-9]', '', 'g'), '0') as order_digits,
       carrier_scac, origin_dc, dest_city,
       strptime(ship_date, '%Y-%m-%d %H:%M') as ship_ts,
       strptime(promised_eta, '%Y-%m-%d %H:%M') as promised_eta,
       strptime(current_eta, '%Y-%m-%d %H:%M') as tms_eta,
       mode, cast(weight_lb as integer) as weight_lb
from bronze_tms_shipments
