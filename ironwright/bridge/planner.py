"""Two graphs the bridge meets in the middle (docs/agentic-bridge.md, phase B1).

The gold planner walks back from each mart column to the dimension or fact column it
needs, with that column's grain and how the mart gets it. The source graph is what the
profiler already learned about the uploaded files, laid over the workspace spec a human
approved and the silver names a human chose. No cell value enters either graph: the
profile's samples stay where they are.

```mermaid
flowchart LR
  G[gold.yaml] --> R[requirements<br/>mart column to dim or fact column]
  P[profile.json per upload] --> S[source graph<br/>file, bronze, silver columns, joins]
  W[workspace.yaml] --> S
  D[silver draft renames] --> S
  R --> C{coverage rules}
  S --> C
```
"""
from __future__ import annotations

import json
from typing import Any

from .. import workspaces_tenant as wt
from ..config import UnsafePath
from ..ingest import silver_draft, uploads

# ------------------------------------------------------------------ the gold side

def _tables(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {t["name"]: {**t, "kind": "dimension"} for t in contract["dimensions"]}
    out.update({t["name"]: {**t, "kind": "fact"} for t in contract["facts"]})
    return out


def _key(table: dict[str, Any]) -> str | None:
    return next((c["name"] for c in table["columns"] if c["key"]), None)


def _joins(mart: dict[str, Any], tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each dimension in a mart joins on its key, which the mart's first fact (or, with no
    fact, its first table) must carry under the same name."""
    facts = [t for t in mart["from"] if tables[t]["kind"] == "fact"]
    base = facts[0] if facts else mart["from"][0]
    out = []
    for t in mart["from"]:
        if t == base or tables[t]["kind"] != "dimension":
            continue
        key = _key(tables[t])
        has = any(c["name"] == key for c in tables[base]["columns"])
        out.append({"dimension": t, "on": key, "ok": has,
                    "reason": f"{base} carries {key}, the key of {t}." if has else
                    f"{base} has no {key} column, so nothing joins it to {t}. Add {key} to {base}."})
    return out


def requirements(contract: dict[str, Any]) -> dict[str, Any]:
    """The column requirement graph. Pure over a validated contract."""
    tables = _tables(contract)
    marts = []
    for m in contract["marts"]:
        facts = [t for t in m["from"] if tables[t]["kind"] == "fact"]
        base = facts[0] if facts else m["from"][0]
        grain = m.get("grain") or tables[base]["grain"]
        seen: set[str] = set()
        cols = []
        for t in m["from"]:
            for c in tables[t]["columns"]:
                if c["name"] in seen:
                    continue
                seen.add(c["name"])
                cols.append({"name": c["name"], "ref": f"{m['name']}.{c['name']}", "from": f"{t}.{c['name']}",
                             "type": c["type"], "aggregation": "lookup" if tables[t]["kind"] == "dimension" else "none",
                             "grain": grain})
        marts.append({"name": m["name"], "use_case": m["use_case"], "from": m["from"], "grain": grain,
                      "joins": _joins(m, tables), "rules": m["rules"], "access": m["access"], "columns": cols})
    return {"tables": [{"name": t["name"], "kind": t["kind"], "grain": t["grain"], "key": _key(t),
                        "columns": t["columns"]} for t in tables.values()],
            "marts": marts}


# ------------------------------------------------------------------ the source side

def _silver_names(silver: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """source -> {bronze column: silver column} for each source the kept draft models."""
    if not silver:
        return {}
    renames = silver.get("renames") or {}
    out = {}
    for s in (silver.get("draft") or {}).get("sources") or []:
        if "error" in s or not isinstance(s.get("name"), str):
            continue
        mine = renames.get(s["name"]) or {}
        out[s["name"]] = {c["name"]: mine.get(c["name"], c["name"]) for c in s.get("columns") or []
                          if isinstance(c, dict) and isinstance(c.get("name"), str)}
    return out


def source_graph(spec: dict[str, Any], profiles: dict[str, dict[str, Any]],
                 silver: dict[str, Any] | None) -> dict[str, Any]:
    """Pure: the workspace spec, profile.json per upload id, and the kept silver draft."""
    names = _silver_names(silver)
    tables, by_file = [], {}
    for src in spec.get("sources") or []:
        upload_id, _, file_name = src["upload"].partition("/")
        prof = profiles.get(upload_id) or {}
        f = next((x for x in prof.get("files") or [] if x.get("name") == file_name and "error" not in x), None)
        stats = {c["name"]: c for c in (f or {}).get("columns") or []}
        rows = f["rows"] if f else None
        silver_cols = names.get(src["name"])
        cols = []
        for c in src["columns"]:
            s = stats.get(c["name"])
            cols.append({
                "name": c["name"], "type": c["type"], "declared_key": bool(c.get("key")),
                "silver": None if silver_cols is None else silver_cols.get(c["name"]),
                "profiled": s is not None,
                "null_rate": s["null_rate"] if s else None,
                "unique": (s["distinct"] == rows and not s["nulls"]) if s and rows else None,
            })
        tables.append({"name": src["name"], "upload": upload_id, "file": file_name, "rows": rows,
                       "silver": silver_cols is not None, "columns": cols})
        by_file[(upload_id, file_name)] = src["name"]
    joins = []
    for upload_id, prof in sorted(profiles.items()):
        for j in prof.get("candidate_joins") or []:
            sides = []
            for side in (j["left"], j["right"]):
                file_name, _, column = side.rpartition(".")
                table = by_file.get((upload_id, file_name))
                if table is None:
                    break
                sides.append(f"{table}.{column}")
            if len(sides) == 2:
                joins.append({"left": sides[0], "right": sides[1], "confidence": j["confidence"]})
    return {"tables": tables, "joins": joins}


def _read_profile(org_id: str, upload_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((uploads.upload_dir(org_id, upload_id) / "profile.json").read_text())
    except (FileNotFoundError, ValueError, UnsafePath):
        return None


def workspace_sources(org_id: str, slug: str) -> dict[str, Any]:
    """The source graph of one of this org's workspaces. FileNotFoundError when it has none."""
    spec = wt.load_workspace(org_id, slug).spec
    profiles = {}
    for src in spec["sources"]:
        upload_id = src["upload"].partition("/")[0]
        if upload_id not in profiles:
            got = _read_profile(org_id, upload_id)
            if got is not None:
                profiles[upload_id] = got
    return source_graph(spec, profiles, silver_draft.read_saved(org_id, slug))
