with d as (select first_reported as day, count(*) as claims from gold_claim_features group by 1)
select day, claims, (select median(claims) from d) as baseline from d
