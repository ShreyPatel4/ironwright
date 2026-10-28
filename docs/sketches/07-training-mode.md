# Sketch 07: Training mode

## Purpose

The platform learns from its operators without ever learning silently.
Approvals, denials, escalations and Ask overrides are already audit rows;
training mode replays them into labeled examples, and a retrain can serve
only after passing the pack evals plus a regression gate against prior
human verdicts.

## What it reads

- `audit_log`: the human verdicts (approved_by, denied, escalated) with
  their full decision context (action, params, objects, policy).
- The ask log: refusals the human overrode by rephrasing (a weak label),
  answers the human marked wrong (needs a feedback affordance first: one
  "this was wrong" control on Ask answers, itself an audit row).
- Pack evals as the floor any candidate must clear.

## The loop, explicit

```
observe (audit rows)  ->  label replay (no label across missing context)
     -> candidate (prompt variant or fine-tune via the model seam)
     -> GATE: pack evals green AND agreement with prior human verdicts
              not lower than the serving variant
     -> serve, with the variant id on every narrative it writes
     -> every serve is observable; the loop closes
```

Labels inherit the pulse discipline: a verdict without its full context
rows is excluded, never imputed. The training set is listable because it
IS a filtered audit query; an operator can read every example the model
learned from.

## UI

One Training panel under OPERATE: current serving variant, candidates
with their gate results (evals, agreement rate, n), a promote button that
is disabled unless both gates are green, and the full label list behind a
link. Nothing auto-promotes.

## Gate

Done when: on retail, replaying the audit log yields a label set a test
can count and audit; a deliberately bad candidate (prompt that ignores
evidence) is BLOCKED by the agreement gate; and promotion writes an audit
row naming variant, gates, and numbers.

## Not in this slice

Fine-tuning infrastructure (prompt variants first; the seam accepts both);
cross-workspace learning; any training on L1 user data without an explicit
per-workspace opt-in recorded in audit.

## Shipped slice

`ironwright/training.py`, with `tests/test_training.py` proving each claim.

Proven:

- Labels replay from the hash-chained audit log and nowhere else. Each
  needs_approval row pairs with its later applied or denied settlement,
  seq ordered, the same way the approvals inbox pairs them. A re-request
  pairs with its own settlement. A straight policy denial is a label too,
  verdict denied_by_policy with the deciding rule. Unsettled requests are
  not labels.
- Gated offline evaluation of proposed rules against the human verdicts:
  `evaluate` replays every label through the pack rules plus the proposal
  and reports agreement, with every disagreement listed by seq. `adopted`
  is always false; adoption happens only through the reviewed orgpolicy
  and escalation paths.
- `write_labels` rewrites reports/training_labels.jsonl atomically and a
  rerun over the same audit log is byte-identical.

NOT claimed: model fine-tuning, automatic adoption, online learning, or
the serve-and-promote loop sketched above. The router ships unmounted;
the console view (console/src/views/Training.jsx) ships unwired.
