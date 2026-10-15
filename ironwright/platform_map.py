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
    ("src_drops", "sources", "SFTP and object-store drops", "planned", "src_files",
     "Same source specs, remote readers (SFTP, S3/GCS/R2). Ships only with a replayable fixture."),
    ("src_jdbc", "sources", "Warehouses over JDBC/ADBC", "planned", "src_files",
     "Postgres, SQL Server, Oracle, Snowflake, BigQuery via Arrow ADBC readers."),
    ("src_sap", "sources", "ERP and SAP on prem", "planned", "src_drops",
     "IDoc file drops first (a copybook cousin), OData extractors next, RFC/BAPI adapter later."),
    ("src_stream", "sources", "Streams (Kafka protocol)", "planned", "src_jdbc",
     "Batch windows into bronze first; consumers with replayable fixtures."),
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
    ("sem_org_policy", "semantic", "Org chart drives policy", "planned", "sem_org",
     "Generate policies.yaml from data owners and chains; edits arrive as reviewed diffs."),
    ("sem_asof", "semantic", "As-of everywhere", "planned", "sem_ontology",
     "Point-in-time reads across objects and functions."),
    ("sem_discovery", "semantic", "Discovery: what the estate didn't know", "planned", "sem_ontology",
     "The one-stop-shop thesis as a surface: duplicate identities (crosswalk merges), "
     "contradictions (reconcile exceptions), orphans and dormant feeds, and the de facto "
     "structure mined from the audit trail. The machinery exists and is eval-proven on packs; "
     "this gives its findings a face. First in the post-review build order."),
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
    ("op_otel", "operate", "Observability deck (OTLP out)", "planned", "op_agents",
     "Spans are already OTel-shaped; exporter to Grafana/Jaeger/Datadog. Console waterfall stays."),
    ("op_bi_embed", "operate", "BI embed: Looker, Power BI, Metabase", "planned", "lake_iceberg",
     "Iframe/embed SDK over the lakehouse SQL surface."),
    ("op_boards", "operate", "Boards, the built-in BI", "planned", "lake_iceberg",
     "Pin any Explorer query as a tile; every tile carries its SQL and freshness."),
    # lane: govern
    ("gov_policy", "govern", "Default-deny policy with dry runs", "live", "governance",
     "Rules in evaluation order; tester against the real engine."),
    ("gov_approvals", "govern", "Approvals through the audit chain", "live", "approvals",
     "needs_approval rows are the inbox; decisions land in the same chain."),
    ("gov_audit", "govern", "Hash-chained audit", "live", "audit",
     "Server-side verify; the same chain the public site verifies in-browser."),
    ("gov_principals", "govern", "Principals and role lenses", "live", "principals",
     "Act-as with masking and audit attribution. An identity claim, not authentication."),
    ("gov_escalate", "govern", "Escalation at any grain", "planned", "sem_org_policy",
     "Any denied or masked access can raise to the owning role; the grant is itself policy."),
    ("gov_objboard", "govern", "Governed object dashboards", "planned", "sem_ontology",
     "Per object: state, links, agent touches, pending approvals, audit trail."),
]

LIVE_VIEWS = {"data", "pipeline", "loads", "explorer", "semantic", "ask", "agents",
              "approvals", "audit", "governance", "principals", "organization"}


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
