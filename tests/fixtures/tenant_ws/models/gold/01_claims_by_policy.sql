select p.policy_no, p.holder, count(c.claim_id) as claims, coalesce(sum(c.amount), 0) as total_amount
from silver_policies p left join silver_claims c on c.policy_ref = p.policy_no
group by 1, 2
