# Top down meets bottom up: the requirements, reconciled

Status: 2026-10-14. The top-down column is the owner's requirement set,
verbatim in spirit. The bottom-up column is what the repo provably does
today. The gap column is honest, and every gap names the slice that closes
it. This document and the Platform map must agree; when they drift, the
map's test wins.

## The thesis that sits above every requirement

One stop shop for any org to understand itself, including the relations
its owners do not know about. This is not aspiration; it is what the
engine already does on every synthetic run: it finds the vendor registered
twice under different ids, the item whose tax id contradicts its portal
record, the planogram version flip that silently orphans sales rows, the
reference that points at nothing. On packs those discoveries are planted
and eval-checked, which is how we know the machinery finds them. The
platform promise is the same machinery pointed at an estate nobody
sabotaged on purpose, where the findings are real and nobody planted them.

The discovery surfaces, in value order:
1. Identity: who is actually the same entity across systems (crosswalk,
   merges, review queue; exists today, engine-side).
2. Contradiction: records that cannot both be true (reconcile with known
   differences; exists today as a per-table report).
3. Orphans and dormancy: references to nothing, data nobody reads, feeds
   that stopped (partially exists as quality checks; needs a surface).
4. Structure the org never wrote down: the de facto approval paths and
   data flows mined from the audit trail (planned; the audit log is the
   substrate and already exists).

## The matrix

| Top-down requirement | Bottom-up state today | Gap | Closes in |
|---|---|---|---|
| Discover unknown relations (the thesis) | Engine finds planted identity/contradiction/orphan defects, eval-proven; merges + review queue in stores | No console surface for discoveries | Insights surface: a Discovery tab fed by crosswalk merges, review queue, reconcile exceptions. Early, all-Python, high value |
| Databricks/Foundry-grade UI | Console: 13 surfaces, Direction A light, canvas map, zero posed buttons | Boards, object dashboards, richer explorer (schema browser, history) | Boards + governed object dashboards slices |
| Connect any source (SAP, warehouses, streams, CDC) | 4 file families live through one declarative path | Remote readers, ADBC, IDoc/OData, Kafka, Debezium | Connector ladder, cheapest-true first; fixture + contract test per connector or it is not listed |
| Agent + model connectors | 5 model providers behind one seam; agents bound to governed toolsets | Registry UX; per-agent eval sets | Agent registry slice after Ask hardening |
| Evals + feedback loop + retrain from user actions | Every human decision is already an audit row; pack evals gate everything | Label replay, retrain gate, regression against prior human verdicts | Training mode; never silent, spec'd in platform-vision.md |
| Observability deck | OTel-shaped spans on disk; console waterfall | OTLP exporter to Grafana/Jaeger/Datadog | Exporter slice, small |
| BI (Looker/Power BI) or built-in | Read-only SQL explorer on the lakehouse | Embed SDKs; Boards (pin query as tile) | Boards first: it is the Explorer plus layout, weeks not months |
| Escalation at any grain | Approval chains per action; denials are audit rows | Escalate-on-deny to owning role; grants as policy | After org-drives-policy |
| Team / org chart setup | Scenario engine generates org, owners, chains; console creates orgs | Org edits; org generates policies.yaml | Org-to-policy slice (the governance keystone) |
| Governed dashboard per object | Object browser: props, crosswalk, neighbors | State + agent touches + approvals + audit trail per object | Object dashboard slice; cheap, early |
| Ultimate flexibility | Packs are data; sources declarative; SQL transforms; model seam; principals act-as | Plugin points not yet documented as a contract | Extension contract doc when first external user exists |

## How the two plans differ, and the ruling

Top-down orders by imagined demo impact: connectors and BI first, because
that is what platforms sell with. Bottom-up orders by provable truth per
hour: semantic depth and surfaces over the data we already trust. The
ruling stands as the strategy passes set it, with one amendment the thesis
forces: the Discovery surface jumps the queue. It is the one-stop-shop
claim made visible, it needs zero new connectors, and it turns machinery
we already trust into the benefit an owner feels in the first minute.

Build order after the review session, WIP-1 as always:
1. Discovery surface (identity merges, review queue, reconcile exceptions,
   on packs and created orgs alike).
2. Governed object dashboards.
3. Boards.
4. Org-to-policy, then escalation at grain.
5. Connector ladder begins (drops, then ADBC), each behind a fixture.
6. Training mode, once per-use-case evals harden.

Public claims ceiling unchanged: nothing on coconutlabs.org names a
capability until its node on the map is LIVE.
