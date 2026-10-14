select STORE_NBR as store_nbr, ITEM_NBR as item_nbr,
       strptime(regexp_extract(_source_file, '(\d{8})', 1), '%Y%m%d')::date as snapshot_date,
       cast(ON_HAND_QTY as integer) as on_hand
from bronze_inventory
