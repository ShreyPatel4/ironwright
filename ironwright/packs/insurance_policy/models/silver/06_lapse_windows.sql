-- Billing is the only system that records a lapse. A window stays open until a reinstatement closes it.
select l.policy_nbr, l.event_date as lapse_date,
       (select min(r.event_date) from silver_billing_events r where r.policy_nbr = l.policy_nbr
          and r.event_type = 'REINSTATE' and r.event_date > l.event_date) as reinstate_date
from silver_billing_events l where l.event_type = 'LAPSE'
