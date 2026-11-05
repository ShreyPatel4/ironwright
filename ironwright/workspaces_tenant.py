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

A source has versions. workspace.yaml stays contract 3 and names the latest upload.
Older versions live in source_versions/<source>/<NNNN>.json, each with the runs that
loaded it, so a version maps to its bronze rows through _run_id.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import yaml
from pyiceberg.exceptions import NoSuchTableError

from .config import (
    UnsafePath,
    ensure_tenant,
    private_dir,
    upload_file,
    workspace_dir,
    workspaces_root,
    write_private,
)
from .ingest import drift
from .legacy import UPLOAD_ENCODINGS, read_source
from .packs import Pack
from .quality import STRUCTURAL
from .sqlguard import SqlRejected, guard_select

MARKER = ".ironwright-tenant-workspace"
MAX_YAML_BYTES = 1024 * 1024
MODEL_LAYERS = ("silver", "gold")
VERSIONS_DIR = "source_versions"
# How many records to skip around the data, per source. Contract 3 has no field for it,
# so workspace.yaml stays as the contract says and this sits beside it.
RECORDS_FILE = "records.yaml"
MAX_SKIP = 16
RUN_MODES = ("append", "full")
_VERSION_FILE = re.compile(r"\d{4,}\.json")

_IDENT = re.compile(r"[a-z][a-z0-9_]{0,62}")
_TABLE = re.compile(r"(bronze|silver|gold)\.[a-z][a-z0-9_]{0,62}")
_MODEL_FILE = re.compile(r"[a-z0-9][a-z0-9_]{0,62}\.sql")
_TYPE = re.compile(r"string|varchar|integer|int|bigint|double|float|boolean|date|timestamp|decimal\(\d{1,2},\s*\d{1,2}\)")
_VALUE = re.compile(r"[^'\\\x00]{0,200}")


class WorkspaceError(ValueError):
    """The spec is wrong. The message is ours and is safe to show."""


class NoSuchSource(WorkspaceError):
    """The workspace has no source by that name."""


class DriftBlocked(WorkspaceError):
    """The latest version cannot run until the spec or the file changes."""


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


def _records(raw: Any, sources: set[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for source, skips in _mapping(raw if raw is not None else {}, "records").items():
        if source not in sources:
            raise _bad(f"records: no source {source}")
        where = f"records.{source}"
        skips = _mapping(skips, where)
        _only(skips, {"skip_header", "skip_footer"}, where)
        for k, n in skips.items():
            if _int(n, f"{where}.{k}") > MAX_SKIP:
                raise _bad(f"{where}.{k}: at most {MAX_SKIP}")
        out[source] = {"skip_header": skips.get("skip_header", 0), "skip_footer": skips.get("skip_footer", 0)}
    return out


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
                     policies: dict[str, Any] | None = None,
                     records: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate everything first; nothing touches the disk on a bad spec."""
    try:
        d = workspace_dir(org_id, slug)
    except UnsafePath as e:
        raise _bad(f"bad workspace name: {slug!r}") from e
    normal = _normalize(org_id, spec, models)
    ontology, policies = _ontology(ontology), _policies(policies)
    records = _records(records, {s["name"] for s in normal["sources"]})
    ensure_tenant(org_id)
    try:
        d.mkdir(mode=0o700)  # atomic: two creators, one winner, and no adopting a folder
    except FileExistsError as e:
        raise _bad(f"workspace {slug} already exists") from e
    write_private(d / MARKER, json.dumps(_marker_body(org_id, slug)))
    write_private(d / "workspace.yaml", yaml.safe_dump(spec, sort_keys=False))
    if records:
        write_private(d / RECORDS_FILE, yaml.safe_dump(records, sort_keys=True))
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
    records = _records(_read_optional_yaml(d / RECORDS_FILE, RECORDS_FILE), {s["name"] for s in spec["sources"]})
    for src in spec["sources"]:
        src["records"] = records.get(src["name"], {})
    return Pack(name=slug, dir=d, spec=spec,
                ontology=_ontology(_read_optional_yaml(d / "ontology.yaml", "ontology.yaml")),
                policies=_policies(_read_optional_yaml(d / "policies.yaml", "policies.yaml")),
                usecases={}, run_root=d / "run")


def read_workspace(org_id: str, slug: str) -> dict[str, Any]:
    """What a reader sees: the spec as written, which never names a server path."""
    d = _existing(org_id, slug)
    spec = _read_yaml(d / "workspace.yaml", "workspace.yaml")
    return {"slug": slug, "title": spec.get("title") if isinstance(spec, dict) else None, "spec": spec,
            "records": _read_optional_yaml(d / RECORDS_FILE, RECORDS_FILE) or {}}


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


# ---------------------------------------------------------------- source versions

@contextlib.contextmanager
def _versions_lock(d: Path) -> Iterator[None]:
    """One writer at a time: a number and the yaml pointer change together. The exclusive
    create in _claim is the backstop if a shared volume does not honor flock."""
    fd = os.open(d / ".source_versions.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _replace(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    write_private(tmp, text)
    os.replace(tmp, path)


def _version_path(d: Path, source: str, version: int) -> Path:
    return d / VERSIONS_DIR / source / f"{version:04d}.json"


def _read_versions(d: Path, source: str) -> list[dict[str, Any]]:
    folder = d / VERSIONS_DIR / source
    if not folder.is_dir():
        return []
    found = [f for f in folder.iterdir() if _VERSION_FILE.fullmatch(f.name)]
    records = [json.loads(_read_small(f, f"{VERSIONS_DIR}/{source}")) for f in found]
    return sorted(records, key=lambda r: r["version"])


def _claim(d: Path, source: str, record: dict[str, Any]) -> None:
    private_dir(d / VERSIONS_DIR / source)
    fd = os.open(_version_path(d, source, record["version"]), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(record, fh, indent=1, sort_keys=True)


def _save_version(d: Path, source: str, record: dict[str, Any]) -> None:
    _replace(_version_path(d, source, record["version"]), json.dumps(record, indent=1, sort_keys=True))


def _raw_source(spec: dict[str, Any], source: str) -> dict[str, Any]:
    for src in spec.get("sources") or []:
        if src.get("name") == source:
            return src
    raise NoSuchSource(f"this workspace has no source {source}")


def _source_shape(d: Path, raw: dict[str, Any]) -> dict[str, Any]:
    records = (_read_optional_yaml(d / RECORDS_FILE, RECORDS_FILE) or {}).get(raw["name"], {})
    return {"format": raw["format"], "layout": raw.get("layout", {}), "columns": raw["columns"], "records": records}


def _readable(org_id: str, source: str, record: dict[str, Any]) -> dict[str, Any]:
    """A version as read_upload and drift want it: its own shape, its own bytes."""
    shape = record["source"]
    where = f"sources.{source}"
    return {"name": source, "kind": "upload", "format": shape["format"], "layout": shape["layout"],
            "columns": _columns(shape["columns"], where), "records": shape.get("records", {}),
            "upload_path": str(_upload_path(org_id, record["upload"], where))}


def _file_facts(org_id: str, ref: str, where: str) -> dict[str, Any]:
    raw = _upload_path(org_id, ref, where).read_bytes()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _ensure_first(org_id: str, d: Path, pack_root: Path, raw: dict[str, Any]) -> None:
    """The upload a workspace was made with is version 1. Nothing wrote a record when
    it was created, so the first caller does. A workspace that already ran before
    versions existed keeps that run on v1."""
    source = raw["name"]
    if _read_versions(d, source):
        return
    record = {"version": 1, "upload": raw["upload"], "created_at": round(time.time(), 3), "created_by": "",
              "source": _source_shape(d, raw), "runs": [], "drift": None,
              **_file_facts(org_id, raw["upload"], f"sources.{source}")}
    try:
        report = json.loads((pack_root / "reports" / "migrate.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        report = {}
    for w in report.get("writes", []):
        if w["table"] == f"bronze.{source}" and w["published"]:
            record["runs"] = [{"run_id": report["run_id"], "rows": w["rows"]}]
    _claim(d, source, record)


def _spec_of(d: Path) -> dict[str, Any]:
    return _mapping(_read_yaml(d / "workspace.yaml", "workspace.yaml"), "workspace.yaml")


def _ensure_versions(org_id: str, d: Path) -> None:
    """Validates first. These files can change on disk, and a source name becomes a path."""
    spec = _spec_of(d)
    _normalize(org_id, spec, _read_models(d))
    with _versions_lock(d):
        for raw in spec.get("sources") or []:
            _ensure_first(org_id, d, d / "run", raw)


def _impact_inputs(d: Path) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    ontology = _read_optional_yaml(d / "ontology.yaml", "ontology.yaml") or {}
    models = _read_models(d)
    return ontology, [(layer, _model_table(name), sql) for layer in MODEL_LAYERS
                      for name, sql in sorted(models.get(layer, {}).items())]


def _baseline(records: list[dict[str, Any]], version: int) -> dict[str, Any] | None:
    """What this version is judged against: the last one that loaded, else the one
    before. A blocked version in between never became the accepted shape."""
    earlier = [r for r in records if r["version"] < version]
    return next((r for r in reversed(earlier) if r["runs"]), earlier[-1] if earlier else None)


def _assess(org_id: str, d: Path, source: str, shape: dict[str, Any], upload: str, version: int,
            records: list[dict[str, Any]]) -> dict[str, Any]:
    """Drift for one file against the spec as it stands now, so a spec update can unblock."""
    where = f"sources.{source}"
    src = {"name": source, "kind": "upload", "format": shape["format"], "layout": shape["layout"],
           "records": shape.get("records", {}),
           "columns": _columns(shape["columns"], where), "upload_path": str(_upload_path(org_id, upload, where))}
    base_record = _baseline(records, version)
    base = None
    if base_record is not None:
        try:
            base = drift.observe(_readable(org_id, source, base_record))
        except WorkspaceError:
            base = None  # the older bytes are gone; only the spec is left to judge by
        if base is not None and base.error:
            base = None
    report = drift.diff(src, drift.observe(src), base, version=version,
                        against=base_record["version"] if base_record else None,
                        loaded_before=any(r["runs"] for r in records))
    ontology, models = _impact_inputs(d)
    return drift.annotate(report, ontology, models)


def source_versions(org_id: str, slug: str, source: str) -> list[dict[str, Any]]:
    """Every version, oldest first. FileNotFoundError when this org has no such workspace."""
    d = _existing(org_id, slug)
    _raw_source(_spec_of(d), source)
    _ensure_versions(org_id, d)
    return _read_versions(d, source)


def add_source_version(org_id: str, slug: str, source: str, upload: str, created_by: str = "") -> dict[str, Any]:
    """The next extract. It becomes version N+1 and workspace.yaml points at it, whatever
    the drift says: the run is what refuses to load blocking drift. Sending the same
    upload again answers with the version it already is, so a retry adds nothing."""
    d = _existing(org_id, slug)
    where = f"sources.{source}"
    spec = _spec_of(d)
    _raw_source(spec, source)
    facts = _file_facts(org_id, upload, where)
    _ensure_versions(org_id, d)
    with _versions_lock(d):
        records = _read_versions(d, source)
        if records[-1]["upload"] == upload:
            return records[-1]
        spec = _spec_of(d)
        raw = _raw_source(spec, source)
        version = records[-1]["version"] + 1
        shape = _source_shape(d, raw)
        record = {"version": version, "upload": upload, "created_at": round(time.time(), 3), "created_by": created_by,
                  "source": shape, "runs": [], **facts,
                  "drift": _assess(org_id, d, source, shape, upload, version, records)}
        _claim(d, source, record)
        raw["upload"] = upload
        _replace(d / "workspace.yaml", yaml.safe_dump(spec, sort_keys=False))
    return record


def update_source_spec(org_id: str, slug: str, source: str, *, columns: Any = None, layout: Any = None) -> dict[str, Any]:
    """The answer to blocking drift. The whole spec validates again before anything is
    written, and the drift is judged again against the new spec."""
    d = _existing(org_id, slug)
    _ensure_versions(org_id, d)
    with _versions_lock(d):
        spec = _spec_of(d)
        raw = _raw_source(spec, source)
        if columns is not None:
            raw["columns"] = columns
        if layout is not None:
            raw["layout"] = layout
        _normalize(org_id, spec, _read_models(d))
        records = _read_versions(d, source)
        latest = records[-1]
        latest["source"] = _source_shape(d, raw)
        latest["drift"] = _assess(org_id, d, source, latest["source"], latest["upload"], latest["version"], records)
        _replace(d / "workspace.yaml", yaml.safe_dump(spec, sort_keys=False))
        _save_version(d, source, latest)
    return latest


def drift_overview(org_id: str, slug: str) -> dict[str, Any]:
    """The latest report per source, and every item that touches the accepted ontology.
    Impact is worked out on read, because an ontology can be accepted after the upload."""
    d = _existing(org_id, slug)
    _ensure_versions(org_id, d)
    ontology, models = _impact_inputs(d)
    sources, impact = {}, []
    for raw in _spec_of(d).get("sources") or []:
        latest = _read_versions(d, raw["name"])[-1]
        report = latest["drift"]
        if report is None:
            report = _assess(org_id, d, raw["name"], latest["source"], latest["upload"], latest["version"],
                             _read_versions(d, raw["name"]))
        report = drift.annotate(report, ontology, models)
        sources[raw["name"]] = report
        impact += [{"source": raw["name"], "version": latest["version"], "kind": i["kind"], "column": i["column"],
                    "severity": i["severity"], "ontology": i["ontology"]} for i in report["items"] if i["ontology"]]
    return {"sources": sources, "ontology_impact": impact}


def read_source_version(org_id: str, slug: str, source: str, version: int) -> pa.Table:
    """The bronze rows of one version, from its last run."""
    from .lake import Lakehouse

    records = source_versions(org_id, slug, source)
    record = next((r for r in records if r["version"] == version), None)
    if record is None or not record["runs"]:
        raise _bad(f"version {version} of {source} was never loaded")
    data = Lakehouse(load_workspace(org_id, slug)).read(f"bronze.{source}")
    run_id = record["runs"][-1]["run_id"]
    return data.filter(pc.equal(data.column("_run_id"), run_id))


def _loaded_version(records: list[dict[str, Any]], src: dict[str, Any]) -> dict[str, Any]:
    """The version this pack holds. Newest is not the same thing: the next extract can
    land while a run is going, and it must not be credited with that run."""
    return next((r for r in reversed(records) if r["upload"] == src["upload"]), records[-1])


def _gate_drift(org_id: str, d: Path, pack: Pack) -> None:
    """Judge every latest version against the spec as it stands now, then load what is
    left of a warned file. read_upload refuses a declared column the file does not have,
    and a warning has to mean the run goes on."""
    blocked = []
    for src in pack.spec["sources"]:
        source = src["name"]
        # The spec, not the record: a record keeps the narrower shape its last run loaded under.
        shape = {"format": src["format"], "layout": src["layout"], "columns": src["columns"], "records": src["records"]}
        with _versions_lock(d):
            records = _read_versions(d, source)
            latest = _loaded_version(records, src)
            latest["drift"] = _assess(org_id, d, source, shape, latest["upload"], latest["version"], records)
            _save_version(d, source, latest)
        if latest["drift"]["blocking"]:
            blocked.append(drift.blocking_message(latest["drift"]))
            continue
        gone = {i["column"] for i in latest["drift"]["items"] if i["kind"] == "missing_column"}
        src["columns"] = [c for c in src["columns"] if c["name"] not in gone]
    if blocked:
        raise DriftBlocked(" ".join(blocked))


def _next_run_id(d: Path, pack: Pack) -> str:
    """Later than any run before it, as text, because bronze_x picks max(_run_id).
    Two runs in one second would otherwise share an id and read as one."""
    stamp = time.strftime("%Y%m%d%H%M%S")
    seen = [_report(pack, "migrate").get("run_id", "")]
    for src in pack.spec["sources"]:
        seen += [run["run_id"] for r in _read_versions(d, src["name"]) for run in r["runs"]]
    last = max(seen)
    return stamp if stamp > last else f"{last}a"


def _rebuild_bronze(org_id: str, d: Path, pack: Pack, tracer: Any, base_id: str) -> None:
    """Full rebuild without losing history: bronze restarts, then every version that
    loaded before goes back in order from its kept bytes, read with the shape it loaded
    under. Everything is read first, so a version that cannot be read leaves bronze as it
    was. Checks already ran on these rows, and the new spec may not fit an old file.
    The catalog does the drop because a hosted lake does not live under run/."""
    from .lake import Lakehouse

    replays: list[tuple[dict[str, Any], dict[str, Any], pa.Table]] = []
    for src in pack.spec["sources"]:
        records = _read_versions(d, src["name"])
        current = _loaded_version(records, src)["version"]
        for record in records:
            if record["version"] < current and record["runs"]:
                readable = _readable(org_id, src["name"], record)
                replays.append((src, record, read_source(readable, pack.paths.legacy)))
    lake = Lakehouse(pack)
    for src in pack.spec["sources"]:
        with contextlib.suppress(NoSuchTableError):
            lake.catalog.drop_table(f"bronze.{src['name']}")
        with _versions_lock(d):
            latest = _loaded_version(_read_versions(d, src["name"]), src)
            latest["runs"] = []
            _save_version(d, src["name"], latest)
    for k, (src, record, data) in enumerate(replays, 1):
        run_id = f"{base_id}r{k:03d}"
        data = data.append_column("_run_id", pa.array([run_id] * data.num_rows, pa.string()))
        data = data.append_column("_source_system", pa.array([src.get("system", src["name"])] * data.num_rows, pa.string()))
        lake.write_wap(f"bronze.{src['name']}", data, [], run_id, "append", tracer)
        with _versions_lock(d):
            record = next(r for r in _read_versions(d, src["name"]) if r["version"] == record["version"])
            record["runs"] = [{"run_id": run_id, "rows": data.num_rows}]
            _save_version(d, src["name"], record)


def _record_runs(d: Path, pack: Pack, report: dict[str, Any]) -> None:
    writes = {w["table"]: w for w in report["writes"]}
    for src in pack.spec["sources"]:
        w = writes.get(f"bronze.{src['name']}")
        if not w or not w["published"]:
            continue
        with _versions_lock(d):
            latest = _loaded_version(_read_versions(d, src["name"]), src)
            latest["runs"].append({"run_id": report["run_id"], "rows": w["rows"]})
            latest["source"] = {"format": src["format"], "layout": src["layout"], "columns": src["columns"],
                                "records": src["records"]}
            _save_version(d, src["name"], latest)


def _version_layer(org_id: str, d: Path, pack: Pack) -> dict[str, Any] | None:
    """The new version against the last one, for sources that have both. It fails only
    on what a refresh promises: every run still holds its rows, and the old bytes are
    still here. A real change between extracts is the point of a refresh, so it is
    reported, never failed. None when no source has a version to compare."""
    from .cli import _save
    from .lake import Lakehouse

    con = None
    sources, problems = {}, []
    for src in pack.spec["sources"]:
        name = src["name"]
        records = _read_versions(d, name)
        latest = _loaded_version(records, src)
        records = [r for r in records if r["version"] <= latest["version"]]
        last = next((r for r in reversed(records[:-1]) if r["runs"]), None)
        if last is None or not latest["runs"]:
            continue
        con = con or Lakehouse(pack).connect()
        held = dict(con.execute(f'select _run_id, count(*) from "bronze_{name}_all" group by 1').fetchall())
        for r in records:
            problems += [f"{name} v{r['version']}: run {run['run_id']} holds {held.get(run['run_id'], 0)} rows, expected {run['rows']}"
                         for run in r["runs"] if held.get(run["run_id"], 0) != run["rows"]]
        try:
            old, new = (drift.observe(_readable(org_id, name, r)) for r in (last, latest))
        except WorkspaceError:
            problems.append(f"{name}: the bytes of an earlier version are gone")
            continue
        sources[name] = {"from_version": last["version"], "to_version": latest["version"],
                         **drift.compare(_readable(org_id, name, latest), old, new)}
    if not sources and not problems:
        return None
    _save(pack, "version_reconcile", sources)
    return {"layer": "version_reconcile", "passed": not problems,
            "headline": "; ".join(problems) if problems else f"{len(sources)} sources compared to their last version",
            "metrics": {"sources": sources, "problems": problems}}


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


def run_workspace(org_id: str, slug: str, mode: str = "append") -> dict[str, Any]:
    """bronze, silver, gold, reconcile, ontology, checks, export. There is no
    generate stage and no agents stage. It never wipes run/: bronze keeps every
    run, as it does for a pack, and the raw bytes live in uploads/.

    append adds this version to bronze as a new run. full restarts bronze and loads
    every version again from its kept bytes. Blocking drift stops either one before
    anything is written."""
    from .cli import STAGES, _save
    from .evals import eval_migration, eval_pipeline
    from .export import export_pack
    from .lake import migrate
    from .tracing import Tracer

    if mode not in RUN_MODES:
        raise _bad(f"mode must be one of {', '.join(RUN_MODES)}")
    d = _existing(org_id, slug)
    _ensure_versions(org_id, d)
    pack = load_workspace(org_id, slug)
    _gate_drift(org_id, d, pack)
    tracer = Tracer(pack.paths.ensure().traces)
    args = argparse.Namespace(seed=None, limit=None, web_dir=None)
    run_id = _next_run_id(d, pack)
    if mode == "full":
        _rebuild_bronze(org_id, d, pack, tracer, run_id)
        run_id = f"{run_id}z"
    migrated = migrate(pack, tracer, run_id)
    _save(pack, "migrate", migrated)
    _record_runs(d, pack, migrated)
    STAGES["reconcile"](pack, tracer, args)
    if pack.ontology:
        STAGES["ontology"](pack, tracer, args)
    layers = [eval_pipeline(pack), eval_migration(pack), _structure_layer(pack)]
    versions = _version_layer(org_id, d, pack)
    if versions:
        layers.append(versions)
    report = {"pack": pack.name, "passed": all(la["passed"] for la in layers), "layers": layers,
              "planted": []}
    _save(pack, "evals", report)
    if pack.ontology:
        # Never the default out_dir: that one folder is shared across orgs.
        export_pack(pack, pack.paths.reports)
    return report
