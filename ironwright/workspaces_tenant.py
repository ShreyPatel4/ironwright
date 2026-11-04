"""Tenant workspaces: contract 3 of docs/saas-mvp.md.

A workspace is a pack with no generator. Its sources point at the org's own
uploads and everything in it is authored by the tenant, so all of it is
untrusted. Every load validates again, because the files on disk are the
source of truth and they can change after create.

What the tenant can write is narrower than what a shipped pack can, on purpose:

  quality     structural checks only, built from plain names. No expressions:
              the audit connection in lake.write_wap has no external access lock.
  SQL         one SELECT, and it only ever runs on lake.connect, which is locked.
  ontology    objects and links. No functions (they run on the unlocked ontology
              store connection) and no actions yet.

The run carries structural and reconciliation checks only. There is no ground
truth here, so identity and agent evals would pass with nothing to compare.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from .config import (
    UnsafePath,
    ensure_tenant,
    private_dir,
    upload_file,
    workspace_dir,
    workspaces_root,
    write_private,
)
from .legacy import UPLOAD_ENCODINGS
from .packs import Pack
from .quality import STRUCTURAL
from .sqlguard import SqlRejected, guard_select

MARKER = ".ironwright-tenant-workspace"
MAX_YAML_BYTES = 1024 * 1024
MODEL_LAYERS = ("silver", "gold")

_IDENT = re.compile(r"[a-z][a-z0-9_]{0,62}")
_TABLE = re.compile(r"(bronze|silver|gold)\.[a-z][a-z0-9_]{0,62}")
_MODEL_FILE = re.compile(r"[a-z0-9][a-z0-9_]{0,62}\.sql")
_TYPE = re.compile(r"string|varchar|integer|int|bigint|double|float|boolean|date|timestamp|decimal\(\d{1,2},\s*\d{1,2}\)")
_VALUE = re.compile(r"[^'\\\x00]{0,200}")


class WorkspaceError(ValueError):
    """The spec is wrong. The message is ours and is safe to show."""


def _bad(msg: str) -> WorkspaceError:
    return WorkspaceError(msg)


def _mapping(v: Any, where: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise _bad(f"{where}: expected a mapping")
    return v


def _listing(v: Any, where: str) -> list[Any]:
    if not isinstance(v, list):
        raise _bad(f"{where}: expected a list")
    return v


def _only(d: dict[str, Any], allowed: set[str], where: str, required: set[str] = frozenset()) -> None:
    unknown = sorted(str(k) for k in set(d) - allowed)
    if unknown:
        raise _bad(f"{where}: unknown key {', '.join(unknown)}")
    absent = sorted(required - set(d))
    if absent:
        raise _bad(f"{where}: missing {', '.join(absent)}")


def _ident(v: Any, where: str) -> str:
    if not isinstance(v, str) or not _IDENT.fullmatch(v):
        raise _bad(f"{where}: must be a plain name (lowercase letter first, then letters, digits, _)")
    return v


def _text(v: Any, where: str, limit: int = 200) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise _bad(f"{where}: must be text of at most {limit} characters")
    return v


def _select(sql: Any, where: str) -> str:
    if not isinstance(sql, str):
        raise _bad(f"{where}: must be SQL text")
    try:
        return guard_select(sql)
    except SqlRejected as e:
        raise _bad(f"{where}: {e}") from e


def _int(v: Any, where: str, minimum: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise _bad(f"{where}: must be a whole number of at least {minimum}")
    return v


# ---------------------------------------------------------------- sources

def _rejoin_decimal(c: dict[str, Any]) -> dict[str, Any]:
    """Contract 3 writes {name: amount, type: decimal(12,2)}. YAML splits that
    flow mapping at the comma into type: decimal(12 and a bare key 2). Put the
    type back so the contract's own spelling loads."""
    if not (isinstance(c.get("type"), str) and re.fullmatch(r"decimal\(\d+", c["type"])):
        return c
    tails = [k for k, v in c.items() if v is None and isinstance(k, (str, int)) and re.fullmatch(r"\d+\)", str(k))]
    if len(tails) != 1:
        return c
    rest = {k: v for k, v in c.items() if k != tails[0]}
    return {**rest, "type": f"{c['type']},{tails[0]}"}


def _columns(raw: Any, where: str) -> list[dict[str, Any]]:
    cols = [_rejoin_decimal(_mapping(c, f"{where}.columns")) for c in _listing(raw, f"{where}.columns")]
    if not cols:
        raise _bad(f"{where}.columns: declare at least one column")
    names = set()
    for c in cols:
        _only(c, {"name", "type", "key"}, f"{where}.columns", {"name", "type"})
        name = _ident(c["name"], f"{where}.columns.name")
        if name in names:
            raise _bad(f"{where}.columns: {name} is declared twice")
        names.add(name)
        if not isinstance(c["type"], str) or not _TYPE.fullmatch(c["type"]):
            raise _bad(f"{where}.columns.{name}: unknown type")
        if not isinstance(c.get("key", False), bool):
            raise _bad(f"{where}.columns.{name}: key must be true or false")
    return cols


def _encoding(layout: dict[str, Any], where: str) -> None:
    encoding = layout.get("encoding", "utf-8")
    if not isinstance(encoding, str) or encoding not in UPLOAD_ENCODINGS:
        raise _bad(f"{where}.layout.encoding: use one of {', '.join(sorted(UPLOAD_ENCODINGS))}")


def _csv_layout(layout: dict[str, Any], where: str) -> None:
    _only(layout, {"delimiter", "header", "encoding"}, f"{where}.layout")
    delimiter = layout.get("delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n"':
        raise _bad(f"{where}.layout.delimiter: must be one character")
    if not isinstance(layout.get("header", True), bool):
        raise _bad(f"{where}.layout.header: must be true or false")
    _encoding(layout, where)


def _fixed_layout(layout: dict[str, Any], columns: list[dict[str, Any]], where: str) -> None:
    _only(layout, {"fields", "encoding"}, f"{where}.layout", {"fields"})
    _encoding(layout, where)
    fields = _listing(layout["fields"], f"{where}.layout.fields")
    if not fields:
        raise _bad(f"{where}.layout.fields: declare at least one field")
    spans = []
    names = set()
    for f in fields:
        f = _mapping(f, f"{where}.layout.fields")
        _only(f, {"name", "start", "width"}, f"{where}.layout.fields", {"name", "start", "width"})
        name = _ident(f["name"], f"{where}.layout.fields.name")
        if name in names:
            raise _bad(f"{where}.layout.fields: {name} is declared twice")
        names.add(name)
        spans.append((_int(f["start"], f"{where}.layout.fields.{name}.start"),
                      _int(f["width"], f"{where}.layout.fields.{name}.width", 1)))
    spans.sort()
    if any(a[0] + a[1] > b[0] for a, b in pairwise(spans)):
        raise _bad(f"{where}.layout.fields: fields overlap")
    missing = sorted({c["name"] for c in columns} - names)
    if missing:
        raise _bad(f"{where}.columns: no field for {', '.join(missing)}")


def _source(raw: Any, org_id: str, seen: set[str]) -> dict[str, Any]:
    src = _mapping(raw, "sources[]")
    _only(src, {"name", "upload", "format", "layout", "columns"}, "sources[]",
          {"name", "upload", "format", "columns"})
    name = _ident(src["name"], "sources[].name")
    if name in seen:
        raise _bad(f"sources: {name} is declared twice")
    seen.add(name)
    where = f"sources.{name}"
    columns = _columns(src["columns"], where)
    layout = _mapping(src.get("layout", {}), f"{where}.layout")
    if src["format"] == "csv":
        _csv_layout(layout, where)
    elif src["format"] == "fixed_width":
        _fixed_layout(layout, columns, where)
    else:
        raise _bad(f"{where}.format: use csv or fixed_width")
    return {"name": name, "kind": "upload", "format": src["format"], "layout": layout, "columns": columns,
            "upload": src["upload"], "upload_path": str(_upload_path(org_id, src["upload"], where))}


def _upload_path(org_id: str, ref: Any, where: str) -> Path:
    """One answer for a bad ref, a missing file and another org's file, so a
    spec cannot be used to probe what exists."""
    missing = _bad(f"{where}.upload: no such upload: {ref}")
    try:
        path = upload_file(org_id, ref)
    except UnsafePath as e:
        raise missing from e
    if path.is_symlink() or not path.is_file():
        raise missing
    return path


# ---------------------------------------------------------------- quality

def _check(raw: Any, table: str) -> dict[str, Any]:
    chk = _mapping(raw, f"quality.{table}")
    where = f"quality.{table}"
    kind = chk.get("check")
    if not isinstance(kind, str) or kind not in STRUCTURAL:
        raise _bad(f"{where}: a tenant workspace runs structural checks only: {', '.join(sorted(STRUCTURAL))}")
    allowed = {"check", "name", "owner", "severity", "description"}
    required = {"check"}
    if kind == "row_count_min":
        allowed |= {"min"}
        required |= {"min"}
        _int(chk.get("min"), f"{where}.min")
    else:
        allowed |= {"column"}
        required |= {"column"}
        cols = chk.get("column")
        for c in cols if isinstance(cols, list) and kind == "unique" else [cols]:
            _ident(c, f"{where}.column")
    if kind == "accepted_values":
        allowed |= {"values"}
        required |= {"values"}
        values = _listing(chk.get("values"), f"{where}.values")
        if not values or not all(isinstance(v, (str, int, float)) and _VALUE.fullmatch(str(v)) for v in values):
            raise _bad(f"{where}.values: plain values only, no quotes or backslashes")
    if kind == "type_castable":
        allowed |= {"type"}
        required |= {"type"}
        if not isinstance(chk.get("type"), str) or not _TYPE.fullmatch(chk["type"]):
            raise _bad(f"{where}.type: unknown type")
    _only(chk, allowed, where, required)
    if chk.get("severity", "block") not in ("block", "warn"):
        raise _bad(f"{where}.severity: use block or warn")
    for k in ("name", "owner", "description"):
        if k in chk:
            _text(chk[k], f"{where}.{k}")
    return chk


def _quality(raw: Any, tables: set[str]) -> dict[str, list[dict[str, Any]]]:
    """The contract spells quality as a list and the engine reads a mapping of
    table to checks. Both arrive here and the engine gets the mapping."""
    pairs: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        pairs = [(t, c) for t, checks in raw.items() for c in _listing(checks, f"quality.{t}")]
    else:
        for item in _listing(raw, "quality"):
            item = _mapping(item, "quality[]")
            _only(item, set(item), "quality[]", {"table"})
            pairs.append((item["table"], {k: v for k, v in item.items() if k != "table"}))
    out: dict[str, list[dict[str, Any]]] = {}
    for table, chk in pairs:
        if not isinstance(table, str) or table not in tables:
            raise _bad(f"quality: no table {table}")
        out.setdefault(table, []).append(_check(chk, table))
    return out


def _column_checks(sources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """What the declared columns promise, checked on bronze as warnings. Bronze
    holds the file as received, and cleaning it up is silver's job."""
    out: dict[str, list[dict[str, Any]]] = {}
    base = {"severity": "warn", "owner": "workspace"}
    for src in sources:
        checks = out.setdefault(f"bronze.{src['name']}", [])
        for c in src["columns"]:
            if c.get("key"):
                checks.append({"check": "not_null", "column": c["name"], **base})
                checks.append({"check": "unique", "column": c["name"], **base})
            if c["type"] not in ("string", "varchar"):
                checks.append({"check": "type_castable", "column": c["name"], "type": c["type"], **base})
    return out


# ---------------------------------------------------------------- reconcile

def _reconcile(raw: Any) -> list[dict[str, Any]]:
    for rule in _listing(raw, "reconcile"):
        rule = _mapping(rule, "reconcile[]")
        _only(rule, {"name", "legacy_sql", "lake_sql", "key", "measures", "known_differences"}, "reconcile[]",
              {"name", "legacy_sql", "lake_sql", "key", "measures"})
        where = f"reconcile.{_text(rule['name'], 'reconcile[].name')}"
        _select(rule["legacy_sql"], f"{where}.legacy_sql")
        _select(rule["lake_sql"], f"{where}.lake_sql")
        keys = _listing(rule["key"], f"{where}.key")
        if not keys:
            raise _bad(f"{where}.key: name at least one key column")
        for k in keys:
            _ident(k, f"{where}.key")
        measures = _mapping(rule["measures"], f"{where}.measures")
        if not measures:
            raise _bad(f"{where}.measures: name at least one measure")
        for m, tol in measures.items():
            _ident(m, f"{where}.measures")
            if isinstance(tol, bool) or not isinstance(tol, (int, float)) or tol < 0:
                raise _bad(f"{where}.measures.{m}: the tolerance must be a number of at least 0")
        for kd in _listing(rule.get("known_differences", []), f"{where}.known_differences"):
            kd = _mapping(kd, f"{where}.known_differences")
            _only(kd, {"when", "reason"}, f"{where}.known_differences", {"when", "reason"})
            _text(kd["when"], f"{where}.known_differences.when", 500)
            _text(kd["reason"], f"{where}.known_differences.reason", 500)
    return raw


# ---------------------------------------------------------------- models, ontology, policies

def _model_table(file_name: str) -> str:
    stem = file_name[: -len(".sql")]
    return stem.split("_", 1)[1] if stem[:2].isdigit() and "_" in stem else stem


def _models(models: Any) -> set[str]:
    tables = set()
    for layer, files in _mapping(models or {}, "models").items():
        if layer not in MODEL_LAYERS:
            raise _bad(f"models: use {' or '.join(MODEL_LAYERS)}, not {layer}")
        for name, sql in _mapping(files, f"models.{layer}").items():
            if not isinstance(name, str) or not _MODEL_FILE.fullmatch(name):
                raise _bad(f"models.{layer}: bad file name {name!r}")
            _select(sql, f"models.{layer}.{name}")
            tables.add(f"{layer}.{_ident(_model_table(name), f'models.{layer}.{name}')}")
    return tables


def _ontology(raw: Any) -> dict[str, Any]:
    ont = _mapping(raw if raw is not None else {}, "ontology")
    if not ont:
        return {}
    _only(ont, {"object_types", "link_types"}, "ontology", {"object_types"})
    types = _mapping(ont["object_types"], "ontology.object_types")
    if not types:
        raise _bad("ontology.object_types: declare at least one")
    for tname, tdef in types.items():
        where = f"ontology.{_text(tname, 'ontology.object_types', 64)}"
        tdef = _mapping(tdef, where)
        _only(tdef, {"prefix", "owner", "description", "title", "sensitive", "sources", "resolution", "precedence"},
              where, {"sources"})
        sources = _listing(tdef["sources"], f"{where}.sources")
        if not sources:
            raise _bad(f"{where}.sources: declare at least one")
        for s in sources:
            s = _mapping(s, f"{where}.sources")
            _only(s, {"system", "table", "key", "fields", "where"}, f"{where}.sources",
                  {"system", "table", "key", "fields"})
            _ident(s["system"], f"{where}.sources.system")
            if not isinstance(s["table"], str) or not _TABLE.fullmatch(s["table"]):
                raise _bad(f"{where}.sources.table: must be layer.name")
            _ident(s["key"], f"{where}.sources.key")
            for f, col in _mapping(s["fields"], f"{where}.sources.fields").items():
                _ident(f, f"{where}.sources.fields")
                _ident(col, f"{where}.sources.fields.{f}")
            if "where" in s:
                _select(f"select 1 from t where {_text(s['where'], f'{where}.sources.where', 500)}",
                        f"{where}.sources.where")
    for lname, ldef in _mapping(ont.get("link_types", {}), "ontology.link_types").items():
        where = f"ontology.link_types.{_text(lname, 'ontology.link_types', 64)}"
        ldef = _mapping(ldef, where)
        _only(ldef, {"from", "to", "description", "sql", "multi"}, where, {"from", "to", "sql"})
        for end in ("from", "to"):
            if not isinstance(ldef[end], str) or ldef[end] not in types:
                raise _bad(f"{where}.{end}: no object type {ldef[end]}")
        _select(ldef["sql"], f"{where}.sql")
    return ont


def _policies(raw: Any) -> dict[str, Any]:
    pol = _mapping(raw if raw is not None else {}, "policies")
    _only(pol, {"default", "rules"}, "policies")
    if pol.get("default", "deny") != "deny":
        raise _bad("policies.default: default deny is the only default")
    for r in _listing(pol.get("rules", []), "policies.rules"):
        r = _mapping(r, "policies.rules")
        _only(r, {"id", "effect", "principal", "action", "when"}, "policies.rules", {"effect", "action"})
        if r["effect"] not in ("permit", "forbid"):
            raise _bad("policies.rules: effect must be permit or forbid")
    return pol


# ---------------------------------------------------------------- the whole spec

def _normalize(org_id: str, spec: Any, models: Any) -> dict[str, Any]:
    """Validate a raw workspace.yaml and return the spec the engine reads."""
    spec = _mapping(spec, "workspace.yaml")
    _only(spec, {"kind", "version", "title", "sources", "quality", "reconcile"}, "workspace.yaml",
          {"kind", "version", "title", "sources"})
    if spec["kind"] != "workspace":
        raise _bad("workspace.yaml: kind must be workspace")
    if spec["version"] != 1 or isinstance(spec["version"], bool):
        raise _bad("workspace.yaml: version must be 1")
    title = _text(spec["title"], "workspace.yaml: title")
    raw_sources = _listing(spec["sources"], "sources")
    if not raw_sources:
        raise _bad("sources: declare at least one")
    seen: set[str] = set()
    sources = [_source(s, org_id, seen) for s in raw_sources]
    tables = {f"bronze.{s['name']}" for s in sources} | _models(models)
    quality = _column_checks(sources)
    for table, checks in _quality(spec.get("quality", []), tables).items():
        quality.setdefault(table, []).extend(checks)
    return {"kind": "workspace", "version": 1, "title": title, "sources": sources, "quality": quality,
            "reconcile": _reconcile(spec.get("reconcile", []))}


def _read_small(path: Path, where: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise _bad(f"{where}: cannot be read") from e
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_YAML_BYTES + 1)
    except OSError as e:  # a folder opens fine and only fails on read
        raise _bad(f"{where}: cannot be read") from e
    if len(raw) > MAX_YAML_BYTES:
        raise _bad(f"{where}: larger than {MAX_YAML_BYTES} bytes")
    try:
        return raw.decode()
    except UnicodeDecodeError as e:
        raise _bad(f"{where}: not utf-8") from e


def _read_yaml(path: Path, where: str) -> Any:
    try:
        return yaml.safe_load(_read_small(path, where))
    except yaml.YAMLError as e:
        raise _bad(f"{where}: not valid yaml") from e


def _read_optional_yaml(path: Path, where: str) -> Any:
    try:
        return _read_yaml(path, where)
    except FileNotFoundError:
        return None


def _read_models(d: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for layer in MODEL_LAYERS:
        folder = d / "models" / layer
        if folder.is_symlink():
            raise _bad(f"models.{layer}: is a symlink")
        if folder.is_dir():
            out[layer] = {f.name: _read_small(f, f"models.{layer}.{f.name}") for f in sorted(folder.iterdir())}
    return out


# ---------------------------------------------------------------- CRUD

def _marker_body(org_id: str, slug: str) -> dict[str, str]:
    return {"created_by": "ironwright", "org_id": org_id, "name": slug}


def _created_here(d: Path, org_id: str, slug: str) -> bool:
    marker = d / MARKER
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        return json.loads(marker.read_text()) == _marker_body(org_id, slug)
    except (json.JSONDecodeError, OSError):
        return False


def _existing(org_id: str, slug: str) -> Path:
    """The folder of a workspace this org made, or FileNotFoundError. Every
    other answer (bad id, bad slug, foreign folder, another org) looks the same."""
    try:
        d = workspace_dir(org_id, slug)
    except UnsafePath as e:
        raise FileNotFoundError(slug) from e
    if not _created_here(d, org_id, slug):
        raise FileNotFoundError(slug)
    return d


def create_workspace(org_id: str, slug: str, spec: dict[str, Any], *,
                     models: dict[str, dict[str, str]] | None = None,
                     ontology: dict[str, Any] | None = None,
                     policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate everything first; nothing touches the disk on a bad spec."""
    try:
        d = workspace_dir(org_id, slug)
    except UnsafePath as e:
        raise _bad(f"bad workspace name: {slug!r}") from e
    _normalize(org_id, spec, models)
    ontology, policies = _ontology(ontology), _policies(policies)
    ensure_tenant(org_id)
    try:
        d.mkdir(mode=0o700)  # atomic: two creators, one winner, and no adopting a folder
    except FileExistsError as e:
        raise _bad(f"workspace {slug} already exists") from e
    write_private(d / MARKER, json.dumps(_marker_body(org_id, slug)))
    write_private(d / "workspace.yaml", yaml.safe_dump(spec, sort_keys=False))
    if ontology:
        write_private(d / "ontology.yaml", yaml.safe_dump(ontology, sort_keys=False))
    if policies:
        write_private(d / "policies.yaml", yaml.safe_dump(policies, sort_keys=False))
    for layer, files in (models or {}).items():
        folder = private_dir(d / "models" / layer)
        for name, sql in files.items():
            write_private(folder / name, sql)
    return read_workspace(org_id, slug)


def load_workspace(org_id: str, slug: str) -> Pack:
    """The same Pack the engine runs for a shipped pack. There is no generator
    to call, and the bytes come from the org's uploads."""
    d = _existing(org_id, slug)
    spec = _normalize(org_id, _read_yaml(d / "workspace.yaml", "workspace.yaml"), _read_models(d))
    return Pack(name=slug, dir=d, spec=spec,
                ontology=_ontology(_read_optional_yaml(d / "ontology.yaml", "ontology.yaml")),
                policies=_policies(_read_optional_yaml(d / "policies.yaml", "policies.yaml")),
                usecases={}, run_root=d / "run")


def read_workspace(org_id: str, slug: str) -> dict[str, Any]:
    """What a reader sees: the spec as written, which never names a server path."""
    d = _existing(org_id, slug)
    spec = _read_yaml(d / "workspace.yaml", "workspace.yaml")
    return {"slug": slug, "title": spec.get("title") if isinstance(spec, dict) else None, "spec": spec}


def list_workspaces(org_id: str) -> list[dict[str, Any]]:
    try:
        root = workspaces_root(org_id)
    except UnsafePath:
        return []
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not _created_here(d, org_id, d.name):
            continue
        try:
            got = read_workspace(org_id, d.name)
        except (WorkspaceError, FileNotFoundError):
            continue
        out.append({"slug": got["slug"], "title": got["title"]})
    return out


def delete_workspace(org_id: str, slug: str) -> None:
    shutil.rmtree(_existing(org_id, slug))


# ---------------------------------------------------------------- run

def _structure_layer(pack: Pack) -> dict[str, Any]:
    """Green has to mean the tables landed. The demo pipeline eval only asks
    whether a blocked write leaked, which an empty run would also pass."""
    writes = {w["table"]: w for w in _report(pack, "migrate").get("writes", [])}
    expected = [f"bronze.{s['name']}" for s in pack.spec["sources"]]
    for layer in MODEL_LAYERS:
        expected += [f"{layer}.{name}" for name, _ in pack.models(layer)]
    problems = []
    for table in expected:
        w = writes.get(table)
        if not w or not w["published"]:
            problems.append(f"{table} was not published")
        elif not w["rows"]:
            problems.append(f"{table} has no rows")
    onto = _report(pack, "ontology")
    for tname in pack.ontology.get("object_types", {}):
        if not onto.get("object_types", {}).get(tname, {}).get("objects"):
            problems.append(f"{tname} has no objects")
    orphans = sum(v["orphans"] for v in onto.get("links", {}).values())
    return {"layer": "structure", "passed": not problems,
            "headline": "; ".join(problems) if problems else
                        f"{len(expected)} tables published with rows, "
                        f"{len(pack.ontology.get('object_types', {}))} object types, {orphans} orphan link rows",
            "metrics": {"tables": len(expected), "problems": problems, "orphan_link_rows": orphans}}


def _report(pack: Pack, name: str) -> dict[str, Any]:
    p = pack.paths.reports / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def run_workspace(org_id: str, slug: str) -> dict[str, Any]:
    """bronze, silver, gold, reconcile, ontology, checks, export. There is no
    generate stage and no agents stage. It never wipes run/: bronze keeps every
    run, as it does for a pack, and the raw bytes live in uploads/."""
    from .cli import STAGES, _save
    from .evals import eval_migration, eval_pipeline
    from .export import export_pack
    from .tracing import Tracer

    pack = load_workspace(org_id, slug)
    tracer = Tracer(pack.paths.ensure().traces)
    args = argparse.Namespace(seed=None, limit=None, web_dir=None)
    STAGES["migrate"](pack, tracer, args)
    STAGES["reconcile"](pack, tracer, args)
    if pack.ontology:
        STAGES["ontology"](pack, tracer, args)
    layers = [eval_pipeline(pack), eval_migration(pack), _structure_layer(pack)]
    report = {"pack": pack.name, "passed": all(la["passed"] for la in layers), "layers": layers,
              "planted": []}
    _save(pack, "evals", report)
    if pack.ontology:
        # Never the default out_dir: that one folder is shared across orgs.
        export_pack(pack, pack.paths.reports)
    return report
