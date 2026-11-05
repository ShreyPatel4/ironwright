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
- A person names a record by the key in their own file, so an id resolves through
  the crosswalk as well as the ontology id. It resolves to exactly one object or it
  does not resolve. The call still takes the ontology id, so a digest does not depend
  on how the question spelled the record.
- The route is always the deterministic one. A model, only when a caller hands
  one in, runs after the call has happened and receives strings, so it cannot
  pick the function or an argument. It may only narrate, inside the fences in
  llm.py, and the guardrail in agents/guardrail.py has the last word. The offline
  template passes that same guardrail, because it interpolates cell values too.
- Every answer carries its calls (function, args, result digest) and lands
  in an append-only ask log, so any answer can be REPLAYED: run the same
  calls again and compare digests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .agents.guardrail import scrub_value, settle
from .config import private_dir
from .llm import LLM, boundary_rule, fence_record, new_nonce, sanitize, untrusted
from .ontology.runtime import Ontology
from .packs import Pack

QUESTION_CAP = 500
_WORD = re.compile(r"[a-z0-9_-]+")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")  # case-preserving, for id resolution
# A key can hold a dot, slash or colon. Sentence punctuation after it is not part of it.
_KEY_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_./:-]*[A-Za-z0-9_])?")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_FAKE_MIDNIGHT = re.compile(r"(\d{4}-\d{2}-\d{2})[ T]00:00:00(?:\.0+)?(?:Z|[+-]00:?00)?")
_AGGREGATE = re.compile(r"\b(how many|counts?|totals?|sums?|averages?|avg|mean|median|per|each)\b", re.IGNORECASE)
# Words that ask for a record and say nothing about which one or what about it.
_LOOKUP_WORDS = {"id", "get", "find", "look", "lookup", "up", "by", "its", "one", "record", "number", "key", "no"}
_CAN_DO_SHOWN = 4
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


def _scalar(ptype: Any) -> bool:
    return not isinstance(ptype, str) or ptype in ("int", "float", "str", "bool")


def _type_words(ptype: str) -> list[str]:
    return _CAMEL.sub(" ", ptype).replace("_", " ").lower().split()


def _objects_named(ont: Ontology, ptype: str, token: str) -> set[str]:
    """Which objects this spelling names: the ontology id, or a key in the crosswalk. Exact
    first, because in a file that mixes case, ab-1 and AB-1 can be two records. Other case
    only when the exact spelling names nothing, and both foldings count, so it cannot pick."""
    def exact(tok: str) -> set[str]:
        hits = {r[0] for r in ont.store.con.execute(
            "select distinct id from crosswalk where type = ? and source_key = ?", [ptype, tok]).fetchall()}
        return hits | ({tok} if ont.store.get(ptype, tok) else set())
    return exact(token) or exact(token.upper()) | exact(token.lower())


def _resolve_params(question: str, params: dict[str, str], ont: Ontology) -> tuple[dict[str, Any], dict[str, str]] | None:
    """(ontology ids for the call, the words the person used for them), or None. Every
    required param must name exactly one real object in the question. Conservative by
    design: no guessing, no fuzzy ids, and no picking between two records. Short integer
    keys are why: longest first would answer for 1001 when someone asked about 1001 and 7."""
    args: dict[str, Any] = {}
    named: dict[str, str] = {}
    for pname, ptype in params.items():
        if _scalar(ptype):
            return None  # scalar params are not inferable from prose; refuse
        hits: dict[str, str] = {}
        for whole in _KEY_TOKEN.findall(question):
            found = _objects_named(ont, ptype, whole)
            if found:
                hits.update({oid: whole for oid in found})
                continue
            for part in _TOKEN.findall(whole):  # customer:1001 names 1001
                hits.update({oid: part for oid in _objects_named(ont, ptype, part)})
        if len(hits) != 1:
            return None
        args[pname], named[pname] = next(iter(hits.items()))
    return args, named


def _named_words(fn: dict[str, Any], named: dict[str, str]) -> dict[str, str]:
    return {p: f"{' '.join(_type_words(t))} {named[p]}" for p, t in fn["params"].items()}


def _can_do(pack: Pack, ont: Ontology, principal: str | None) -> str:
    """What this workspace can answer, for this person. A refusal should not name a
    function policy would deny them, so the engine rules here as on the call."""
    allowed = [f["description"].rstrip(". ") for f in registry(pack) if f["description"] and
               (principal is None or ont.policy.authorize(principal, f"function:{f['name']}", {}).allowed)]
    if not allowed:
        return ""
    more = len(allowed) - _CAN_DO_SHOWN
    tail = f"; and {more} more" if more > 0 else ""
    return f" On this workspace I can: {'; '.join(allowed[:_CAN_DO_SHOWN])}{tail}."


def _refused(code: str, reason: str) -> dict[str, Any]:
    return {"status": "refused", "reason_code": code, "reason": reason}


def _bare_lookup(question: str, pack: Pack, ont: Ontology) -> dict[str, Any] | None:
    """`show me customer 1001` has one real word, so word overlap can never route it. It
    asks for a record of a type, and nothing else. So: the question names a type, the only
    other words are ones that just ask for a record, and the pack has exactly one read of
    one record of that type. A further word means it is asking something else, and word
    overlap decides. Returns a route, a refusal, or None to say this rule does not apply."""
    qtok = set(_tokens(question))
    getters: dict[str, list[dict[str, Any]]] = {}
    fdefs = pack.ontology.get("functions", {}) or {}
    for fn in registry(pack):
        types = list(fn["params"].values())
        if fdefs[fn["name"]].get("returns") == "one" and len(types) == 1 and not _scalar(types[0]):
            getters.setdefault(types[0], []).append(fn)
    named_types = [t for t in getters if set(_type_words(t)) <= qtok]
    if len(named_types) != 1 or len(getters[named_types[0]]) != 1:
        return None
    ptype = named_types[0]
    fn = getters[ptype][0]
    resolved = _resolve_params(question, fn["params"], ont)
    if resolved is None:
        word = " ".join(_type_words(ptype))
        return _refused("unresolved_params", f"You named a {word}, but I could not find exactly one with that id. "
                                             f"Use one id from your data. It needs: {', '.join(fn['params'])}.")
    args, named = resolved
    used = {t for key in named.values() for t in _tokens(key)}
    if qtok - used - set(_type_words(ptype)) - _LOOKUP_WORDS:
        return None
    return {"status": "routed", "function": fn["name"], "args": args, "named": _named_words(fn, named),
            "description": fn["description"]}


def route_offline(question: str, pack: Pack, ont: Ontology, principal: str | None = None) -> dict[str, Any]:
    route = _route(question, pack, ont, principal)
    if route["status"] == "refused" and route["reason_code"] != "ambiguous" and _AGGREGATE.search(question):
        return _refused("aggregate_unsupported",
                        "I do not do counts, totals or averages here yet. I read one record, or the list "
                        f"on one record, by an id from your data.{_can_do(pack, ont, principal)}")
    return route


def _route(question: str, pack: Pack, ont: Ontology, principal: str | None) -> dict[str, Any]:
    lookup = _bare_lookup(question, pack, ont)
    if lookup is not None and lookup["status"] == "routed":
        return lookup
    qtok = set(_tokens(question))
    scored = []
    for fn in registry(pack):
        ftok = set(_tokens(fn["name"])) | set(_tokens(fn["description"]))
        score = len(qtok & ftok)
        if score >= 2:
            scored.append((score, fn["name"], fn))
    if not scored:
        return lookup or _refused("out_of_scope", "No registered question matches this. Ask routes only to the "
                                                  "workspace's governed functions; it never guesses."
                                                  f"{_can_do(pack, ont, principal)}")
    scored.sort(key=lambda s: (-s[0], s[1]))
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        names = ", ".join(s[1] for s in scored if s[0] == scored[0][0])
        return _refused("ambiguous", f"The question matches more than one function equally ({names}). "
                                     "Say which, or name the measure you want.")
    fn = scored[0][2]
    resolved = _resolve_params(question, fn["params"], ont)
    if resolved is None:
        need = ", ".join(f"{k} ({v})" for k, v in fn["params"].items())
        return _refused("unresolved_params", f"Matched {fn['name']} but could not resolve every required id "
                                             f"from the question. It needs: {need}. Use one id from your data "
                                             "for each; nothing is guessed.")
    args, named = resolved
    return {"status": "routed", "function": fn["name"], "args": args, "named": _named_words(fn, named),
            "description": fn["description"]}


def _visible(row: dict[str, Any]) -> dict[str, Any]:
    """What a person reads: the row's declared properties, not the store's own columns."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _label(column: str) -> str:
    words = column.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _shown(value: Any) -> str:
    """A date typed as a timestamp reads as a date. A real time of day stays."""
    text = str(value)
    midnight = _FAKE_MIDNIGHT.fullmatch(text)
    return midnight.group(1) if midnight else text


def _narrate_offline(description: str, named: dict[str, str], result: Any) -> str:
    """Rendering only. The rows and their digest are untouched, so replay still holds."""
    who = ", ".join(scrub_value(v) for v in named.values())
    lines = [f"{description} For {who}:" if who else description]
    rows = result["rows"]
    if not rows:
        lines.append("  no matching rows")
    for row in rows[:5]:
        cells = [f"{scrub_value(_label(k))}: {scrub_value(_shown(v))}" for k, v in _visible(row).items() if v is not None]
        lines.append("  " + ", ".join(cells))
    if len(rows) > 5:
        lines.append(f"  ... and {len(rows) - 5} more rows")
    return "\n".join(lines)


def _narration_prompt(question: str, description: str, named: dict[str, str],
                      result: dict[str, Any], nonce: str) -> str:
    rows = "\n".join(fence_record(_visible(r), "cell", nonce) for r in result["rows"][:5])
    return (f"Question: {untrusted(question, 'question', nonce, cap=QUESTION_CAP)}\n"
            f"Function: {result['function']}: {description}\n"
            f"Arguments:\n{fence_record(named, 'argument', nonce)}\n"
            f"Result, cite as [ev:{result['id']}], {len(result['rows'])} rows:\n{rows}\n"
            "Answer the question in 1 to 3 plain sentences from this result only. "
            "Cite every fact with its [ev:ID]. Do not invent numbers.")


def ask(pack: Pack, ont: Ontology, question: str, principal: str = "user:console",
        llm: LLM | None = None, log_path: Path | None = None) -> dict[str, Any]:
    """llm is opt-in. Configuring a provider for the agents does not start
    sending a user's cells to it from here; a caller has to hand one in.
    log_path is where this asker's log lives when the pack's own folder is shared."""
    question = sanitize(question)
    entry: dict[str, Any] = {"ts": round(time.time(), 3), "question": question[:QUESTION_CAP],
                             "principal": principal}
    if len(question) > QUESTION_CAP:
        entry.update(status="refused", reason_code="question_too_long",
                     reason=f"The question is over {QUESTION_CAP} characters. Ask for one thing, by id.")
        _log(pack, entry, log_path)
        return entry
    route = route_offline(question, pack, ont, principal)
    if route["status"] == "refused":
        entry.update(status="refused", reason_code=route["reason_code"],
                     reason=route["reason"])
        _log(pack, entry, log_path)
        return entry
    try:
        result = ont.call(route["function"], route["args"], principal=principal)
    except PermissionError as e:
        entry.update(status="refused", reason_code="policy_denied", reason=str(e))
        _log(pack, entry, log_path)
        return entry
    call = {"call_id": "c1", "function": route["function"], "args": route["args"],
            "evidence_id": result["id"], "result_digest": _digest(result)}
    template = f"{_narrate_offline(route['description'], route['named'], result)}\n[ev:{result['id']}]"
    known = {result["id"]}
    answer, _, reasons, cited = settle(None, template, known)
    narrator = "offline-template"
    if llm is not None and not llm.offline:
        frozen = _digest(call)
        nonce = new_nonce()
        res = llm.complete(f"You narrate one governed answer.\n{boundary_rule(nonce)}",
                           _narration_prompt(question, route["description"], route["named"], result, nonce))
        if res.text:
            answer, grounded, reasons, cited = settle(res.text, template, known, known)
            narrator = res.model if grounded else narrator
        if _digest(call) != frozen:  # not an assert statement: python -O must not strip it
            raise AssertionError("the governed call changed during narration")
    entry.update(status="answered", answer=answer, narrator=narrator, citations=cited,
                 calls=[call], result=result)
    if reasons:
        entry["guardrail"] = reasons
    _log(pack, {k: v for k, v in entry.items() if k != "result"}, log_path)
    return entry


def _log(pack: Pack, entry: dict[str, Any], log_path: Path | None = None) -> None:
    path = log_path or pack.paths.ensure().reports / "ask_log.jsonl"
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_log(pack: Pack, log_path: Path | None = None) -> list[dict[str, Any]]:
    path = log_path or pack.paths.reports / "ask_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def metrics(pack: Pack, log_path: Path | None = None) -> dict[str, Any]:
    log = read_log(pack, log_path)
    answered = sum(1 for e in log if e.get("status") == "answered")
    refused = sum(1 for e in log if e.get("status") == "refused")
    total = answered + refused
    return {"asked": total, "answered": answered, "refused": refused,
            "refusal_rate": round(refused / total, 3) if total else None,
            "note": "Refusals are a feature: Ask answers only through governed "
                    "functions and never guesses."}


def _example_key(ont: Ontology, ptype: str, oid: str) -> str:
    """The key this object goes by in the customer's file, if it names this object alone
    and reads as one word. Otherwise the ontology id, which always does."""
    keys = ont.store.con.execute("select source_key from crosswalk where type = ? and id = ? order by system, source_key",
                                 [ptype, oid]).fetchall()
    for (key,) in keys:
        if key and _KEY_TOKEN.fullmatch(key) and _objects_named(ont, ptype, key) == {oid}:
            return key
    return oid


def _example_question(fn: dict[str, Any], names: dict[str, str]) -> str:
    stem = fn["description"].rstrip(".")
    tail = " ".join(f"{'for' if i == 0 else 'in'} {' '.join(_type_words(ptype))} {names[p]}"
                    for i, (p, ptype) in enumerate(fn["params"].items()))
    return f"{stem} {tail}".strip()


def examples(pack: Pack, ont: Ontology, limit: int = 3,
             principal: str = "user:console") -> list[dict[str, Any]]:
    """Ready-to-ask questions composed from the registry plus real records, named the way
    the customer's file names them, so a new user's first click answers instead of
    refusing. Only functions the asking principal is permitted are offered; the policy
    engine rules here exactly as it does on the call itself. A question goes on offer
    only once the router has sent it to the function it was written for."""
    engine = ont.policy
    out = []
    for fn in registry(pack):
        if len(out) >= limit:
            break
        if not engine.authorize(principal, f"function:{fn['name']}", {}).allowed:
            continue
        ids = {}
        ok = True
        for pname, ptype in (fn["params"] or {}).items():
            if _scalar(ptype):
                ok = False
                break
            row = ont.store.con.execute(
                "select id from objects where type = ? order by id limit 1", [ptype]
            ).fetchone()
            if not row:
                ok = False
                break
            ids[pname] = row[0]
        if not ok:
            continue
        keys = {p: _example_key(ont, fn["params"][p], oid) for p, oid in ids.items()}
        for names in (keys, ids):
            question = _example_question(fn, names)
            if route_offline(question, pack, ont).get("function") == fn["name"]:
                out.append({"question": question, "function": fn["name"]})
                break
    return out


def replay(pack: Pack, ont: Ontology, index: int, log_path: Path | None = None) -> dict[str, Any]:
    log = read_log(pack, log_path)
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
