"""Ontology store: objects, links, crosswalk, action log, outbox and audit.

One DuckDB file per pack. Lake tables are registered into the same connection,
so ontology functions can join objects to silver and gold in one query.
AWS mapping: Aurora Postgres (objects, outbox, audit index) + S3 Object Lock
for the immutable audit archive.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import duckdb

SCHEMA = """
create table if not exists objects (type varchar, id varchar, title varchar, props json, version integer,
  updated_at double, primary key (type, id));
create table if not exists object_history (type varchar, id varchar, version integer, props json,
  changed_by varchar, action_run varchar, ts double);
create table if not exists crosswalk (type varchar, id varchar, system varchar, source_key varchar,
  method varchar, confidence double);
create table if not exists merge_log (type varchar, id varchar, members json, method varchar, confidence double, ts double);
create table if not exists review_queue (type varchar, a varchar, b varchar, score double, reason varchar, ts double);
create table if not exists links (link_type varchar, from_type varchar, from_id varchar, to_type varchar,
  to_id varchar, props json);
create table if not exists idempotency (key varchar primary key, action_run varchar, result json, ts double);
create table if not exists outbox (id varchar primary key, action_run varchar, system varchar, payload json,
  status varchar, attempts integer, created double, updated double, last_error varchar);
create table if not exists audit_log (seq integer, ts double, actor varchar, on_behalf_of varchar,
  action varchar, status varchar, params varchar, objects varchar, before varchar, after varchar, evidence varchar,
  policy varchar, approved_by varchar, idempotency_key varchar, action_run varchar, prev_hash varchar, hash varchar);
"""


def _j(v: Any) -> str:
    return json.dumps(v, default=str, sort_keys=True)


class OntologyStore:
    def __init__(self, path: Path, read_only: bool = False):
        self.path = path
        self.con = duckdb.connect(str(path), read_only=read_only)
        if not read_only:
            self.con.execute(SCHEMA)
            self.con.execute("create or replace macro src_key(t, i, s) as "
                             "(select source_key from crosswalk where type = t and id = i and system = s limit 1)")
            self.con.execute("create or replace macro ont_id(t, s, k) as "
                             "(select id from crosswalk where type = t and system = s and source_key = k limit 1)")

    def reset(self) -> None:
        for t in ("objects", "object_history", "crosswalk", "merge_log", "review_queue", "links"):
            self.con.execute(f"delete from {t}")

    def reset_runtime(self) -> None:
        for t in ("idempotency", "outbox", "audit_log"):
            self.con.execute(f"delete from {t}")

    def bulk(self, table: str, rows: list[list[Any]]) -> None:
        """Fast insert through Arrow (DuckDB executemany is row by row)."""
        if not rows:
            return
        import pyarrow as pa
        cols = [c[0] for c in self.con.execute(f"select * from {table} limit 0").description]
        data = pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)})
        self.con.register("_bulk", data)
        self.con.execute(f"insert into {table} select * from _bulk")
        self.con.unregister("_bulk")

    # ----- objects -----
    def get(self, type_: str, id_: str) -> dict[str, Any] | None:
        r = self.con.execute("select props, version, title from objects where type = ? and id = ?", [type_, id_]).fetchone()
        if not r:
            return None
        props = json.loads(r[0])
        props.update({"_id": id_, "_type": type_, "_version": r[1], "_title": r[2]})
        return props

    def get_asof(self, type_: str, id_: str, ts: float) -> dict[str, Any] | None:
        """The object's state at time ts, resolved from object_history.

        The answering version is the latest history row written at or before
        ts. None when the object did not exist yet (or never did). When that
        version is still the latest one, the current objects row answers, so
        the read also carries _title. Historical snapshots carry the stored
        props only: object_history has no title column. Every result sets
        _asof_ts to the answering version's change time."""
        row = self.con.execute(
            "select version, props, ts from object_history where type = ? and id = ? and ts <= ? "
            "order by ts desc, version desc limit 1", [type_, id_, ts]).fetchone()
        if not row:
            return None
        version, props_json, changed_ts = row
        last = self.con.execute(
            "select max(version) from object_history where type = ? and id = ?",
            [type_, id_]).fetchone()
        if last and last[0] == version:
            cur = self.get(type_, id_)
            if cur is not None:
                return {**cur, "_asof_ts": changed_ts}
        props = json.loads(props_json)
        props.update({"_id": id_, "_type": type_, "_version": version, "_asof_ts": changed_ts})
        return props

    def find_type(self, id_: str) -> str | None:
        r = self.con.execute("select type from objects where id = ?", [id_]).fetchone()
        return r[0] if r else None

    def upsert(self, type_: str, id_: str, props: dict[str, Any], title: str, changed_by: str,
               action_run: str | None = None) -> int:
        cur = self.con.execute("select version from objects where type = ? and id = ?", [type_, id_]).fetchone()
        version = (cur[0] + 1) if cur else 1
        clean = {k: v for k, v in props.items() if not k.startswith("_") or k in ("_prov", "_pending_sync")}
        self.con.execute("insert or replace into objects values (?, ?, ?, ?, ?, ?)",
                         [type_, id_, title, _j(clean), version, time.time()])
        self.con.execute("insert into object_history values (?, ?, ?, ?, ?, ?, ?)",
                         [type_, id_, version, _j(clean), changed_by, action_run, time.time()])
        return version

    def list(self, type_: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.con.execute("select id, title, props, version from objects where type = ? order by id limit ? offset ?",
                                [type_, limit, offset]).fetchall()
        return [{"_id": r[0], "_title": r[1], "_version": r[3], **json.loads(r[2])} for r in rows]

    def neighbors(self, id_: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "select link_type, 'out' dir, to_type, to_id, props from links where from_id = ? union all "
            "select link_type, 'in' dir, from_type, from_id, props from links where to_id = ?", [id_, id_]).fetchall()
        return [{"link": r[0], "direction": r[1], "type": r[2], "id": r[3], "props": json.loads(r[4] or "{}")} for r in rows]

    # ----- audit (append only, hash chained so tampering is detectable) -----
    AUDIT_COLS = ["seq", "ts", "actor", "on_behalf_of", "action", "status", "params", "objects", "before",
                  "after", "evidence", "policy", "approved_by", "idempotency_key", "action_run"]
    JSON_COLS = {"params", "objects", "before", "after", "evidence", "policy"}

    def audit(self, **rec: Any) -> dict[str, Any]:
        last = self.con.execute("select seq, hash from audit_log order by seq desc limit 1").fetchone()
        seq, prev = ((last[0] + 1), last[1]) if last else (1, "genesis")
        rec = {**rec, "seq": seq, "ts": round(time.time(), 6)}
        vals = [_j(rec.get(c)) if c in self.JSON_COLS else rec.get(c) for c in self.AUDIT_COLS]
        h = hashlib.sha256((prev + _j(vals)).encode()).hexdigest()
        self.con.execute(f"insert into audit_log ({', '.join(self.AUDIT_COLS)}, prev_hash, hash) values "
                         f"({', '.join('?' * (len(self.AUDIT_COLS) + 2))})", vals + [prev, h])
        return {**rec, "hash": h}

    def verify_audit_chain(self) -> bool:
        cols = ", ".join(self.AUDIT_COLS)
        prev = "genesis"
        for row in self.con.execute(f"select {cols}, prev_hash, hash from audit_log order by seq").fetchall():
            vals, p, h = list(row[:-2]), row[-2], row[-1]
            if p != prev or hashlib.sha256((prev + _j(vals)).encode()).hexdigest() != h:
                return False
            prev = h
        return True

    def stats(self) -> dict[str, Any]:
        q = lambda s: self.con.execute(s).fetchall()  # noqa: E731
        return {
            "objects": dict(q("select type, count(*) from objects group by 1 order by 1")),
            "links": dict(q("select link_type, count(*) from links group by 1 order by 1")),
            "crosswalk_rows": q("select count(*) from crosswalk")[0][0],
            "merges": q("select count(*) from merge_log where json_array_length(members) > 1")[0][0],
            "review_queue": q("select count(*) from review_queue")[0][0],
            "audit_records": q("select count(*) from audit_log")[0][0],
            "outbox": dict(q("select status, count(*) from outbox group by 1")),
        }
