# Sketch 05: Escalation at any grain

## Purpose

Any denial becomes a door instead of a wall: a denied action or a masked
field can be raised to the role that owns the object, and the grant, if
given, is itself policy applied through the normal path. No bypasses
exist, because an escalation that skipped policy would be the vulnerability
this platform exists to prevent.

## Depends on

Sketch 04. The owning role must be derivable (data_owners), and grants
must be expressible as policy the engine already evaluates.

## Mechanics

1. A deny (policy) or a mask (lens) in any surface renders "escalate to
   {owning role}" beside the denial, carrying the exact decision context
   (principal, action/field, object, denying rule).
2. Escalation = an audit row (`status: escalated`) plus an inbox entry for
   the owner role, same machinery as approvals.
3. The owner's grant emits a narrow permit (this principal, this action,
   this object id, expiry) into the generated policy file via the Sketch
   04 diff flow; deny closes it with a reason. Both are audit rows.
4. Expiry is enforced by the policy's `when` clause, not a cron.

## UI

```
denied: delist_item on ITM-a7189b9f
rule no-agent-delisting                [escalate to cmo]
--------------------------------------------------------------------
APPROVALS (cmo)     escalation · user:cm.2 wants delist_item
                    on ITM-a7189b9f · denied by no-agent-delisting
                    [grant until 2026-11-01] [deny with reason]
```

## Gate

Done when: a denied principal escalates, the owner grants, the SAME
request now passes the real PolicyEngine until expiry and fails after,
and all four moments (deny, escalate, grant, expire) are rows in the
hash-chained audit, asserted by test.

## Not in this slice

Notification transport (the inbox is the console; email is a connector
question); multi-step grant chains.
