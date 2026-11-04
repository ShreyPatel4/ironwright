"""Propose an ontology from evidence. A human accepts it. Nothing else writes one.

Rules decide everything here. They read the tables a run published (types, row
counts, null and distinct counts, value overlap) plus T3's profile.json when one
sits beside the upload. A profile describes the raw file, so it only adds weight:
a key has to hold in the table the object is built from.

```mermaid
flowchart LR
  L[lake tables] --> R[rules]
  P[profile.json, optional] --> R
  R --> S[proposal, stored]
  S --> H{human accepts, with edits}
  H --> O[ontology.yaml + policies.yaml + audit row]
  O --> B[run job builds objects]
  B --> A[Ask calls generated reads]
```

What a proposal never holds: a cell value (reasons carry counts and names only),
a server path, an org id, a clock. So the same rows give the same proposal and id.

Why functions are not in ontology.yaml: a function's SQL runs on the ontology store
connection, which has no external access lock, and T2's validator refuses tenant
functions for that reason. So the SQL lives here. `generated_functions` builds it
from names it has checked, it binds every value, and it only reads objects and links.
There are no actions: those need a policy a human wrote (default deny stays).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import yaml

from .. import workspaces_tenant as wt
from ..agents.guardrail import guard
from ..config import private_dir, write_private
from ..lake import Lakehouse
from ..llm import LLM, boundary_rule, fence_record, new_nonce
from ..packs import Pack
from .store import OntologyStore

PROPOSAL_FILE = "ontology_proposal.json"
VERSIONS_DIR = "ontology_versions"
RULE_PREFIX = "ontology-read-"
MIN_OVERLAP = 0.5
MAX_EDITS = 500
NARRATION_CAP = 1200
LIST_LIMIT = 100

_IDENT = re.compile(r"[a-z][a-z0-9_]{0,62}")
_TYPE_NAME = re.compile(r"[A-Z][A-Za-z0-9]{0,63}")
_LINK_NAME = re.compile(r"[a-z][a-z0-9_]{0,127}")
_KEY_SHAPE = re.compile(r"(?:(.+)_)?(id|ref|no|num|number|key|code)")
_NAME_LIKE = re.compile(r"(^|_)(name|title|holder)(_|$)")
_NOT_A_KEY_TYPE = ("double", "float", "real", "decimal", "boolean", "date", "timestamp", "time")
_IDENTITY = (("email", re.compile(r"(^|_)e?_?mail(_|$)")),
             ("phone", re.compile(r"(^|_)(phone|mobile|tel)(_|$)")),
             ("name", re.compile(r"(^|_)name(_|$)")))


class ProposalError(Exception):
    """There is nothing to propose from yet. The message is safe to show."""


class StaleProposal(Exception):
    """The id is not the proposal this workspace holds now."""


class BadEdit(ValueError):
    """An edit that cannot apply. The message is safe to show."""


class StoreBusy(Exception):
    """A run holds the ontology store. Try again when it is done."""


# ---------------------------------------------------------------- names

def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if re.search(r"(s|x|z|ch|sh)es$", word):
        return word[:-2]
    if word.endswith(("ss", "us", "is")) or not word.endswith("s"):
        return word
    return word[:-1]


def _table_singular(stem: str) -> str:
    head, _, last = stem.rpartition("_")
    return f"{head}_{_singular(last)}" if head else _singular(last)


def object_name(table_stem: str) -> str:
    return "".join(w.capitalize() for w in _table_singular(table_stem).split("_") if w)


def _snake(type_name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type_name).lower()


def _plural(words: str) -> str:
    if words.endswith("y"):
        return words[:-1] + "ies"
    return words + ("es" if words.endswith(("s", "x", "z", "ch", "sh")) else "s")


# ---------------------------------------------------------------- evidence

def _quote(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _table_stats(con: duckdb.DuckDBPyConnection, ident: str, reserved: set[str]) -> dict[str, Any]:
    """Counts only. Cells stay in the lake."""
    view = ident.replace(".", "_")
    described = [(r[0], str(r[1]).lower()) for r in con.execute(f"describe {view}").fetchall()]
    visible = [(n, t) for n, t in described if not n.startswith("_")]  # engine columns
    usable = [(n, t) for n, t in visible if _IDENT.fullmatch(n) and n not in reserved]
    selects = ["count(*)"] + [f"count({_quote(n)}), count(distinct {_quote(n)})" for n, _ in usable]
    row = con.execute(f"select {', '.join(selects)} from {view}").fetchone()
    rows = row[0]
    columns = [{"name": n, "type": t, "nulls": rows - row[1 + 2 * i], "distinct": row[2 + 2 * i]}
               for i, (n, t) in enumerate(usable)]
    layer, stem = ident.split(".")
    return {"ident": ident, "view": view, "layer": layer, "stem": stem, "rows": rows, "columns": columns,
            "left_out": len(visible) - len(usable)}


def _read_profile(path: Path) -> dict[str, Any]:
    """T3's profile.json as plain data. It is evidence, not a requirement, so a
    missing or broken one counts as none."""
    try:
        found = json.loads(wt._read_small(path, "profile.json"))
    except (FileNotFoundError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _profile_evidence(pack: Pack) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    """(source name -> candidate key columns, joins as source and column names)."""
    sources: dict[str, str] = {}  # file name -> source name
    keys: dict[str, set[str]] = {}
    joins: list[dict[str, str]] = []
    profiles: dict[Path, dict[str, Any]] = {}
    for src in pack.spec["sources"]:
        upload = Path(src["upload_path"])
        sources[upload.name] = src["name"]
        if upload.parent not in profiles:
            profiles[upload.parent] = _read_profile(upload.parent / "profile.json")

    def ref(value: Any) -> tuple[str, str] | None:
        if not isinstance(value, str) or "." not in value:
            return None
        file_name, column = value.rsplit(".", 1)
        return (sources[file_name], column) if file_name in sources else None

    for profile in profiles.values():
        for f in profile.get("files") or []:
            if isinstance(f, dict) and f.get("name") in sources:
                keys.setdefault(sources[f["name"]], set()).update(
                    k["column"] for k in f.get("candidate_keys") or [] if isinstance(k, dict) and "column" in k)
        for j in profile.get("candidate_joins") or []:
            if not isinstance(j, dict):
                continue
            left, right, one = ref(j.get("left")), ref(j.get("right")), ref(j.get("unique_side"))
            if left and right and one in (left, right):
                many = right if one == left else left
                joins.append({"from": many[0], "from_column": many[1], "to": one[0], "to_column": one[1]})
    return keys, joins


# ---------------------------------------------------------------- objects

def _key_shape_matches(column: str, singular: str) -> bool:
    m = _KEY_SHAPE.fullmatch(column)
    return bool(m and (m.group(1) == singular or (m.group(1) or "").endswith(f"_{singular}")))


def _choose_key(table: dict[str, Any], declared: set[str], profiled: set[str]) -> tuple[str, float, str] | None:
    singular = _table_singular(table["stem"])
    best: tuple[tuple[int, int, int, int], str, float, str] | None = None
    for i, c in enumerate(table["columns"]):
        if c["nulls"] or c["distinct"] != table["rows"] or not table["rows"]:
            continue
        if c["type"].startswith(_NOT_A_KEY_TYPE):
            continue
        name = c["name"]
        named_for_table = name == "id" or _key_shape_matches(name, singular)
        shaped = bool(_KEY_SHAPE.fullmatch(name))
        if named_for_table:
            confidence, why = 0.95, f"The name {name} reads as this table's own key."
        elif name in declared:
            confidence, why = 0.9, f"The workspace spec declares {name} as a key."
        elif shaped:
            confidence, why = 0.9, f"The name {name} reads like a key."
        else:
            confidence, why = 0.6, f"Only the values say {name} is a key, not the name."
        if name in profiled:
            confidence, why = min(0.99, round(confidence + 0.1, 2)), f"{why} The upload profile agrees."
        rank = (not named_for_table, name not in declared, not shaped, i)
        if best is None or rank < best[0]:
            best = (rank, name, confidence, why)
    return (best[1], best[2], best[3]) if best else None


def _property(c: dict[str, Any], rows: int) -> dict[str, Any]:
    typed = not c["type"].startswith("varchar")
    return {"name": c["name"], "type": c["type"], "unique": bool(rows) and not c["nulls"] and c["distinct"] == rows,
            "confidence": 0.95 if typed else 0.8,
            "reason": f"Stored as {c['type']}. {c['nulls']} of {rows} values are empty and {c['distinct']} are distinct."}


def _objects(tables: list[dict[str, Any]], declared: dict[str, set[str]], profiled: dict[str, set[str]]
             ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    objects: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    used: set[str] = set()
    for t in tables:
        key = _choose_key(t, declared.get(t["stem"], set()), profiled.get(t["stem"], set()))
        if key is None:
            skipped.append({"table": t["ident"], "reason": f"No column in its {t['rows']} rows is unique and never "
                                                           "empty, so it has no key to build an object on. "
                                                           "A silver model that removes duplicates would give it one."})
            continue
        name, n = object_name(t["stem"]), 1
        base = name
        while name in used:
            n += 1
            name = f"{base}{n}"
        used.add(name)
        column, confidence, why = key
        reason = f"{t['ident']} has {t['rows']} rows and {column} is unique and never empty. {why}"
        if t["left_out"]:
            reason += f" I left out {t['left_out']} columns the engine cannot select by name."
        objects.append({"name": name, "table": t["ident"], "key": column,
                        "properties": [_property(c, t["rows"]) for c in t["columns"]],
                        "confidence": confidence, "reason": reason})
    return objects, skipped


# ---------------------------------------------------------------- links

def _overlap(con: duckdb.DuckDBPyConnection, a: dict[str, Any], col: str, b: dict[str, Any], key: str,
             normalize: bool) -> tuple[int, int]:
    wrap = "ltrim({}::varchar, '0')" if normalize else "{}::varchar"
    x, y = wrap.format(_quote(col)), wrap.format(_quote(key))
    return con.execute(
        f"select count(distinct x), count(distinct x) filter (where x in (select {y} from {b['view']})) "
        f"from (select {x} as x from {a['view']} where {_quote(col)} is not null)").fetchone()


def _link_candidates(objects: list[dict[str, Any]], tables: dict[str, dict[str, Any]],
                     joins: list[dict[str, str]]) -> list[tuple[int, dict[str, Any], str, dict[str, Any], bool]]:
    """(rank, from object, column, to object, the profile saw it). Named matches
    sort first, so when both directions fit, the one the name points at wins."""
    out = []
    for a in objects:
        for b in objects:
            if a is b:
                continue
            singular = _table_singular(tables[b["table"]]["stem"])
            a_stem, b_stem = tables[a["table"]]["stem"], tables[b["table"]]["stem"]
            for p in a["properties"]:
                col = p["name"]
                by_name = _key_shape_matches(col, singular)
                same = col == b["key"] and bool(_KEY_SHAPE.fullmatch(col))
                by_profile = any(j["from"] == a_stem and j["from_column"] == col
                                 and j["to"] == b_stem and j["to_column"] == b["key"] for j in joins)
                if by_name or same or by_profile:
                    out.append((0 if by_name else 1, a, col, b, by_profile))
    return sorted(out, key=lambda c: (c[0], c[1]["name"], c[2], c[3]["name"]))


def _links(con: duckdb.DuckDBPyConnection, objects: list[dict[str, Any]], tables: dict[str, dict[str, Any]],
           joins: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    links: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    pairs: set[frozenset[tuple[str, str]]] = set()
    for _, a, col, b, by_profile in _link_candidates(objects, tables, joins):
        pair = frozenset({(a["name"], col), (b["name"], b["key"])})
        if pair in pairs:
            continue
        ta, tb = tables[a["table"]], tables[b["table"]]
        distinct, matched = _overlap(con, ta, col, tb, b["key"], normalize=False)
        normalize = False
        if not distinct or matched / distinct < MIN_OVERLAP:
            d2, m2 = _overlap(con, ta, col, tb, b["key"], normalize=True)
            if d2 and m2 / d2 >= MIN_OVERLAP:
                distinct, matched, normalize = d2, m2, True
        if not distinct or matched / distinct < MIN_OVERLAP:
            skipped.append({"table": f"{a['table']}.{col}",
                            "reason": f"{col} reads like a key to {b['name']}, but only {matched} of its {distinct} "
                                      f"distinct values match {b['key']}, so I do not propose this link."})
            continue
        pairs.add(pair)
        stats = next(c for c in ta["columns"] if c["name"] == col)
        filled = ta["rows"] - stats["nulls"]
        one_to_one = stats["distinct"] == filled
        orphans = distinct - matched
        confidence = round(0.5 + 0.45 * matched / distinct + (0.04 if by_profile else 0), 2)
        reason = f"{col} reads like a key to {b['name']}." if not by_profile else f"The upload profile joins {col} to {b['key']}."
        reason += f" {matched} of {distinct} distinct values match {b['key']}"
        reason += ", after dropping leading zeros." if normalize else "."
        if orphans:
            reason += f" Values with no match, which will count as orphans: {orphans}."
        reason += (f" Each {a['name']} points at a different {b['name']}." if one_to_one
                   else f" Values repeat, so many {_plural(_snake(a['name']).replace('_', ' '))} share one {b['name']}.")
        links.append({"from": a["name"], "to": b["name"], "via": col,
                      "cardinality": "one_to_one" if one_to_one else "many_to_one",
                      "confidence": confidence, "reason": reason,
                      "to_column": b["key"], "normalize": normalize, "orphans": orphans})
    links.sort(key=lambda k: (k["from"], k["to"], k["via"]))
    used: set[str] = set()
    for k in links:
        name = f"{_snake(k['from'])}_to_{_snake(k['to'])}"
        k["name"] = name if name not in used else f"{name}_via_{k['via']}"
        used.add(k["name"])
    return links, skipped


def _identity_hints(con: duckdb.DuckDBPyConnection, objects: list[dict[str, Any]], tables: dict[str, dict[str, Any]]
                    ) -> list[dict[str, Any]]:
    """A hint for a human, never a merge. It reports how many, and never which."""
    columns = []
    for o in objects:
        for p in o["properties"]:
            kind = next((k for k, pat in _IDENTITY if pat.search(p["name"])), None)
            if kind:
                columns.append((kind, o, p["name"]))
    hints = []
    norm = "lower(trim({}::varchar))"
    for i, (kind, a, ca) in enumerate(columns):
        for other_kind, b, cb in columns[i + 1:]:
            if kind != other_kind or a is b:
                continue
            x, y = norm.format(_quote(ca)), norm.format(_quote(cb))
            matches = con.execute(f"select count(distinct {x}) from {tables[a['table']]['view']} "
                                  f"where {x} in (select {y} from {tables[b['table']]['view']})").fetchone()[0]
            if not matches:
                continue
            smaller = min(next(c["distinct"] for c in tables[a["table"]]["columns"] if c["name"] == ca),
                          next(c["distinct"] for c in tables[b["table"]]["columns"] if c["name"] == cb))
            hints.append({"kind": kind, "left": f"{a['table']}.{ca}", "right": f"{b['table']}.{cb}", "matches": matches,
                          "confidence": round(0.4 + 0.5 * matches / max(smaller, 1), 2),
                          "reason": f"Both columns read as {kind} and {matches} distinct values appear in both, ignoring "
                                    f"case. {a['name']} and {b['name']} may share people. I do not merge on this. You decide."})
    return sorted(hints, key=lambda h: (h["left"], h["right"]))


# ---------------------------------------------------------------- the ontology an accept writes

def _title(o: dict[str, Any]) -> str:
    return next((p["name"] for p in o["properties"] if _NAME_LIKE.search(p["name"])), o["key"])


def _link_sql(link: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> str:
    """The key on each side is what the crosswalk holds, so a key edit or a zero
    bridge goes through a join. Everything here passed the identifier check."""
    sa, sb = a["table"].split(".")[1], b["table"].split(".")[1]
    va, vb = a["table"].replace(".", "_"), b["table"].replace(".", "_")
    if b["key"] == link["to_column"] and not link["normalize"]:
        return (f"select '{sa}' from_system, {a['key']}::varchar from_key, '{sb}' to_system, "
                f"{link['via']}::varchar to_key from {va} where {link['via']} is not null")
    wrap = "ltrim({}::varchar, '0')" if link["normalize"] else "{}::varchar"
    on = f"{wrap.format('l.' + link['via'])} = {wrap.format('r.' + link['to_column'])}"
    return (f"select '{sa}' from_system, l.{a['key']}::varchar from_key, '{sb}' to_system, "
            f"r.{b['key']}::varchar to_key from {va} l join {vb} r on {on}")


def to_ontology(objects: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    """Objects and links only: what T2's validator allows a tenant to hold."""
    by_name = {o["name"]: o for o in objects}
    types = {o["name"]: {"prefix": o["name"][:3].upper(), "owner": "workspace", "title": _title(o),
                         "description": f"Proposed from {o['table']}.",
                         "sources": [{"system": o["table"].split(".")[1], "table": o["table"], "key": o["key"],
                                      "fields": {p["name"]: p["name"] for p in o["properties"]}}]}
             for o in objects}
    out: dict[str, Any] = {"object_types": types}
    if links:
        out["link_types"] = {k["name"]: {"from": k["from"], "to": k["to"], "description": f"Through {k['via']}.",
                                         "sql": _link_sql(k, by_name[k["from"]], by_name[k["to"]])}
                             for k in links}
    return out


def _fields_sql(tdef: dict[str, Any], alias: str = "") -> str:
    sensitive = tdef.get("sensitive") or []
    names = sorted({f for s in tdef.get("sources", []) for f in s.get("fields", {})})
    plain = [f for f in names if isinstance(f, str) and _IDENT.fullmatch(f) and f not in sensitive]
    cols = [f"{alias}id as _id", f"{alias}title as _title"] + [f"{alias}props->>'$.{f}' as \"{f}\"" for f in plain]
    return ", ".join(cols)


def generated_functions(ontology: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """One get per object type and one list per link. Only names that pass a plain
    identifier check reach the SQL, and each value is a bound parameter. T2's
    validator allows free text in a type name, so this check is not redundant."""
    types = {n: t for n, t in (ontology.get("object_types") or {}).items()
             if isinstance(n, str) and _TYPE_NAME.fullmatch(n)}
    out: dict[str, dict[str, Any]] = {}
    for name, tdef in types.items():
        words = _snake(name).replace("_", " ")
        out[f"get_{_snake(name)}"] = {
            "description": f"Get one {words} by its id.", "params": {"id": name}, "returns": "one",
            "sql": f"select {_fields_sql(tdef)} from objects where type = '{name}' and id = $id"}
    for lname, ldef in sorted((ontology.get("link_types") or {}).items()):
        a, b = ldef.get("from"), ldef.get("to")
        if a not in types or b not in types or not isinstance(lname, str) or not _LINK_NAME.fullmatch(lname):
            continue
        fname = f"list_{_snake(a)}_by_{_snake(b)}"
        words_a, words_b = _snake(a).replace("_", " "), _snake(b).replace("_", " ")
        description = f"List the {_plural(words_a)} on one {words_b}, by {words_b} id."
        if fname in out:
            fname, description = f"{fname}_via_{lname}", f"{description} Through {lname}."
        param = f"{_snake(b)}_id"
        out[fname] = {
            "description": description, "params": {param: b}, "returns": "many",
            "sql": f"select {_fields_sql(types[a], 'o.')} from links l join objects o on o.type = '{a}' and o.id = l.from_id "
                   f"where l.link_type = '{lname}' and l.to_type = '{b}' and l.to_id = ${param} order by o.id limit {LIST_LIMIT}"}
    return out


def askable(pack: Pack) -> Pack:
    """The pack Ask runs on: the same pack, plus the reads the engine generates.
    A serving route needs this and nothing else to answer over a tenant workspace."""
    return replace(pack, ontology={**pack.ontology, "functions": generated_functions(pack.ontology)})


# ---------------------------------------------------------------- the proposal

def _digest(proposal: dict[str, Any]) -> str:
    body = {k: v for k, v in proposal.items() if k not in ("id", "narration")}
    return "op-" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def _entity_tables(all_tables: list[str]) -> tuple[list[str], str]:
    """Silver holds the entities. A draft with no models has only bronze, and the
    first run of a T3 draft is exactly that, so bronze stands in."""
    silver = sorted(t for t in all_tables if t.startswith("silver."))
    return (silver, "silver") if silver else (sorted(t for t in all_tables if t.startswith("bronze.")), "bronze")


def _narrate(proposal: dict[str, Any], llm: LLM) -> dict[str, str] | None:
    """It reads the finished proposal and writes a paragraph beside it. No path from
    its reply leads back into a decision, and the digest proves it on the way out."""
    if llm.offline:
        return None
    frozen = json.dumps(proposal, sort_keys=True)
    nonce = new_nonce()
    rows = [fence_record({"object": o["name"], "table": o["table"], "key": o["key"], "confidence": o["confidence"]},
                         "object", nonce) for o in proposal["objects"]]
    rows += [fence_record({"from": k["from"], "to": k["to"], "via": k["via"], "cardinality": k["cardinality"],
                           "confidence": k["confidence"]}, "link", nonce) for k in proposal["links"]]
    res = llm.complete(f"You explain a proposed data model to the person who will approve it.\n{boundary_rule(nonce)}",
                       "These decisions are final as far as you are concerned. You cannot add or remove any.\n"
                       + "\n".join(rows)
                       + "\nIn 2 to 4 plain sentences, say what this model describes and what to check first.")
    if json.dumps(proposal, sort_keys=True) != frozen:  # not an assert statement: python -O must not strip it
        raise AssertionError("the proposal changed during narration")
    if not res.text:
        return None
    text = guard(res.text[:NARRATION_CAP], set()).text.strip()
    return {"text": text, "model": res.model} if text else None


def propose(pack: Pack, llm: LLM | None = None) -> dict[str, Any]:
    """Pure over the lake's rows. llm is opt-in, as it is for Ask."""
    lake = Lakehouse(pack)
    all_tables = lake.tables()
    if not all_tables:
        raise ProposalError("This workspace has no tables yet. Run it first, then ask for a proposal.")
    con = lake.connect(capped=True)
    try:
        reserved = {r[0] for r in con.execute(
            "select keyword_name from duckdb_keywords() where keyword_category = 'reserved'").fetchall()}
        entity_idents, layer = _entity_tables(all_tables)
        stats = [_table_stats(con, t, reserved) for t in entity_idents]
        by_ident = {t["ident"]: t for t in stats}
        declared = {s["name"]: {c["name"] for c in s["columns"] if c.get("key")} for s in pack.spec["sources"]}
        profiled, joins = _profile_evidence(pack)
        objects, skipped = _objects(stats, declared, profiled)
        links, link_skips = _links(con, objects, by_ident, joins)
        hints = _identity_hints(con, objects, by_ident)
    finally:
        con.close()
    skipped += link_skips
    for t in sorted(t for t in all_tables if t.startswith("gold.")):
        skipped.append({"table": t, "reason": "A gold table holds derived rows, so it is a report on objects and not an object itself."})
    functions = generated_functions(to_ontology(objects, links))
    proposal = {"source_layer": layer, "objects": objects, "links": links, "identity_hints": hints,
                "functions": [{"name": n, "kind": "read", "description": f["description"], "params": f["params"]}
                              for n, f in sorted(functions.items())],
                "skipped": sorted(skipped, key=lambda s: s["table"])}
    proposal["id"] = _digest(proposal)
    proposal["narration"] = None
    if llm is not None:
        proposal["narration"] = _narrate(proposal, llm)
    return proposal


def store_proposal(pack: Pack, llm: LLM | None = None) -> dict[str, Any]:
    proposal = propose(pack, llm=llm)
    write_private(pack.dir / PROPOSAL_FILE, json.dumps(proposal, indent=1, sort_keys=True))
    return proposal


def read_proposal(org_id: str, slug: str) -> dict[str, Any] | None:
    """None when none has been made. FileNotFoundError when this org has no such workspace."""
    pack = wt.load_workspace(org_id, slug)
    try:
        return json.loads(wt._read_small(pack.dir / PROPOSAL_FILE, PROPOSAL_FILE))
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- accept

def _text(edit: dict[str, Any], field: str) -> str:
    value = edit.get(field)
    if not isinstance(value, str):
        raise BadEdit(f"{edit.get('op')}: {field} must be text")
    return value


_EDIT_FIELDS = {"rename_object": {"op", "object", "to"}, "drop_object": {"op", "object"},
                "drop_property": {"op", "object", "property"}, "set_key": {"op", "object", "key"},
                "drop_link": {"op", "link"}}


def _apply_edit(edit: Any, objects: list[dict[str, Any]], links: list[dict[str, Any]]
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(edit, dict) or edit.get("op") not in _EDIT_FIELDS or not isinstance(edit["op"], str):
        raise BadEdit("each edit is one of: " + ", ".join(sorted(_EDIT_FIELDS)))
    op = edit["op"]
    if set(edit) != _EDIT_FIELDS[op]:
        raise BadEdit(f"{op} takes exactly: {', '.join(sorted(_EDIT_FIELDS[op]))}")
    if op == "drop_link":
        name = _text(edit, "link")
        if not any(k["name"] == name for k in links):
            raise BadEdit(f"drop_link: no link named {name}")
        return objects, [k for k in links if k["name"] != name]
    name = _text(edit, "object")
    target = next((o for o in objects if o["name"] == name), None)
    if target is None:
        raise BadEdit(f"{op}: no object named {name}")
    if op == "drop_object":
        return [o for o in objects if o is not target], [k for k in links if name not in (k["from"], k["to"])]
    if op == "rename_object":
        to = _text(edit, "to")
        if not _TYPE_NAME.fullmatch(to):
            raise BadEdit("rename_object: a name is a capital letter, then letters and digits")
        if any(o["name"] == to for o in objects):
            raise BadEdit(f"rename_object: {to} is taken")
        target["name"] = to
        for k in links:
            k["from"] = to if k["from"] == name else k["from"]
            k["to"] = to if k["to"] == name else k["to"]
        return objects, links
    column = _text(edit, "property" if op == "drop_property" else "key")
    prop = next((p for p in target["properties"] if p["name"] == column), None)
    if prop is None:
        raise BadEdit(f"{op}: {name} has no property {column}")
    if op == "set_key":
        if not prop["unique"]:
            raise BadEdit(f"set_key: {column} is not unique and never empty")
        if prop["type"].startswith(_NOT_A_KEY_TYPE):
            raise BadEdit(f"set_key: {column} is a {prop['type']}, which does not make a stable key")
        target["key"] = column
        return objects, links
    if column == target["key"]:
        raise BadEdit(f"drop_property: {column} is the key of {name}")
    if any(k["from"] == name and k["via"] == column for k in links):
        raise BadEdit(f"drop_property: a link goes through {column}. Drop the link first.")
    target["properties"] = [p for p in target["properties"] if p is not prop]
    return objects, links


def _apply_edits(proposal: dict[str, Any], edits: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(edits) > MAX_EDITS:
        raise BadEdit(f"at most {MAX_EDITS} edits")
    objects, links = copy.deepcopy(proposal["objects"]), copy.deepcopy(proposal["links"])
    for edit in edits:
        objects, links = _apply_edit(edit, objects, links)
    if not objects:
        raise BadEdit("an ontology needs at least one object")
    return objects, links


def _merged_policies(pack: Pack, functions: dict[str, Any]) -> dict[str, Any]:
    """Keep what a human wrote. Replace our own rules. Permit reads by name, one
    at a time: a wildcard would also permit whatever gets added later."""
    kept = [r for r in pack.policies.get("rules") or [] if not str(r.get("id", "")).startswith(RULE_PREFIX)]
    ours = [{"id": f"{RULE_PREFIX}{n}", "effect": "permit", "principal": "user:*", "action": f"function:{n}"}
            for n in sorted(functions)]
    return {"default": "deny", "rules": kept + ours}


def _replace_file(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    write_private(tmp, text)
    os.replace(tmp, path)


def _claim_version(d: Path, record: dict[str, Any]) -> int:
    """O_EXCL, so two accepts at once take two numbers and neither overwrites."""
    private_dir(d)
    taken = [int(f.stem) for f in d.glob("*.json") if f.stem.isdigit()]
    version = max(taken, default=0) + 1
    while True:
        try:
            fd = os.open(d / f"{version:04d}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            version += 1
            continue
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": version, **record}, fh, indent=1, sort_keys=True)
        return version


def accept(org_id: str, slug: str, proposal_id: str, edits: list[Any], accepted_by: str) -> dict[str, int]:
    """Validate everything, then take the store, then write. A refusal at any of
    those steps leaves the workspace as it was."""
    pack = wt.load_workspace(org_id, slug)
    proposal = read_proposal(org_id, slug)
    if proposal is None or proposal["id"] != proposal_id:
        raise StaleProposal(proposal_id)
    objects, links = _apply_edits(proposal, edits)
    ontology = to_ontology(objects, links)
    policies = _merged_policies(pack, generated_functions(ontology))
    try:
        wt._ontology(ontology)
        wt._policies(policies)
    except wt.WorkspaceError as e:
        raise BadEdit(str(e)) from e
    try:
        store = OntologyStore(pack.paths.ensure().ontology_db)
    except duckdb.IOException as e:
        raise StoreBusy() from e
    try:
        version = _claim_version(pack.dir / VERSIONS_DIR, {
            "proposal_id": proposal_id, "edits": edits, "accepted_by": accepted_by, "accepted_at": round(time.time(), 3),
            "ontology": ontology, "policies": policies})
        _replace_file(pack.dir / "policies.yaml", yaml.safe_dump(policies, sort_keys=False))
        _replace_file(pack.dir / "ontology.yaml", yaml.safe_dump(ontology, sort_keys=False))
        store.audit(actor=accepted_by, action="ontology.accept", status="applied",
                    params={"proposal_id": proposal_id, "ontology_version": version, "edits": edits})
    finally:
        store.con.close()
    return {"ontology_version": version}
