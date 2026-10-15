# Sketch 04: The org chart drives policy

## Purpose

The governance keystone. Today a created organization carries data owners
and approver chains as facts; policy lives separately in each pack's
policies.yaml. This slice derives policy FROM the org, so "who may do
what" follows from "who owns what", and an org edit becomes a governance
change through a reviewed diff, never a silent one.

## What it reads and writes

- Reads `org.json`: `data_owners{object_type: role}`,
  `approver_chains[{action, chain[]}]`, `roles[{id, approves[]}]`.
- Emits `policies.generated.yaml` in the workspace: permits for owners on
  their object types' actions, approval requirements wired to the chain's
  first role, forbids for irreversible actions without a chain. The
  existing PolicyEngine consumes it unchanged.
- Never edits a pack's hand-written policies.yaml; packs keep theirs, and
  a generated file alongside loses to the hand-written one on conflict,
  loudly.

## UI

```
GOVERNANCE · generated from the org             [regenerate preview]
--------------------------------------------------------------------
DIFF  + permit  role:space_planning_lead  action:propose_facing_change
      + require-approval  delist_item  chain cmo -> ceo
      - (nothing removed)
      [apply]  [discard]
applied generations are audit rows; the diff is the review
--------------------------------------------------------------------
```

## Gate

Done when: generating from a created org yields a policy set under which
the Governance dry-run tester gives the same verdicts the org chart
implies (owner permitted, non-owner denied, chain's approver required),
asserted as a test over every role in the org; and regeneration after an
org edit shows a correct diff, applied only on click, recorded in audit.

## Not in this slice

Editing the org in the console (regeneration consumes org.json as-is);
packs switching to generated policy.
