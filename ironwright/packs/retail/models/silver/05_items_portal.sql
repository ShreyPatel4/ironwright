select gtin14,
       vendor_code,
       item_name,
       cast(list_price_usd as double precision) as list_price_usd,
       cast(case_pack as integer) as case_pack
from bronze_portal_catalog
