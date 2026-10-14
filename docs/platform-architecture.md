# Platform architecture: from demo engine to a usable migration platform

Status: SETTLED 2026-10-14 after two strategy passes. Nothing here ships as a
public claim until it exists and is measured.

## What this platform is

A business with old systems says: I have mainframe files, aging databases, CSV
drops, and nobody fully knows how it fits together. I want a modern stack and
more out of what I have. Migrating that today takes a whole data organization:
engineers, ops, analysts, DBAs, scientists, governance, compliance, secops,
serving. Ironwright does that work step by step, as a product, with an ontology
built from the ground up over the migrated estate, and an agent layer that can
act on it behind policy and approval.

The demos stay synthetic: four fictional companies with planted defects and
ground truth, so every mechanism is provable. Real estates run locally first.

## The migration playbook is the product

Each step below is what a team would do by hand. The platform does it as a
gated stage with evidence. The engine already runs most of this spine for the
synthetic packs; generalizing it is the work.

| Step | Who does this manually | Platform stage | Exists today |
|---|---|---|---|
| Inventory and profile the estate | DE + DBA | source registry, profiler | partial (legacy specs) |
| Pin inbound contracts | DE | versioned contracts, fail-loud validation | yes, per pack |
| Land raw | DE | bronze, write-audit-publish | yes |
| Type, dedupe, conform | DE + analysts | silver + quality gates | yes |
| Business metrics | analytics | gold models | yes |
| Prove old equals new | ops + controllers | reconciliation with known differences | yes |
| Who is who across systems | data stewards | identity resolution + crosswalk | yes |
| Name the business objects | the whole org, badly | ontology: objects, links, functions, actions | yes |
| Who may do what | governance + compliance | default-deny policy, approval chains | yes |
| Prove what happened | audit + secops | hash-chained audit, replayable traces | yes |
| Ask and act | analysts + operators | agents behind policy; Ask surface | agents yes, Ask planned |
| Serve it out | serving teams | REST + MCP; query surfaces | yes, single-pack |
| Keep it running | ops | orchestration, lineage, monitoring | thin |

## One direction of dependency, no services

The architecture keeps the four layers the docs already name (legacy, lake,
ontology, application). No second vocabulary, no service mesh, no gRPC, no
process split until two processes genuinely must exist. What is enforced is
the direction:

- The data side (legacy, lake, quality, reconcile) never imports the semantic
  side (ontology, policy, agents, llm).
- The semantic side touches the data side only through the lakehouse facade,
  never raw sources or pipeline internals.
- Both rules are machine-checked by an AST test inside the ordinary suite
  (tests/test_architecture.py), so a violating import fails CI the moment it
  is written.

Iceberg is already the data/semantic contract and SQL-authored transforms
already make the execution engine swappable (DuckDB on a laptop, bigger
engines later). That is most of the strangler value with none of the tax.

## The systems language: Rust, as a library, benchmark-gated

Decision: when native speed is needed, it arrives as a Rust crate with a
Python API (the polars / pydantic-core / delta-rs shape), never a sidecar
service. Data crosses the boundary as Arrow IPC stream bytes read with
pyarrow.ipc: one memcpy, no cross-toolchain arrow-version coupling. The
extension ships as an optional extra (pip install "ironwright[fast]"); CI
runs the whole suite with the extension off by default and on in a second
job, so a Rust failure is structurally incapable of blocking the product.

First candidate: the fixed-width/CSV reader plus a one-pass column profiler.
Its landing gate, in full, before any Rust is written into main:

- Corpus: the committed seeded generator (scripts/bench_legacy_reader.py,
  sha256-pinned) plus at least one public dataset (candidate: NOAA
  GHCN-Daily .dly fixed-width archive), so the number is not self-dealt.
- The Python number it must beat, measured first and published here:
  **32.0 MB/s, 346,456 rows/s** (p50 of 5 warm-cache runs over a 100 MB /
  1,081,007-row seeded corpus, record width 96, Python 3.12.13, Apple
  M1 Pro, 2026-10-14).
- Threshold: 10x or it does not land. 2x does not justify a second toolchain.
- Correctness: byte-identical to the Python path under the repo's
  sort_keys serialization on all four packs plus 20 scenario seeds, and
  differential fuzz on malformed input (truncated lines, embedded nulls,
  short final records).
- Memory: peak RSS at or below the Python path.
- Scope note: EBCDIC and packed-decimal are a separate later increment. The
  Python reference is latin-1, so the equivalence gate cannot cover EBCDIC;
  that increment needs its own reference or a copybook round-trip fixture
  first.

## Priorities: the product is the semantic layer

Two goals, named so neither pretends to be the other. "Trustworthy
reconciliation and governance" is the product goal; nobody adopts a
migration platform for ingest throughput. "Fast native core" is a hiring
receipt that runs beside the main line, gated as above. Ranked order of
build: console first (it makes everything showable), then semantic depth
(Ask, role lenses with field-level masking, as-of everywhere, evidence
packs), then the Rust codec. The scenario engine stays parked at slice 1
as the how-ground-truth-scales story.

## Guards against scope-as-identity

The vision has no terminal state, so the guards are mechanical:

- make demo-check is a required CI job with a wall-clock bound; the
  90-second narrative (docs/DEMO-SCRIPT.md) must survive every refactor.
- Any phase of work must change DEMO-SCRIPT.md in the same PR or it is not
  a phase.
- WIP limit: one phase in flight at a time.
- Workspace isolation is an asserted invariant
  (tests/test_workspace_isolation.py), not a promised one.

## Distribution and SaaS posture

Local first: one install, runs on a laptop, data never leaves the machine.
That is the v1 promise and most of the v1 threat model. The service contracts
are the SaaS hedge: hosted later means deploying the same services with real
authn, isolation and a rewritten threat model, not re-architecting. Until that
day the public claims never say hosted, multi-tenant, enterprise, or secure
for regulated data. X-Principal is a header, not an identity provider; policy
is a correctness control today, not a security control.

## Verification levels (mechanical, non-dismissible)

Derived from artifacts on disk, never hand-set:

- L3: ground truth present and evals green. The four packs.
- L2: generated scenario, structural invariants only. Zero instances today;
  exists as a level so nobody confuses generated with proven.
- L1: your files. Structural and reconciliation checks you defined. No ground
  truth, and the badge says so on every surface that shows your data,
  including every Ask answer.

## Sequencing (carries the earlier console verdict forward)

1. C0 spine: workspace registry (swap, not pool), run endpoints over existing
   stages, approvals over REST, audit verify, verification-level derivation.
2. C1 console (React): workspaces, pipeline DAG, data explorer, ontology
   browser and org chart, approvals inbox, audit.
3. C2 Ask on the synthetic packs only: grounded answers composed of ontology
   function calls, evidence ids on every answer, an abstention path with its
   own eval and a published refusal rate, full replayability. Untrusted text
   never enters a prompt in this slice.
4. C4m: one real first-party workspace, mapped by hand. First real bytes, so
   the security rewrite gates it. Proof that the path works before any wizard.
5. C3 migration assistant: profile files, propose source specs and types,
   human authors the mapping with assistance. Never auto-magic.
6. First service extraction, benchmark-gated, per the plane rules above.

## What does not change

Evals gate everything. Rules decide, the LLM narrates. Nothing irreversible
before interrupt. Default deny. Packs are data, the engine is generic.
Synthetic demos; local real data stays local and is untrusted at every prompt
boundary.
