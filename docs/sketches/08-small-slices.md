# Sketch 08: The small slices

Each of these is days, not weeks, and none blocks the queue above.

## Observability exporter (op_otel)

Spans are already OTel-shaped dicts on disk. The slice: an OTLP/HTTP
exporter (`ironwright export-traces --endpoint ...`) mapping our span
fields onto OTel semantics, plus a console setting panel that only shows
"connected" after a real export round-trips. Gate: spans visible in a
local Jaeger started by the fixture script, trace ids intact.

## BI embed (op_bi_embed)

An iframe panel per board-capable tool (Metabase first: open source, can
fixture honestly; Looker/Power BI documented but untested until someone
has a license). The embed points at the lakehouse through each tool's own
connection; we never proxy credentials. Gate: a fixtured local Metabase
renders a chart over retail gold inside the console, documented click by
click. If no tool can be fixtured honestly, Boards (sketch 03) is the
answer and this slice stays planned.

## As-of everywhere (sem_asof)

`object_history` already versions every object. The slice: `as_of`
parameter on object reads and ontology functions, resolving each object
to its version at t; Iceberg snapshots give the lake the same read. Gate:
a test reads Harbor Pet Foods at v1 and v3 and gets different, correct
props, and a function evaluated as-of a past run matches that run's
report.

## Engine swap (lake_engines)

Transforms are SQL already. The slice is a contract test suite that runs
every silver/gold model against DuckDB and one more engine (Trino in a
fixture container) and diffs results; the console gains an engine picker
only after the diff is empty on all packs. Gate: the diff report, green.

## Profiler (pipe_profiler)

Points at sketch C3 in platform-architecture.md: profile files, propose
source specs, human authors the mapping. Stays gated behind C4m (one real
estate mapped by hand) per the standing ruling; this file exists so the
map node has a sketch to link.
