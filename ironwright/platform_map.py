"""The platform map: every capability as a node, state derived, never posed.

LIVE nodes carry the console view they open and, where cheap, a real count.
PLANNED nodes carry what they are and what they depend on, verbatim from
docs/platform-vision.md. The test suite enforces the house rule: a node may
not claim live without a real surface behind it.
"""
from __future__ import annotations

from typing import Any

# (id, lane, label, status, view-or-depends, detail)
_NODES: list[tuple[str, str, str, str, str, str]] = [
    # lane: sources
    ("src_files", "sources", "Files: fixed width, CSV, JSONL, SQLite", "live", "data",
     "The four legacy families every pack lands through today, declared as source specs."),
    ("src_drops", "sources", "Object-store drops (S3 API)", "live", "data",
     "Kind s3: bucket objects land through the exact local reader path, byte-identical by "
     "construction and by test (moto fixture, offline). SFTP is not yet claimed."),
    ("src_jdbc", "sources", "Warehouses over ADBC", "live", "data",
     "Kind adbc: sqlite driver fixture-proven byte-identical to the native path. The "
     "postgresql driver is code-present but unclaimed until fixtured; others later."),
    ("src_sap", "sources", "SAP IDoc flat-file drops", "live", "data",
     "Kind idoc: segment-layout parsing with control records, truncation handled, unknown "
     "segments preserved raw. Flat files only; OData and RFC are later and not claimed."),
    ("src_stream", "sources", "Stream windows (recorded segments)", "live", "data",
     "Kind stream_window: recorded event segments land in deterministic windows with "
     "_window_id, idempotent re-runs. A live Kafka-protocol consumer is later, not claimed."),
    ("src_cdc", "sources", "CDC log capture", "planned", "src_stream",
     "Debezium-class capture into event sources. After streams, not before."),
    # lane: pipeline
    ("pipe_contracts", "pipeline", "Inbound contracts", "live", "pipeline",
     "Versioned, fail-loud validation on every source."),
    ("pipe_wap", "pipeline", "Bronze with write-audit-publish", "live", "loads",
     "Every write on an audit branch; checks decide publish, failures quarantine."),
    ("pipe_quality", "pipeline", "Silver and gold with quality gates", "live", "pipeline",
     "Typed, deduped, conformed; business models in SQL."),
    ("pipe_reconcile", "pipeline", "Reconciliation with known differences", "live", "pipeline",
     "Old equals new, proven, with the differences that are supposed to exist."),
    ("pipe_profiler", "pipeline", "Profiler and migration assistant", "planned", "src_files",
     "Profile any file, propose source specs and types; the human authors the mapping. C3."),
    # lane: lakehouse
    ("lake_iceberg", "lakehouse", "Iceberg lakehouse", "live", "explorer",
     "Bronze, silver, gold as Iceberg tables; read-only SQL in the console."),
    ("lake_quarantine", "lakehouse", "Quarantine", "live", "loads",
     "Failed loads land beside the data, never in it."),
    ("lake_engines", "lakehouse", "Swappable engines", "planned", "lake_iceberg",
     "Transforms are SQL; DuckDB on a laptop, Trino or Spark later without rewriting models."),
    # lane: semantic
    ("sem_ontology", "semantic", "Ontology with identity resolution", "live", "semantic",
     "Objects, links, crosswalk provenance per identity."),
    ("sem_org", "semantic", "Organizations from the scenario engine", "live", "organization",
     "Create a whole org from a spec: hierarchy, owners, approval chains. L2 by construction."),
    ("sem_org_policy", "semantic", "Org chart drives policy", "live", "orgpolicy",
     "Policy generated from data owners and approval chains, applied only through a reviewed "
     "diff with an append-only log, and consumed by every serving decision. Hand-written "
     "rules load first and win: a conflicting generated rule is dropped and the conflict "
     "is reported, not swallowed."),
    ("sem_asof", "semantic", "As-of object reads", "live", "semantic",
     "Any object readable at any past timestamp from its version history, masked like every "
     "read. Function-level as-of needs lake snapshots and is not claimed yet."),
    ("sem_discovery", "semantic", "Discovery: what the estate didn't know", "live", "discovery",
     "The one-stop-shop thesis as a surface: duplicate identities from the merge log, "
     "review-queue ambiguities, reconcile contradictions, failing checks and quarantined "
     "loads. Derived, never invented; the test proves it finds the planted facts without "
     "being told they were planted. Audit-trail structure mining remains a later increment."),
    # lane: operate
    ("op_agents", "operate", "Agents on governed tools", "live", "agents",
     "Rules decide, models narrate; every decision cited and traced."),
    ("op_ask", "operate", "Ask with honest refusals", "live", "ask",
     "Routes only to registered functions; refusal rate published; replayable."),
    ("op_models", "operate", "Model providers", "live", "ask",
     "offline, ollama, anthropic, openai, cloudflare behind one seam."),
    ("op_training", "operate", "Training mode from observed actions", "planned", "op_agents",
     "Approvals, denials and overrides are audit rows; replay them into labels, retrain gated "
     "by pack evals plus prior human verdicts. Nothing retrains silently."),
    ("op_otel", "operate", "OTLP trace export", "live", "agents",
     "Every trace exports as OTLP/HTTP JSON to any collector endpoint; ids and parent links "
     "proven round-trip against an offline fixture collector. A managed deck is not claimed."),
    ("op_bi_embed", "operate", "BI embed: Looker, Power BI, Metabase", "planned", "lake_iceberg",
     "Iframe/embed SDK over the lakehouse SQL surface."),
    ("op_boards", "operate", "Boards, the built-in BI", "live", "boards",
     "Tiles over the lakehouse: each carries its SQL, its viz and its last run time. Tiles "
     "pass the same SELECT-only guard as the Explorer, rejected at save. Stale tiles say "
     "when they ran instead of posing as current."),
    # lane: govern
    ("gov_policy", "govern", "Default-deny policy with dry runs", "live", "governance",
     "Rules in evaluation order; tester against the real engine."),
    ("gov_approvals", "govern", "Approvals through the audit chain", "live", "approvals",
     "needs_approval rows are the inbox; decisions land in the same chain."),
    ("gov_audit", "govern", "Hash-chained audit", "live", "audit",
     "Server-side verify; the same chain the public site verifies in-browser."),
    ("gov_principals", "govern", "Principals and role lenses", "live", "principals",
     "Act-as with masking and audit attribution. An identity claim, not authentication."),
    ("gov_auth", "govern", "Hosted authentication", "planned", "gov_principals",
     "An invite gate (salted local credential hashes, console door only) is live; the API "
     "stays locally open, which is why this node stays PLANNED until the hosted build closes "
     "it with real authentication end to end."),
    ("gov_escalate", "govern", "Escalation at any grain", "live", "approvals",
     "A denial raises to the owning role; a grant is a narrow expiring policy rule through "
     "the reviewed-diff path that every serving decision consults until it expires, never "
     "a bypass, and hand-written forbids still win."),
    ("gov_objboard", "govern", "Governed object dashboards", "live", "semantic",
     "Every object opens to its standing page: props, identity crosswalk, version history, "
     "the audit trail that touched it, pending approvals on it, and per-action permits from "
     "a live policy dry-run for whoever is looking."),
]

LIVE_VIEWS = {"data", "pipeline", "loads", "explorer", "semantic", "ask", "agents",
              "approvals", "audit", "governance", "principals", "organization", "discovery",
              "boards", "orgpolicy"}


def platform_map() -> dict[str, Any]:
    nodes = []
    ids = {n[0] for n in _NODES}
    for node_id, lane, label, status, ref, detail in _NODES:
        n: dict[str, Any] = {"id": node_id, "lane": lane, "label": label,
                             "status": status, "detail": detail}
        if status == "live":
            n["view"] = ref
        else:
            n["depends_on"] = ref if ref in ids else None
        nodes.append(n)
    return {"nodes": nodes,
            "lanes": ["sources", "pipeline", "lakehouse", "semantic", "operate", "govern"],
            "note": "LIVE opens the real surface. PLANNED is a concept with its dependency; "
                    "it never wears a working button. docs/platform-vision.md is the longhand."}
