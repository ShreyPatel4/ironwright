"""Iceberg medallion lakehouse with Write Audit Publish.

bronze  append-only, every legacy run is its own snapshot (replayable for reconciliation)
silver  cleaned, conformed, one row per real record (backs ontology objects)
gold    aggregates and metrics (backs ontology functions)

Every write goes to an Iceberg branch first, quality checks run against that
branch, and main is fast-forwarded only if no blocking check fails. A failed
audit drops the branch, so readers of main never see unchecked data.

AWS mapping: catalog = Glue Data Catalog or S3 Tables, warehouse = S3,
transforms = Glue/EMR Spark or Athena, checks = Glue Data Quality.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NoSuchTableError

from .legacy import read_source
from .packs import Pack
from .quality import CheckResult, blocking_failures, run_checks
from .tracing import Tracer

LAYERS = ("bronze", "silver", "gold")


@dataclass
class WriteResult:
    table: str
    rows: int
    published: bool
    snapshot_id: int | None
    branch: str
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "rows": self.rows, "published": self.published,
                "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None, "branch": self.branch,
                "checks": [c.to_dict() for c in self.checks]}


class Lakehouse:
    def __init__(self, pack: Pack):
        self.pack = pack
        p = pack.paths.ensure().lake
        (p / "warehouse").mkdir(parents=True, exist_ok=True)
        self.catalog = SqlCatalog(f"ironwright_{pack.name}", uri=f"sqlite:///{p / 'catalog.db'}",
                                  warehouse=f"file://{p / 'warehouse'}")
        for ns in LAYERS:
            self.catalog.create_namespace_if_not_exists(ns)

    # ---------- reads ----------
    def read(self, ident: str, snapshot_id: int | None = None) -> pa.Table:
        t = self.catalog.load_table(ident)
        return t.scan(snapshot_id=snapshot_id).to_arrow() if snapshot_id else t.scan().to_arrow()

    def tables(self) -> list[str]:
        return [f"{ns}.{n[-1]}" for ns in LAYERS for n in self.catalog.list_tables(ns)
                if not n[-1].endswith("__quarantine")]

    def quarantine_tables(self) -> list[str]:
        return [f"{ns}.{n[-1]}" for ns in LAYERS for n in self.catalog.list_tables(ns) if n[-1].endswith("__quarantine")]

    def history(self, ident: str) -> list[dict[str, Any]]:
        t = self.catalog.load_table(ident)
        return [{"snapshot_id": str(s.snapshot_id), "ts": s.timestamp_ms,
                 "operation": str(s.summary.operation.value) if s.summary else None,
                 "run_id": (s.summary.additional_properties or {}).get("run_id") if s.summary else None}
                for s in t.snapshots()]

    def connect(self) -> duckdb.DuckDBPyConnection:
        """DuckDB with every published table registered as a view named layer_table.
        bronze_x shows only the latest legacy run; bronze_x_all shows every run."""
        con = duckdb.connect()
        for ident in self.tables():
            ns, name = ident.split(".")
            data = self.read(ident)
            con.register(f"_{ns}_{name}", data)
            if ns == "bronze" and "_run_id" in data.column_names:
                con.execute(f"create view {ns}_{name}_all as select * from _{ns}_{name}")
                con.execute(f"create view {ns}_{name} as select * from _{ns}_{name} "
                            f"where _run_id = (select max(_run_id) from _{ns}_{name})")
            else:
                con.execute(f"create view {ns}_{name} as select * from _{ns}_{name}")
        return con

    # ---------- write audit publish ----------
    def write_wap(self, ident: str, data: pa.Table, checks: list[dict[str, Any]], run_id: str,
                  mode: str, tracer: Tracer) -> WriteResult:
        with tracer.span("lake.write_audit_publish", table=ident, rows=data.num_rows, mode=mode) as sp:
            try:
                tbl = self.catalog.load_table(ident)
                if mode == "overwrite" and tbl.schema().as_arrow() != data.schema:
                    # silver and gold are rebuilt each run; a changed model means a new table version
                    self.catalog.drop_table(ident)
                    raise NoSuchTableError(ident)
                if mode == "append":
                    with tbl.update_schema() as us:
                        us.union_by_name(data.schema)
                    tbl = self.catalog.load_table(ident)
            except NoSuchTableError:
                tbl = self.catalog.create_table(ident, schema=data.schema)
                tbl.append(data.slice(0, 0), snapshot_properties={"run_id": run_id, "note": "table created"})
                tbl = self.catalog.load_table(ident)
                sp.event("table_created")

            # row-level quarantine: checks marked on_fail: quarantine pull bad rows aside instead of blocking
            quarantined = []
            q_checks = [c for c in checks if c.get("on_fail") == "quarantine" and c["check"] == "expression"]
            if q_checks and data.num_rows:
                qc = duckdb.connect()
                qc.register("_incoming", data)
                for c in q_checks:
                    bad = qc.execute(f"select * from _incoming where not ({c['condition']})").arrow()
                    if hasattr(bad, "read_all"):
                        bad = bad.read_all()
                    if bad.num_rows:
                        keep = qc.execute(f"select * from _incoming where {c['condition']}").arrow()
                        data = keep.read_all() if hasattr(keep, "read_all") else keep
                        qc.unregister("_incoming")
                        qc.register("_incoming", data)
                        bad = bad.append_column("_quarantine_reason", pa.array([c.get("name", c["condition"])] * bad.num_rows))
                        bad = bad.append_column("_quarantine_run", pa.array([run_id] * bad.num_rows))
                        quarantined.append((c, bad))
                for c, bad in quarantined:
                    qid = f"{ident}__quarantine"
                    try:
                        qt = self.catalog.load_table(qid)
                        with qt.update_schema() as us:
                            us.union_by_name(bad.schema)
                        qt = self.catalog.load_table(qid)
                    except NoSuchTableError:
                        qt = self.catalog.create_table(qid, schema=bad.schema)
                    qt.append(bad, snapshot_properties={"run_id": run_id})
                    sp.event("rows_quarantined", check=c.get("name", c["condition"]), rows=bad.num_rows, table=qid)

            branch = f"audit_{run_id}_{uuid.uuid4().hex[:6]}"
            # WRITE: stage on a branch, main is untouched
            tbl.manage_snapshots().create_branch(tbl.current_snapshot().snapshot_id, branch).commit()
            tbl = self.catalog.load_table(ident)
            props = {"run_id": run_id, "wap": branch}
            if mode == "append":
                tbl.append(data, snapshot_properties=props, branch=branch)
            else:
                tbl.overwrite(data, snapshot_properties=props, branch=branch)
            tbl = self.catalog.load_table(ident)
            staged = tbl.metadata.refs[branch].snapshot_id
            sp.event("written_to_branch", branch=branch, snapshot_id=str(staged))

            # AUDIT: checks read exactly what would be published
            con = duckdb.connect()
            staged_data = tbl.scan(snapshot_id=staged).to_arrow()
            con.register("_staged", staged_data)
            if "_run_id" in staged_data.column_names:
                con.execute(f"create view staged as select * from _staged where _run_id = '{run_id}'")
            else:
                con.execute("create view staged as select * from _staged")
            results = run_checks(con, ident, "staged", [c for c in checks if c.get("on_fail") != "quarantine"])
            for c, bad in quarantined:
                results.append(CheckResult(table=ident, check=c.get("name", c["condition"]), kind=c.get("kind", "semantic"),
                                           severity="quarantine", owner=c.get("owner", "data-platform"), passed=False,
                                           failing_rows=bad.num_rows,
                                           detail=f"{bad.num_rows} rows moved to {ident}__quarantine: {c.get('description', c['condition'])}"))
            blocked = blocking_failures(results)
            for r in results:
                if not r.passed:
                    sp.event("check_failed", check=r.check, severity=r.severity, rows=r.failing_rows, owner=r.owner)

            # PUBLISH or drop
            published = not blocked
            if published:
                tbl.manage_snapshots().set_current_snapshot(snapshot_id=staged).commit()
                sp.event("published", snapshot_id=str(staged))
            else:
                sp.event("quarantined", reason=f"{len(blocked)} blocking checks failed")
                sp.status = "blocked"
            self.catalog.load_table(ident).manage_snapshots().remove_branch(branch).commit()
            sp.set(published=published, checks=len(results), failed=sum(not r.passed for r in results))
            return WriteResult(ident, data.num_rows, published, staged if published else None, branch, results)


def _expose(con: duckdb.DuckDBPyConnection, layer: str, name: str, data: pa.Table) -> None:
    con.register(f"_{layer}_{name}", data)
    con.execute(f"create or replace view {layer}_{name} as select * from _{layer}_{name}")


def migrate(pack: Pack, tracer: Tracer, run_id: str | None = None) -> dict[str, Any]:
    """Legacy -> bronze -> silver -> gold, each table behind its own quality gate."""
    run_id = run_id or time.strftime("%Y%m%d%H%M%S")
    lake = Lakehouse(pack)
    quality = pack.spec.get("quality", {})
    report: dict[str, Any] = {"run_id": run_id, "writes": []}

    with tracer.span("migrate", pack=pack.name, run_id=run_id):
        with tracer.span("ingest.bronze"):
            for src in pack.spec["sources"]:
                with tracer.span("legacy.read", source=src["name"], system=src.get("system", "")) as sp:
                    data = read_source(src, pack.paths.legacy)
                    sp.set(rows=data.num_rows, kind=src["kind"])
                data = data.append_column("_run_id", pa.array([run_id] * data.num_rows, pa.string()))
                data = data.append_column("_source_system", pa.array([src.get("system", src["name"])] * data.num_rows, pa.string()))
                res = lake.write_wap(f"bronze.{src['name']}", data, quality.get(f"bronze.{src['name']}", []),
                                     run_id, "append", tracer)
                report["writes"].append(res.to_dict())

        for layer in ("silver", "gold"):
            with tracer.span(f"transform.{layer}"):
                con = lake.connect()
                for name, sql in pack.models(layer):
                    with tracer.span("model.run", model=f"{layer}.{name}") as sp:
                        data = con.execute(sql).arrow()
                        if hasattr(data, "read_all"):  # newer DuckDB returns a reader
                            data = data.read_all()
                        sp.set(rows=data.num_rows)
                    res = lake.write_wap(f"{layer}.{name}", data, quality.get(f"{layer}.{name}", []),
                                         run_id, "overwrite", tracer)
                    report["writes"].append(res.to_dict())
                    if res.published:
                        _expose(con, layer, name, data)
                    else:
                        # downstream models keep reading the last published version
                        try:
                            _expose(con, layer, name, lake.read(f"{layer}.{name}"))
                        except Exception:
                            pass
    report["tables"] = {t: lake.history(t) for t in lake.tables()}
    return report
