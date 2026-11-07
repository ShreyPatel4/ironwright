"""The bridge planner: does every gold column trace back to a real source column?

Rules decide, in a fixed order, and each answer names the rule. A gold column is

- covered: one source column matches it by a name rule, the type fits, a key is unique in
  the profile, the column holds values, and its table joins the table the rest of the gold
  table comes from. The answer carries the lineage path.
- partial: a source column matches, and the answer lists what is missing.
- a gap: no source column matches any name rule.

When a rule cannot be proved from the profile, the answer is partial, never covered.
There is no fuzzy name rule on purpose: a near name is how a wrong column gets wired in.
A model may narrate the finished report. It never sets a status, and `narrate` proves the
report did not move while it spoke.

```mermaid
flowchart TD
  G[gold column] --> N{name rule<br/>same name, abbreviation,<br/>table prefix}
  N -- none --> GAP[gap]
  N -- match --> T{type fits}
  T --> K{key unique}
  K --> V{holds values}
  V --> J{joins the home table}
  J -- all pass --> COV[covered + lineage]
  J -- any fails --> PAR[partial + what is missing]
```
"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import Any

from ..agents.guardrail import guard
from ..llm import LLM, boundary_rule, fence_record, new_nonce
from .planner import requirements

NARRATION_CAP = 1200
# Short forms that mean one word. Both sides expand before they are compared.
ABBREVIATIONS = {
    "amt": "amount", "no": "number", "num": "number", "nbr": "number", "dt": "date", "qty": "quantity",
    "cd": "code", "desc": "description", "addr": "address", "cust": "customer", "acct": "account",
    "pct": "percent", "ts": "timestamp", "yr": "year", "mo": "month",
}
RULES = {"same_name": 1.0, "abbreviation": 0.9, "table_prefix": 0.85}
_DECIMAL = re.compile(r"decimal\(\d+,\s*\d+\)")
_NUMERIC = ("int", "bigint", "integer")
_FLOATING = ("double", "float")


# ------------------------------------------------------------------ rules

def _expand(name: str) -> str:
    return "_".join(ABBREVIATIONS.get(w, w) for w in name.split("_"))


def _stem(table: str) -> str:
    if table.endswith("ies"):
        return table[:-3] + "y"
    return table[:-1] if table.endswith("s") and not table.endswith("ss") else table


def _name_rule(gold: str, table: str, names: list[str]) -> str | None:
    """The strongest name rule that ties a gold column to a source column, or None."""
    if gold in names:
        return "same_name"
    if _expand(gold) in {_expand(n) for n in names}:
        return "abbreviation"
    stem = _stem(table)
    for n in names:
        if gold == f"{stem}_{n}" or n == f"{stem}_{gold}":
            return "table_prefix"
    return None


def _kind(t: str) -> str:
    if t in _NUMERIC:
        return "int"
    if _DECIMAL.fullmatch(t):
        return "decimal"
    if t in _FLOATING:
        return "double"
    return {"varchar": "string", "boolean": "bool"}.get(t, t)


_FITS = {
    "string": None,  # anything reads as text
    "int": {"int"},
    "decimal": {"int", "decimal"},
    "double": {"int", "decimal", "double"},
    "date": {"date", "timestamp"},
    "timestamp": {"timestamp", "date"},
    "bool": {"bool"},
}


def _type_fits(gold: str, source: str) -> bool:
    allowed = _FITS.get(_kind(gold), {_kind(gold)})
    return allowed is None or _kind(source) in allowed


# ------------------------------------------------------------------ matching

def _candidates(column: str, sources: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for t in sources["tables"]:
        for c in t["columns"]:
            names = [c["name"]] + ([c["silver"]] if c["silver"] and c["silver"] != c["name"] else [])
            rule = _name_rule(column, t["name"], names)
            if rule:
                out.append({"table": t, "column": c, "rule": rule, "score": RULES[rule]})
    return out


def _home(table: dict[str, Any], cands: dict[str, list[dict[str, Any]]]) -> str | None:
    """The source table most of this gold table comes from: the best summed name score."""
    score: dict[str, float] = {}
    for col in table["columns"]:
        best: dict[str, float] = {}
        for c in cands[col["name"]]:
            best[c["table"]["name"]] = max(best.get(c["table"]["name"], 0.0), c["score"])
        for t, s in best.items():
            score[t] = score.get(t, 0.0) + s
    if not score:
        return None
    return min(score.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _reach(start: str, joins: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Every table a join path reaches from start, with the joins it takes."""
    edges: dict[str, list[tuple[str, str]]] = {}
    for j in joins:
        a, b = j["left"].split(".", 1)[0], j["right"].split(".", 1)[0]
        hop = f"{j['left']} = {j['right']}"
        edges.setdefault(a, []).append((b, hop))
        edges.setdefault(b, []).append((a, hop))
    seen, todo = {start: []}, deque([start])
    while todo:
        t = todo.popleft()
        for nxt, hop in sorted(edges.get(t, [])):
            if nxt not in seen:
                seen[nxt] = seen[t] + [hop]
                todo.append(nxt)
    return seen


def _lineage(cand: dict[str, Any], gold_ref: str) -> list[dict[str, str]]:
    t, c = cand["table"], cand["column"]
    path = [{"layer": "file", "ref": f"{t['file']}:{c['name']}"},
            {"layer": "bronze", "ref": f"bronze.{t['name']}.{c['name']}"}]
    if c["silver"]:
        path.append({"layer": "silver", "ref": f"silver.{t['name']}.{c['silver']}"})
    path.append({"layer": "gold", "ref": gold_ref})
    return path


def _check(gold_col: dict[str, Any], cand: dict[str, Any], home: str | None,
           reach: dict[str, list[str]]) -> tuple[list[dict[str, str]], list[str]]:
    t, c = cand["table"], cand["column"]
    where = f"{t['name']}.{c['name']}"
    missing = []
    if not _type_fits(gold_col["type"], c["type"]):
        missing.append({"kind": "type", "detail": f"{where} is {c['type']} and the gold column is {gold_col['type']}. "
                        "No cast I can prove from the profile turns one into the other."})
    if gold_col["key"] and c["unique"] is not True:
        missing.append({"kind": "key", "detail": f"{where} is the key here, and the profile does not show it unique "
                        "and never null on every row read." if c["profiled"] else
                        f"{where} is the key here, and there is no profile of it to prove it unique."})
    if c["null_rate"] == 1.0:
        missing.append({"kind": "values", "detail": f"{where} is null on every row read."})
    via: list[str] = []
    if home is not None and t["name"] != home:
        if t["name"] in reach:
            via = reach[t["name"]]
        else:
            missing.append({"kind": "join", "detail": f"{where} matches, and the rest of this table comes from "
                            f"{home}. No candidate join links {home} to {t['name']}."})
    return missing, via


def _pick(cands: list[dict[str, Any]], home: str | None, reach: dict[str, list[str]]) -> dict[str, Any]:
    return min(cands, key=lambda c: (c["table"]["name"] != home, c["table"]["name"] not in reach,
                                        -c["score"], c["table"]["name"], c["column"]["name"]))


def _gold_rows(contract: dict[str, Any], sources: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for table in contract["dimensions"] + contract["facts"]:
        cands = {c["name"]: _candidates(c["name"], sources) for c in table["columns"]}
        home = _home(table, cands)
        reach = _reach(home, sources["joins"]) if home else {}
        for col in table["columns"]:
            ref = f"{table['name']}.{col['name']}"
            row = {"ref": ref, "table": table["name"], "column": col["name"], "type": col["type"], "key": col["key"],
                   "home": home}
            if not cands[col["name"]]:
                rows.append({**row, "status": "gap", "rule": None, "source": None, "via": [], "lineage": [], "missing": [],
                             "reason": "No source column has this name, an abbreviation of it, or it under the "
                                       "table's own prefix."})
                continue
            pick = _pick(cands[col["name"]], home, reach)
            missing, via = _check(col, pick, home, reach)
            others = len({(c["table"]["name"], c["column"]["name"]) for c in cands[col["name"]]}) - 1
            rows.append({**row, "status": "partial" if missing else "covered", "rule": pick["rule"],
                         "source": {"table": pick["table"]["name"], "column": pick["column"]["name"]}, "via": via,
                         "lineage": _lineage(pick, ref), "missing": missing,
                         "reason": f"Matched by {pick['rule']}." + (f" {others} other source columns also match."
                                                                     if others else "")})
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = {s: sum(r["status"] == s for r in rows) for s in ("covered", "partial", "gap")}
    return {**n, "total": len(rows), "coverage": round(n["covered"] / len(rows), 4) if rows else 0.0}


def plan(contract: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    """The coverage report. Pure over a validated contract and a source graph."""
    req = requirements(contract)
    rows = _gold_rows(contract, sources)
    by_ref = {r["ref"]: r for r in rows}
    marts = []
    for m in req["marts"]:
        broken = {j["dimension"]: j for j in m["joins"] if not j["ok"]}
        cols = []
        for c in m["columns"]:
            g = by_ref[c["from"]]
            missing = list(g["missing"])
            table = c["from"].split(".", 1)[0]
            if table in broken:
                missing.append({"kind": "mart_join", "detail": broken[table]["reason"]})
            status = g["status"] if g["status"] == "gap" or not missing else "partial"
            lineage = g["lineage"] + [{"layer": "mart", "ref": c["ref"]}] if g["lineage"] else []
            cols.append({"ref": c["ref"], "from": c["from"], "aggregation": c["aggregation"], "status": status,
                         "missing": missing, "lineage": lineage})
        marts.append({"name": m["name"], "use_case": m["use_case"], "grain": m["grain"], "joins": m["joins"],
                      "columns": cols, "summary": _summary(cols)})
    every = [c for m in marts for c in m["columns"]]
    return {"requirements": req, "sources": sources, "columns": rows, "marts": marts,
            "summary": {"gold": _summary(rows), "mart": _summary(every)}, "narration": None}


# ------------------------------------------------------------------ the eval

def _observed(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    out = {}
    for r in report["columns"] + [c for m in report["marts"] for c in m["columns"]]:
        got = {"status": r["status"]}
        if r["status"] == "partial":
            got["missing"] = ",".join(sorted({m["kind"] for m in r["missing"]}))
        out[r["ref"]] = got
    return out


def check(report: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    """Exact equality with the ground truth. A planted problem the report missed and a
    problem it invented both fail, and so does a partial with the wrong missing piece."""
    want = {**truth["gold"], **truth["marts"]}
    got = _observed(report)
    missed = sorted(ref for ref, w in want.items() if w["status"] != "covered" and got.get(ref) != w)
    unexpected = sorted(ref for ref, g in got.items() if g["status"] != "covered" and want.get(ref) != g)
    absent = sorted(set(want) - set(got))
    return {"passed": not (missed or unexpected or absent), "missed": missed, "unexpected": unexpected,
            "absent": absent, "checked": len(want)}


# ------------------------------------------------------------------ narration

def narrate(report: dict[str, Any], llm: LLM) -> dict[str, Any]:
    """A paragraph beside the report. The report is frozen while the model speaks, and
    nothing it says reaches a status."""
    report["narration"] = None
    if llm.offline:
        return report
    frozen = json.dumps(report, sort_keys=True)
    nonce = new_nonce()
    lines = [fence_record({"column": r["ref"], "status": r["status"],
                           "missing": ", ".join(m["kind"] for m in r["missing"]) or "nothing"}, "coverage", nonce)
             for r in report["columns"]]
    res = llm.complete("You explain a data coverage report to the person who owns the gold contract.\n"
                       + boundary_rule(nonce),
                       "These statuses are final as far as you are concerned. You cannot change any.\n"
                       + "\n".join(lines)
                       + "\nIn 2 to 4 plain sentences, say what is covered and which gaps to close first.")
    if json.dumps(report, sort_keys=True) != frozen:  # not an assert statement: python -O must not strip it
        raise AssertionError("the report changed during narration")
    text = guard(res.text[:NARRATION_CAP], set()).text.strip() if res.text else ""
    report["narration"] = {"text": text, "model": res.model} if text else None
    return report
