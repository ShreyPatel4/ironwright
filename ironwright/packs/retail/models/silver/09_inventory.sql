-- the snapshot date rides in the file name; pull the 8 digits out and read them as a date
select store_nbr, item_nbr,
       cast(substr(d8, 1, 4) || '-' || substr(d8, 5, 2) || '-' || substr(d8, 7, 2) as date) as snapshot_date,
       cast(on_hand_qty as integer) as on_hand
from (
  select STORE_NBR as store_nbr, ITEM_NBR as item_nbr, ON_HAND_QTY as on_hand_qty,
         regexp_replace(_source_file, '^[^0-9]*([0-9]{8}).*$', '\1') as d8
  from bronze_inventory
) named
