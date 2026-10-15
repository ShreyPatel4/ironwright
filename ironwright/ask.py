"""Ask: a question in, a governed answer out, or an honest refusal.

The contract, which is the whole product here:

- The ROUTE is constrained to the pack's registered ontology functions.
  Nothing else is callable, so a question can only ever become a governed
  read. Actions are never routed; Ask reads.
- OFFLINE (the default) the router is deterministic and conservative: it
  answers only when exactly one function clearly wins on word overlap AND
  every required parameter resolves to a real object named in the question.
  Anything else is a refusal with a named reason. A refusal is a feature;
  a confident wrong answer is the failure this surface cannot have.
- With a model configured, the model may pick the function and arguments,
  but only from the same registry, validated the same way. The model never
  composes the answer's facts; it may only narrate them. (Model routing
  lands behind the same validation; offline is the reference behavior.)
- Every answer carries its calls (function, args, result digest) and lands
  in an append-only ask log, so any answer can be REPLAYED: run the same
  calls again and compare digests.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from .ontology.runtime import Ontology
from .packs import Pack

_WORD = re.compile(r"[a-z0-9_-]+")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")  # case-preserving, for id resolution
_STOP = {"the", "a", "an", "for", "in", "on", "of", "and", "or", "to", "is",
         "what", "which", "how", "why", "me", "show", "tell", "about", "with",
         "at", "this", "that", "over", "last", "days", "day"}


def registry(pack: Pack) -> list[dict[str, Any]]:
    fns = pack.ontology.get("functions", {}) or {}
    return [{"name": n, "description": d.get("description", ""),
             "params": d.get("params", {}) or {}} for n, d in fns.items()]


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _resolve_params(question: str, params: dict[str, str], ont: Ontology) -> dict[str, Any] | None:
    """Every required param must resolve to a real object whose id appears in
    the question. Conservative by design: no guessing, no fuzzy ids."""
    raw = _TOKEN.findall(question)
    candidates = set(raw) | {t.upper() for t in raw} | {t.lower() for t in raw}
    out: dict[str, Any] = {}
    for pname, ptype in params.items():
        if not isinstance(ptype, str) or ptype in ("int", "float", "str", "bool"):
            return None  # scalar params are not inferable from prose; refuse
        hit = None
        for tok in sorted(candidates, key=len, reverse=True):
            if ont.store.get(ptype, tok):
                hit = tok
                break
            if ont.store.get(ptype, tok.upper()):
                hit = tok.upper()
                break
        if hit is None:
            return None
        out[pname] = hit
    return out


def route_offline(question: str, pack: Pack, ont: Ontology) -> dict[str, Any]:
    qtok = set(_tokens(question))
    scored = []
    for fn in registry(pack):
        ftok = set(_tokens(fn["name"])) | set(_tokens(fn["description"]))
        score = len(qtok & ftok)
        if score >= 2:
            scored.append((score, fn["name"], fn))
    if not scored:
        return {"status": "refused", "reason_code": "out_of_scope",
                "reason": "No registered question matches this. Ask routes only to the "
                          "workspace's governed functions; it never guesses."}
    scored.sort(key=lambda s: (-s[0], s[1]))
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        names = ", ".join(s[1] for s in scored if s[0] == scored[0][0])
        return {"status": "refused", "reason_code": "ambiguous",
                "reason": f"The question matches more than one function equally ({names}). "
                          "Say which, or name the measure you want."}
    fn = scored[0][2]
    args = _resolve_params(question, fn["params"], ont)
    if args is None:
        need = ", ".join(f"{k} ({v})" for k, v in fn["params"].items())
        return {"status": "refused", "reason_code": "unresolved_params",
                "reason": f"Matched {fn['name']} but could not resolve every required id "
                          f"from the question. It needs: {need}. Use real ids; nothing is guessed."}
    return {"status": "routed", "function": fn["name"], "args": args,
            "description": fn["description"]}


def _narrate_offline(description: str, args: dict[str, Any], result: Any) -> str:
    lines = [f"{description} For {json.dumps(args)}:"]
    rows = result if isinstance(result, list) else [result]
    for row in rows[:5]:
        if isinstance(row, dict):
            lines.append("  " + ", ".join(f"{k}={row[k]}" for k in row))
        else:
            lines.append(f"  {row}")
    if isinstance(result, list) and len(result) > 5:
        lines.append(f"  … and {len(result) - 5} more rows")
    return "\n".join(lines)


def ask(pack: Pack, ont: Ontology, question: str,
        principal: str = "user:console") -> dict[str, Any]:
    route = route_offline(question, pack, ont)
    entry: dict[str, Any] = {"ts": round(time.time(), 3), "question": question,
                             "principal": principal}
    if route["status"] == "refused":
        entry.update(status="refused", reason_code=route["reason_code"],
                     reason=route["reason"])
        _log(pack, entry)
        return entry
    try:
        result = ont.call(route["function"], route["args"], principal=principal)
    except PermissionError as e:
        entry.update(status="refused", reason_code="policy_denied", reason=str(e))
        _log(pack, entry)
        return entry
    call = {"call_id": "c1", "function": route["function"], "args": route["args"],
            "result_digest": _digest(result)}
    entry.update(status="answered",
                 answer=_narrate_offline(route["description"], route["args"], result),
                 narrator="offline-template",
                 calls=[call], result=result)
    _log(pack, {k: v for k, v in entry.items() if k != "result"})
    return entry


def _log(pack: Pack, entry: dict[str, Any]) -> None:
    path = pack.paths.ensure().reports / "ask_log.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_log(pack: Pack) -> list[dict[str, Any]]:
    path = pack.paths.reports / "ask_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def metrics(pack: Pack) -> dict[str, Any]:
    log = read_log(pack)
    answered = sum(1 for e in log if e.get("status") == "answered")
    refused = sum(1 for e in log if e.get("status") == "refused")
    total = answered + refused
    return {"asked": total, "answered": answered, "refused": refused,
            "refusal_rate": round(refused / total, 3) if total else None,
            "note": "Refusals are a feature: Ask answers only through governed "
                    "functions and never guesses."}


def examples(pack: Pack, ont: Ontology, limit: int = 3,
             principal: str = "user:console") -> list[dict[str, Any]]:
    """Ready-to-ask questions composed from the registry plus real object
    ids, so a new user's first click answers instead of refusing. Only
    functions the asking principal is permitted are offered; the policy
    engine rules here exactly as it does on the call itself."""
    from .policy import PolicyEngine

    engine = PolicyEngine(pack.policies)
    out = []
    for fn in registry(pack):
        if len(out) >= limit:
            break
        if not engine.authorize(principal, f"function:{fn['name']}", {}).allowed:
            continue
        args = {}
        ok = True
        for pname, ptype in (fn["params"] or {}).items():
            if not isinstance(ptype, str) or ptype in ("int", "float", "str", "bool"):
                ok = False
                break
            row = ont.store.con.execute(
                "select id from objects where type = ? order by id limit 1", [ptype]
            ).fetchone()
            if not row:
                ok = False
                break
            args[pname] = row[0]
        if not ok:
            continue
        stem = fn["description"].rstrip(".")
        tail = " ".join(f"for {ptype.lower()} {args[p]}" if i == 0 else f"in {ptype.lower()} {args[p]}"
                        for i, (p, ptype) in enumerate(fn["params"].items()))
        out.append({"question": f"{stem} {tail}".strip(), "function": fn["name"]})
    return out


def replay(pack: Pack, ont: Ontology, index: int) -> dict[str, Any]:
    log = read_log(pack)
    if not (0 <= index < len(log)):
        raise IndexError(f"no ask log entry {index}")
    entry = log[index]
    if entry.get("status") != "answered":
        return {"replayable": False, "reason": "refusals have no calls to replay"}
    checks = []
    for call in entry.get("calls", []):
        result = ont.call(call["function"], call["args"],
                          principal=entry.get("principal", "user:console"))
        checks.append({"call_id": call["call_id"],
                       "expected_digest": call["result_digest"],
                       "actual_digest": _digest(result),
                       "match": _digest(result) == call["result_digest"]})
    return {"replayable": True, "match": all(c["match"] for c in checks), "calls": checks}
