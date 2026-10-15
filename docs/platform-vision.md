# The full platform, as a concept with a build order

Status: CONCEPT, 2026-10-14, on the house rule that a concept never wears a
working button. Everything here is rendered on the console's Platform map
with one of two states, derived from code: LIVE (opens the real surface)
or PLANNED (shows what it is and what it depends on). The map endpoint has
a test that refuses a node claiming to be live without a real surface
behind it.

## Connectors: any source, one landing path

Everything lands through the same declarative source spec into bronze with
write-audit-publish. The taxonomy, in build order of cheapest-true first:

| Family | Protocols | State |
|---|---|---|
| Files | fixed width (copybook), CSV, JSONL events, SQLite | LIVE |
| Drops | SFTP, object stores (S3, GCS, R2) | planned: same specs, remote reader |
| Warehouses | JDBC/ADBC (Postgres, SQL Server, Oracle, Snowflake, BigQuery) | planned: Arrow ADBC readers |
| ERP / SAP on prem | OData extractors, IDoc file drops first; RFC/BAPI via adapter later | planned: IDoc is a copybook cousin, files first |
| Streams | Kafka-protocol consumer, batch windows into bronze | planned |
| CDC | Debezium-class log capture into event sources | planned, after streams |

The rule that keeps this honest: a connector ships only with a replayable
fixture and a contract test; no connector is listed until it moves real
bytes on a laptop.

## Agents and models as connectors

The model seam already exists (offline, ollama, anthropic, openai,
cloudflare; the engine treats narration and routing as pluggable). The
concept grows it into a registry: each agent binds a use case, a toolset
(ontology functions only), a model provider, and an eval set. Adding an
"agent connector" means adding a use case spec plus permits, never code
with open tool access.

## The feedback and training loop

Observed user actions become labeled data, deliberately and visibly:

1. Every approval, denial, and Ask refusal override is already an audit row.
2. Training mode replays those rows into labels (decision, context, human
   verdict) with the same gap discipline the labels in pulse had: no label
   across missing context.
3. A retrain produces a candidate; the candidate passes the pack evals plus
   a regression gate against prior human verdicts before it can serve.
4. Everything the loop learns from is listable, because it is the audit log.

Planned; depends on the eval harness per use case and a model seam that
accepts fine-tuned or prompted variants. Nothing retrains silently, ever.

## Observability

Traces are already OTel-shaped spans on disk. The deck is an exporter plus
dashboards: OTLP out to any collector (Grafana, Jaeger, Datadog), and the
console's own waterfall stays the zero-setup default. Planned: exporter.

## BI

Two doors, in order:
1. Embed what the org already pays for: Looker, Power BI, Metabase by
   iframe/embed SDK over the lakehouse's SQL surface. Planned.
2. Boards, the built-in: pin any Explorer query as a tile, lay out tiles on
   a canvas, every tile carries its SQL and its freshness. Planned; the
   Explorer is the engine for it.

## Escalation at any grain

The approval chain generalizes: any denied or masked access can raise an
escalation to the role that owns the object (data owners exist per object
type; chains exist per action). An escalation is an audit row, lands in the
owner's inbox, and its grant is itself policy, never a bypass. Planned;
depends on org-driven policy below.

## Team and org as the governance source

Created organizations already generate hierarchy, data owners and approver
chains. The slice that closes the loop: generate policies.yaml from the org
chart, so "who may do what" is derived from "who owns what", and editing
the org updates governance through a reviewed diff. Planned (the governance
slice the scenario work always pointed at).

## Governed object dashboards

Every ontology object gets a standing page: its state, its links, every
agent that touched it, every pending approval on it, every audit row about
it. The object browser already shows props, crosswalk, neighbors; the
dashboard adds the operational trail. Planned; cheap, high value, early.

## Order of pursuit

Boards and object dashboards and org-to-policy are weeks and all-Python.
Connector families land cheapest-true first (drops, JDBC, IDoc files).
Training loop and CDC are the long poles and wait for the eval harness and
streams respectively. The map stays the single truth of what is which.

## The one-stop-shop thesis (added 2026-10-14, ruling in top-down-vs-bottom-up.md)

The benefit an owner feels first is not a pipeline; it is being told
something true about their own estate that nobody knew: the vendor that
exists twice, the records that cannot both be right, the feed that died
quietly, the approval path everyone uses but nobody wrote down. The engine
already finds all four classes on synthetic estates and proves it with
evals. The Discovery surface makes those findings first-class, and it
jumped the build queue because it needs no new connectors, only a face.
