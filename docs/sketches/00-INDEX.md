# Sketches for the planned platform

One file per planned item, in the build order top-down-vs-bottom-up.md
ruled. A sketch is a contract, not a wish: it names the exact engine data
it reads (verified against the live store before writing), the UI as a
wireframe, the endpoints, and the gate that makes it done. A sketch with
no gate is not a sketch.

| # | Item | Map node | Reads |
|---|---|---|---|
| 01 | Discovery | sem_discovery | crosswalk, merge_log, review_queue, reconcile.json, quality checks |
| 02 | Governed object dashboards | gov_objboard | objects, links, object_history, audit_log, approvals |
| 03 | Boards (built-in BI) | op_boards | the Explorer's SQL surface |
| 04 | Org drives policy | sem_org_policy | org.json, policies.yaml |
| 05 | Escalation at any grain | gov_escalate | policy denials, data_owners, approver_chains |
| 06 | Connector ladder | src_drops .. src_cdc | source specs, one new reader per rung |
| 07 | Training mode | op_training | audit_log verdicts, pack evals |
| 08 | Small slices | op_otel, op_bi_embed, sem_asof, lake_engines, pipe_profiler | various |

WIP limit one; nothing starts before the review session closes.
