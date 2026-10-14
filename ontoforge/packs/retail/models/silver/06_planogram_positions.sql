-- positions joined to their planogram header so every row carries its effective date
select p.pog_id, h.cluster, cast(p.version as integer) as version,
       cast(h.effective_date as date) as effective_date,
       p.upc, p.fixture, cast(p.shelf as integer) as shelf, cast(p.facings as integer) as facings
from bronze_planogram_positions p
join bronze_planograms h on h.pog_id = p.pog_id and h.version = p.version
