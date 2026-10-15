# Sketch 01: Discovery

## Purpose

The one-stop-shop thesis with a face: tell the operator what the estate
did not know about itself. Four finding classes, each already produced by
machinery that is eval-proven on the packs. Discovery is a READ surface;
it invents nothing, ranks what exists, and every finding links to its
evidence.

## What it reads (verified shapes)

- Identity findings: `merge_log` and the export's merges
  (`{type, id, title, members[], method, confidence, provenance}`), e.g.
  Harbor Pet Foods LLC = mainframe:100207 + portal:V-HAR02 + portal:V-HAR99.
  Ambiguous pairs: `review_queue(type, a, b, score, reason, ts)`.
- Contradictions: `reconcile.json` rules, specifically
  `counts.unexplained > 0` rows and each rule's `differences[]`.
- Orphans and dead feeds: quality checks of kind structural/referential
  from the migrate report (`checks[]` with `failing_rows`), plus sources
  whose latest bronze `_run_id` is older than the workspace's last run.
- Masked-by-policy is NOT a finding; Discovery never leaks around lenses.

## UI

```
DISCOVERY                                                12 findings
--------------------------------------------------------------------
[ All | Identity 6 | Contradiction 1 | Orphans 3 | Dormant 2 ]

IDENTITY   Harbor Pet Foods LLC is three records            conf 1.00
           mainframe:100207 · portal:V-HAR02 · portal:V-HAR99
           merged by exact:tax_id -> open object | see crosswalk

CONTRA     Daily units vs mainframe nightly: 1 explained,  rule page
           0 unexplained after tolerance                   -> evidence

ORPHAN     planogram_positions references item 400000507   -> rows
           removed by planogram A v2
--------------------------------------------------------------------
every row: finding · evidence link · the check or method that found it
```

Each row opens a panel: the finding in one sentence, the evidence rows,
the method (`exact:tax_id`, rule name, check name), and where it leads
(object dashboard, reconcile rule, source).

## Endpoints

- `GET /workspaces/{ws}/discovery` -> `{findings: [{class, title, severity,
  method, evidence_ref, confidence?}], counts}`; pure derivation, no store.
- Reuses existing object/reconcile/loads endpoints for the evidence links.

## Gate

Done when: on retail, Discovery lists the planted vendor duplicates and
the planogram orphan WITHOUT knowing they were planted (derivation reads
stores and reports only), and a test asserts the planted set is a subset
of the findings. On a created org (no planted anything), Discovery says
"nothing found" rather than inventing noise.

## Not in this slice

Audit-trail structure mining (de facto approval paths); cross-workspace
findings; any writeback.
