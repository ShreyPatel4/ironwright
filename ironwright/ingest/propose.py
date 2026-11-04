"""A profile becomes a workspace.yaml draft the user edits and approves.

`workspace` is contract 3 and holds nothing else, so T2's schema can validate it.
`decisions` says why, one entry per choice, with a confidence and a reason. What
contract 3 cannot say yet (a date format, a trailer to skip) is a decision with
`contract_gap: true`, so it is on the record instead of lost.

An LLM may narrate this. No cell value ever reaches it: names are sanitized by
`profiler.snake`, and reasons carry counts and rule names only. A sanitized name
is still tenant text, so a narrator must fence it like any other.
"""
from __future__ import annotations

from typing import Any

from ..legacy import UPLOAD_ENCODINGS

_TEMPORAL = ("date", "timestamp")
# The profile says bool, and contract 3's reader spells it boolean.
_CONTRACT_TYPE = {"bool": "boolean"}


def _source_name(base: str, used: set[str]) -> str:
    name, n = base, 1
    while name in used:
        n += 1
        name = f"{base}_{n}"
    used.add(name)
    return name


def _decision(path: str, value: Any, confidence: float, reason: str, gap: bool = False) -> dict[str, Any]:
    d = {"path": path, "value": value, "confidence": confidence, "reason": reason}
    if gap:
        d["contract_gap"] = True
    return d


def _column(col: dict[str, Any], path: str, is_key: bool, key: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    mixed = len(col.get("formats", {})) > 1
    kind = "string" if mixed else col["type"]
    reason = col["reason"]
    if mixed:
        reason += " A column has one type, so I propose string and leave the formats for a silver model to normalize."
    kind = _CONTRACT_TYPE.get(kind, kind)
    out: dict[str, Any] = {"name": col["name"], "type": kind}
    decisions = [_decision(f"{path}.type", kind, col["confidence"], reason)]
    if col["type"] in _TEMPORAL and not mixed:
        decisions.append(_decision(f"{path}.format", col["format"], col["confidence"],
                                   "Contract 3 has no field for a date format, so the format is recorded here.", gap=True))
    if is_key and key:
        out["key"] = True
        decisions.append(_decision(f"{path}.key", True, key["confidence"], key["reason"]))
    return out, decisions


def _source(f: dict[str, Any], i: int, upload_id: str, used: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    at = f"sources[{i}]"
    key = f["candidate_keys"][0] if f["candidate_keys"] else None
    decisions = [
        _decision(f"{at}.format", f["format"], f["format_confidence"], f["format_reason"]),
        _decision(f"{at}.layout.encoding", f["layout"]["encoding"], f["encoding_confidence"], f["encoding_reason"]),
        _decision(f"{at}.layout.header", f["layout"]["header"], f["header_confidence"], f["header_reason"]),
    ]
    layout: dict[str, Any] = {"encoding": f["layout"]["encoding"]}
    if f["format"] == "fixed_width":
        if f["layout"]["header"]:  # contract 3 gives a fixed width source no header
            decisions[-1]["contract_gap"] = True
            decisions[-1]["reason"] += " Contract 3 cannot skip a fixed width header yet, so bronze will carry it as a row."
        fields = f["layout"]["fields"]
        layout["fields"] = [{"name": x["name"], "start": x["start"], "width": x["width"]} for x in fields]
        how = sorted({x["boundary"] for x in fields})
        decisions.append(_decision(f"{at}.layout.fields", len(fields), min(x["confidence"] for x in fields),
                                   f"Boundaries come from column alignment, by these signals: {', '.join(how)}. "
                                   "Two abutting text fields read as one, so check the widths against a copybook."))
    else:
        layout["header"] = f["layout"]["header"]
        layout["delimiter"] = f["layout"]["delimiter"]
        decisions.append(_decision(f"{at}.layout.delimiter", f["layout"]["delimiter"], f["format_confidence"], f["format_reason"]))
    gaps = (("skip_header_records", f["leader_rows"], "Records before the data look like a header record. Contract 3 cannot skip them yet."),
            ("skip_footer", f["trailer_rows"], "Rows after the data look like a trailer. Contract 3 cannot skip them yet, so bronze will carry them until it can."),
            ("bad_rows", f["bad_rows"]["count"], "Rows with the wrong shape were left out of the profile. Contract 3 cannot say what to do with them yet."))
    decisions += [_decision(f"{at}.layout.{name}", n, 0.9, why, gap=True) for name, n, why in gaps if n]
    if f["truncated"]:
        decisions.append(_decision(f"{at}.rows", f["rows"], 0.5, "The file is over the profiling cap, so this reads only the first part of it."))
    columns = []
    for j, col in enumerate(f["columns"]):
        out, more = _column(col, f"{at}.columns[{j}]", bool(key) and col["name"] == key["column"], key)
        columns.append(out)
        decisions += more
    source = {"name": _source_name(f["source"], used), "upload": f"{upload_id}/{f['name']}", "format": f["format"],
              "layout": layout, "columns": columns}
    return source, decisions


def propose(profile: dict[str, Any], upload_id: str) -> dict[str, Any]:
    """{workspace, decisions}. Pure: the same profile always gives the same draft."""
    sources: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    used: set[str] = set()
    for f in profile["files"]:
        if "error" in f:
            decisions.append(_decision(f"files.{f['name']}", "left out", 1.0, f["error"]))
            continue
        if f["layout"]["encoding"] not in UPLOAD_ENCODINGS:
            decisions.append(_decision(f"files.{f['name']}", "left out", 1.0,
                                       f"The run cannot read {f['layout']['encoding']}, so convert the file to UTF-8."))
            continue
        source, more = _source(f, len(sources), upload_id, used)
        sources.append(source)
        decisions += more
    for name in profile["skipped"]:
        decisions.append(_decision(f"files.{name}", "left out", 1.0, "The upload holds more files than one profile reads."))
    for k, j in enumerate(profile["candidate_joins"]):
        decisions.append(_decision(f"candidate_joins[{k}]", {"left": j["left"], "right": j["right"]}, j["confidence"],
                                   j["reason"] + " Contract 3 has no joins, so this waits for the ontology step."))
    shown = [s["name"] for s in sources[:3]]
    more_files = f" and {len(sources) - 3} more" if len(sources) > 3 else ""
    title = f"Draft from {', '.join(shown)}{more_files}" if shown else "Empty draft"
    return {"workspace": {"kind": "workspace", "version": 1, "title": title, "sources": sources,
                          "quality": [], "reconcile": []},
            "decisions": decisions}
