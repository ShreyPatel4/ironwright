"""The gold contract, `gold.yaml`: the marts a business says it needs (docs/agentic-bridge.md).

A human writes it, or accepts a draft. Nothing in it says what is covered: coverage is
the bridge planner's call, from rules over the sources. So the schema is closed, and a key
it does not know (say `coverage: covered`) is refused rather than kept.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from ..tenancy.models import ROLES
from ..workspaces_tenant import _rejoin_decimal

MAX_BYTES = 256 * 1024
FILE = "gold.yaml"
MAX_ITEMS = 200
MAX_RULES = 50

_IDENT = re.compile(r"[a-z][a-z0-9_]{0,62}")
_USE_CASE_ID = re.compile(r"[a-z][a-z0-9-]{0,62}")
_TYPE = re.compile(r"string|int|bigint|double|float|boolean|bool|date|timestamp|decimal\((\d{1,2}),\s*(\d{1,2})\)")
CONSUMERS = ("bi", "file", "api", "reverse_etl")
MASKING = ("hash", "mask", "drop")
# What a rule may hold: column names, numbers, quoted words and comparison. Nothing that
# ends a statement or starts a comment, so a rule can become SQL later without surprises.
_RULE = re.compile(r"[a-z0-9_ <>=!.,'()+\-*/%]{1,200}")
_RULE_WORDS = frozenset({"and", "or", "not", "null", "is", "in", "true", "false", "between", "like"})
_RULE_STRING = re.compile(r"'[^']*'")
_RULE_NAME = re.compile(r"\b[a-z_][a-z0-9_]*\b")


class GoldError(ValueError):
    """A contract the bridge cannot plan against, in words a person can act on."""


def _mapping(v: Any, where: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise GoldError(f"{where} must be a mapping")
    return v


def _listing(v: Any, where: str, *, required: bool = True) -> list[Any]:
    if v is None and not required:
        return []
    if not isinstance(v, list):
        raise GoldError(f"{where} must be a list")
    if len(v) > MAX_ITEMS:
        raise GoldError(f"{where} holds more than {MAX_ITEMS} items")
    return v


def _only(d: dict[str, Any], allowed: set[str], where: str, required: set[str] = frozenset()) -> None:
    extra = sorted(str(k) for k in set(d) - allowed)
    if extra:
        raise GoldError(f"{where}: unknown field {extra[0]}. Coverage is the planner's call, not the contract's.")
    missing = sorted(required - set(d))
    if missing:
        raise GoldError(f"{where}: missing {missing[0]}")


def _ident(v: Any, where: str) -> str:
    if not isinstance(v, str) or not _IDENT.fullmatch(v):
        raise GoldError(f"{where}: {v!r} is not a name (lowercase letters, digits and underscores)")
    return v


def _text(v: Any, where: str, limit: int = 300) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise GoldError(f"{where} must be text of 1 to {limit} characters")
    return v.strip()


def _type(v: Any, where: str) -> str:
    t = " ".join(str(v).split()).lower() if isinstance(v, str) else None
    m = _TYPE.fullmatch(t or "")
    if not m:
        raise GoldError(f"{where}: type {v!r} is not one I know (string, int, double, bool, date, timestamp, decimal(p,s))")
    if m.group(1) is not None:
        p, s = int(m.group(1)), int(m.group(2))
        if not 0 < p <= 38 or s > p:
            raise GoldError(f"{where}: decimal({p},{s}) has a scale above its precision")
        return f"decimal({p},{s})"
    return "bool" if t == "boolean" else t


def _unique(names: list[str], where: str) -> None:
    seen: set[str] = set()
    for n in names:
        if n in seen:
            raise GoldError(f"{where}: {n} is declared twice")
        seen.add(n)


def _use_case(raw: Any, i: int) -> dict[str, Any]:
    where = f"use_cases[{i}]"
    d = _mapping(raw, where)
    _only(d, {"id", "audience", "question", "freshness", "consumer"}, where, {"id", "question"})
    if not isinstance(d["id"], str) or not _USE_CASE_ID.fullmatch(d["id"]):
        raise GoldError(f"{where}: id {d['id']!r} is lowercase letters, digits and dashes")
    out = {"id": d["id"], "audience": _text(d.get("audience") or "not named yet", f"{where}.audience"),
           "question": _text(d["question"], f"{where}.question", 600),
           "freshness": _text(d.get("freshness") or "not set", f"{where}.freshness")}
    consumer = _mapping(d.get("consumer") or {"type": "bi", "target": "not set"}, f"{where}.consumer")
    _only(consumer, {"type", "target"}, f"{where}.consumer", {"type"})
    if consumer["type"] not in CONSUMERS:
        raise GoldError(f"{where}.consumer: type {consumer['type']!r} is one of {', '.join(CONSUMERS)}")
    out["consumer"] = {"type": consumer["type"], "target": _text(consumer.get("target") or "not set", f"{where}.consumer.target")}
    return out


def _table(raw: Any, kind: str, i: int) -> dict[str, Any]:
    where = f"{kind}[{i}]"
    d = _mapping(raw, where)
    _only(d, {"name", "grain", "columns"}, where, {"name", "columns"})
    name = _ident(d["name"], where)
    where = f"{kind} {name}"
    cols = []
    for j, c in enumerate(_listing(d["columns"], f"{where}.columns")):
        cw = f"{where}.columns[{j}]"
        c = _rejoin_decimal(_mapping(c, cw))
        _only(c, {"name", "type", "key"}, cw, {"name", "type"})
        if not isinstance(c.get("key", False), bool):
            raise GoldError(f"{cw}: key is true or false")
        cols.append({"name": _ident(c["name"], cw), "type": _type(c["type"], cw), "key": c.get("key", False)})
    if not cols:
        raise GoldError(f"{where} needs at least one column")
    _unique([c["name"] for c in cols], where)
    keys = [c["name"] for c in cols if c["key"]]
    if kind == "dimensions" and len(keys) != 1:
        raise GoldError(f"{where} needs exactly one key column, so a fact can join it (it has {len(keys)})")
    return {"name": name, "grain": _text(d.get("grain") or "not stated", f"{where}.grain"), "columns": cols}


def _rule_names(rule: str, where: str) -> set[str]:
    if not _RULE.fullmatch(rule):
        raise GoldError(f"{where}: a rule holds column names, numbers, quoted words and comparisons only")
    bare = _RULE_STRING.sub("''", rule)
    if bare.count("'") % 2:
        raise GoldError(f"{where}: a rule has a quote that never closes")
    return {n for n in _RULE_NAME.findall(bare) if n not in _RULE_WORDS and not n[0].isdigit()}


def _mart(raw: Any, i: int, tables: dict[str, dict[str, Any]], use_cases: set[str]) -> dict[str, Any]:
    where = f"marts[{i}]"
    d = _mapping(raw, where)
    _only(d, {"name", "from", "use_case", "rules", "access", "grain"}, where, {"name", "from"})
    name = _ident(d["name"], where)
    where = f"mart {name}"
    sources = [_ident(t, f"{where}.from") for t in _listing(d["from"], f"{where}.from")]
    if not sources:
        raise GoldError(f"{where}.from names at least one dimension or fact")
    _unique(sources, f"{where}.from")
    for t in sources:
        if t not in tables:
            raise GoldError(f"{where}.from names {t}, which is not a declared dimension or fact")
    columns = {c["name"] for t in sources for c in tables[t]["columns"]}
    use_case = d.get("use_case")
    if use_case is not None and use_case not in use_cases:
        raise GoldError(f"{where}: use_case {use_case!r} is not one of the declared use cases")
    rules = []
    for j, r in enumerate(_listing(d.get("rules"), f"{where}.rules", required=False)[:MAX_RULES]):
        rule = " ".join(_text(r, f"{where}.rules[{j}]", 200).split())
        unknown = sorted(_rule_names(rule, f"{where}.rules[{j}]") - columns)
        if unknown:
            raise GoldError(f"{where}.rules[{j}] names {unknown[0]}, which no table in from has")
        rules.append(rule)
    access = _mapping(d.get("access") or {}, f"{where}.access")
    _only(access, {"roles", "masking"}, f"{where}.access")
    roles = _listing(access.get("roles") or ["member"], f"{where}.access.roles")
    for r in roles:
        if r not in ROLES:
            raise GoldError(f"{where}.access: role {r!r} is one of {', '.join(ROLES)}")
    masking = _mapping(access.get("masking") or {}, f"{where}.access.masking")
    for col, how in masking.items():
        if col not in columns:
            raise GoldError(f"{where}.access.masking names {col}, which no table in from has")
        if how not in MASKING:
            raise GoldError(f"{where}.access.masking: {how!r} is one of {', '.join(MASKING)}")
    out = {"name": name, "from": sources, "use_case": use_case, "rules": rules,
           "access": {"roles": list(dict.fromkeys(roles)), "masking": dict(sorted(masking.items()))}}
    if d.get("grain") is not None:
        out["grain"] = _text(d["grain"], f"{where}.grain")
    return out


def validate(raw: Any) -> dict[str, Any]:
    """The contract, normalized, or GoldError. Pure: no disk, no clock."""
    d = _mapping(raw, "the gold contract")
    _only(d, {"kind", "version", "use_cases", "dimensions", "facts", "marts"}, "the gold contract",
          {"kind", "version", "marts"})
    if d["kind"] != "gold-contract":
        raise GoldError("kind must be gold-contract")
    if d["version"] != 1:
        raise GoldError("version must be 1")
    use_cases = [_use_case(u, i) for i, u in enumerate(_listing(d.get("use_cases"), "use_cases", required=False))]
    _unique([u["id"] for u in use_cases], "use_cases")
    dims = [_table(t, "dimensions", i) for i, t in enumerate(_listing(d.get("dimensions"), "dimensions", required=False))]
    facts = [_table(t, "facts", i) for i, t in enumerate(_listing(d.get("facts"), "facts", required=False))]
    _unique([t["name"] for t in dims + facts], "dimensions and facts")
    tables = {t["name"]: t for t in dims + facts}
    marts = [_mart(m, i, tables, {u["id"] for u in use_cases}) for i, m in enumerate(_listing(d["marts"], "marts"))]
    if not marts:
        raise GoldError("declare at least one mart: the planner works backward from marts")
    _unique([m["name"] for m in marts] + list(tables), "marts, dimensions and facts")
    return {"kind": "gold-contract", "version": 1, "use_cases": use_cases, "dimensions": dims, "facts": facts, "marts": marts}


def parse(text: str) -> dict[str, Any]:
    if len(text.encode()) > MAX_BYTES:
        raise GoldError(f"gold.yaml is too large (the cap is {MAX_BYTES // 1024} KiB)")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise GoldError("gold.yaml is not valid YAML") from e
    return validate(raw)


def dump(contract: dict[str, Any]) -> str:
    return yaml.safe_dump(contract, sort_keys=False)
