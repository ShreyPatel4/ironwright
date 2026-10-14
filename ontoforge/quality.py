"""Quality gates. Structural checks (shape) and semantic checks (meaning) run on a
staged Iceberg branch before anything is published. Every check has a severity
and an owner, so a failure always has someone to route to."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import duckdb


@dataclass
class CheckResult:
    table: str
    check: str
    kind: str          # structural | semantic
    severity: str      # block | warn
    owner: str
    passed: bool
    failing_rows: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STRUCTURAL = {"not_null", "unique", "accepted_values", "row_count_min", "type_castable"}


def _sql_for(chk: dict[str, Any], t: str) -> tuple[str, str]:
    c = chk.get("column")
    kind = chk["check"]
    if kind == "not_null":
        return f"select count(*) from {t} where {c} is null", f"{c} has nulls"
    if kind == "unique":
        cols = c if isinstance(c, str) else ", ".join(c)
        return (f"select coalesce(sum(n - 1), 0) from (select count(*) n from {t} group by {cols} having count(*) > 1)",
                f"duplicate rows on ({cols})")
    if kind == "accepted_values":
        vals = ", ".join(f"'{v}'" for v in chk["values"])
        return f"select count(*) from {t} where {c} is not null and {c} not in ({vals})", f"{c} outside {chk['values']}"
    if kind == "row_count_min":
        return f"select case when count(*) >= {chk['min']} then 0 else 1 end from {t}", f"fewer than {chk['min']} rows"
    if kind == "type_castable":
        return (f"select count(*) from {t} where {c} is not null and try_cast({c} as {chk['type']}) is null",
                f"{c} not castable to {chk['type']}")
    if kind == "range":
        return (f"select count(*) from {t} where {c} is not null and ({c} < {chk.get('min', '-1e30')} "
                f"or {c} > {chk.get('max', '1e30')})", f"{c} outside [{chk.get('min')}, {chk.get('max')}]")
    if kind == "segment_presence":
        expected = ", ".join(f"('{v}')" for v in chk["values"])
        return (f"select count(*) from (values {expected}) e(v) where v not in (select distinct {c} from {t})",
                f"missing {c} segments (a feed may have gone quiet)")
    if kind == "relationship":
        return (f"select count(*) from {t} a where a.{c} is not null and a.{c} not in "
                f"(select {chk['to_column']} from {chk['to_table']})", f"{c} has orphans vs {chk['to_table']}")
    if kind == "expression":
        return f"select count(*) from {t} where not ({chk['condition']})", chk.get("description", chk["condition"])
    raise ValueError(f"unknown check {kind}")


def run_checks(con: duckdb.DuckDBPyConnection, table_label: str, view: str,
               checks: list[dict[str, Any]]) -> list[CheckResult]:
    out = []
    for chk in checks or []:
        sql, detail = _sql_for(chk, view)
        try:
            n = int(con.execute(sql).fetchone()[0] or 0)
        except duckdb.Error as e:
            n, detail = -1, f"check errored: {str(e)[:120]}"
        out.append(CheckResult(
            table=table_label, check=chk.get("name", chk["check"]),
            kind=chk.get("kind", "structural" if chk["check"] in STRUCTURAL else "semantic"),
            severity=chk.get("severity", "block"), owner=chk.get("owner", "data-platform"),
            passed=n == 0, failing_rows=max(n, 0), detail=detail))
    return out


def blocking_failures(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.passed and r.severity == "block"]
