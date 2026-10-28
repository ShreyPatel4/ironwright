"""The platform spine: one app over every workspace.

Where api.create_app binds one pack for the pack-scoped REST surface, this
app is what the console talks to: list workspaces with their derived
verification level, run pipeline stages, read and decide pending approvals,
and verify the audit chain. It opens each workspace's store per request and
closes it after, so workspaces swap freely; a local single-user console
needs no pool.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import time
from typing import Any, Iterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CONSOLE_DIST = pathlib.Path(__file__).resolve().parents[1] / "console" / "dist"

import re

from . import ask as ask_mod
from . import auth as auth_mod
from . import boards as boards_mod
from . import escalation as escalation_mod
from . import orgpolicy as orgpolicy_mod
from .asof import router as asof_router
from .otel_export import router as otel_router
from . import discovery as discovery_mod
from . import verification
from . import workspaces as org_mod
from .scenario.spec import SpecError
from .cli import STAGES
from .lake import Lakehouse
from .ontology.runtime import Ontology
from .packs import list_packs, load_pack
from .tracing import Tracer

CONSOLE_PRINCIPAL = "user:console"
_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

STAGE_ORDER = ["generate", "migrate", "reconcile", "ontology", "agents", "evals", "export"]
DEFAULT_SEEDS = {"retail": 7, "supply_chain": 11, "banking_aml": 23, "insurance_claims": 31}


class RunBody(BaseModel):
    seed: int | None = None
    stages: list[str] | None = None


class ActionBody(BaseModel):
    params: dict[str, Any] = {}
    actor: str = "user:console"
    on_behalf_of: str | None = None
    idempotency_key: str | None = None


class DecideBody(BaseModel):
    approved_by: str
    decision: str = "approve"  # approve | deny


class QueryBody(BaseModel):
    sql: str
    limit: int = 200


class AskBody(BaseModel):
    question: str


class ReplayBody(BaseModel):
    index: int


class PolicyCheckBody(BaseModel):
    principal: str
    action: str  # e.g. "action:delist_item" or "function:vendor_sell_through"
    env: dict[str, Any] = {}


class BoardBody(BaseModel):
    title: str
    tiles: list[dict[str, Any]]


class LoginBody(BaseModel):
    email: str
    password: str


class CreateOrgBody(BaseModel):
    name: str
    archetype: str = "regional_retail_chain"
    size: int = 16
    seed: int = 7


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _guard_select_only(sql: str) -> str:
    """The explorer is read-only by contract. One statement, SELECT or WITH,
    nothing after a terminating semicolon. This shape check is discipline;
    the defense is the connection itself, which disables external access
    (read_text, glob, httpfs, ATTACH) and locks its configuration before any
    user SQL runs."""
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        raise HTTPException(422, "one statement at a time")
    if not _SELECT_ONLY.match(body):
        raise HTTPException(422, "read-only explorer: the statement must start with SELECT or WITH")
    return body


def _pack(name: str):
    if name not in list_packs():
        raise HTTPException(404, f"no workspace named {name}")
    return load_pack(name)


@contextlib.contextmanager
def _ontology(pack) -> Iterator[Ontology]:
    ont = Ontology(pack, Tracer(pack.paths.ensure().traces))
    # Serving surfaces authorize against the live combined engine: hand-written
    # rules plus applied generated policy and unexpired escalation grants. The
    # batch pipeline and evals keep pack.policies alone, so planted-problem
    # ground truth stays reproducible.
    with contextlib.suppress(Exception):
        ont.policy = orgpolicy_mod.live_engine(pack.name)
    try:
        yield ont
    finally:
        with contextlib.suppress(Exception):
            ont.store.con.close()


def _pending_approvals(ont: Ontology) -> list[dict[str, Any]]:
    """needs_approval rows with no LATER applied or denied row for the same
    action and params. Seq order matters: an earlier settlement must not
    swallow a re-request of the same action, or the new request can never be
    decided. The audit log is the only source of truth here."""
    rows = ont.store.con.execute(
        "select seq, actor, action, status, params from audit_log order by seq"
    ).fetchall()
    settled: dict[tuple[str, str], int] = {}
    pending: dict[tuple[str, str], dict[str, Any]] = {}
    for seq, actor, action, status, params in rows:
        key = (action, params or "null")
        if status == "needs_approval":
            pending[key] = {"seq": seq, "actor": actor, "action": action,
                            "params": json.loads(params) if params else {}}
        elif status in ("applied", "applied_pending_sync", "denied"):
            settled[key] = seq
    return [p for k, p in pending.items() if settled.get(k, -1) < p["seq"]]


def create_platform_app() -> FastAPI:
    app = FastAPI(title="ironwright platform",
                  description="One spine over every workspace: runs, approvals, audit, verification.")

    @app.get("/workspaces")
    def workspaces() -> list[dict[str, Any]]:
        out = []
        for name in list_packs():
            pack = load_pack(name)
            out.append({"name": name, "kind": "pack", "title": pack.title,
                        "decision": pack.spec.get("decision"),
                        "verification": verification.derive(pack)})
        for org in org_mod.list_organizations():
            out.append({"name": org["name"], "kind": "organization",
                        "title": org["company"],
                        "decision": f"{org['stores']} stores, {org['roles']} roles · "
                                    f"{org['archetype']} · seed {org['seed']}",
                        "verification": verification.derive_organization(None)})
        return out

    @app.post("/workspaces", status_code=201)
    def create_workspace(body: CreateOrgBody) -> dict[str, Any]:
        try:
            org = org_mod.create_organization(body.name, body.archetype, body.size, body.seed)
        except SpecError as e:
            raise HTTPException(422, str(e))
        return {"name": body.name, "kind": "organization", "org": org,
                "verification": verification.derive_organization(None)}

    @app.delete("/workspaces/{name}")
    def delete_workspace(name: str) -> dict[str, Any]:
        if name in list_packs():
            raise HTTPException(403, "packs ship with the platform and cannot be deleted")
        try:
            org_mod.delete_organization(name)
        except FileNotFoundError:
            raise HTTPException(404, f"no organization named {name}")
        return {"deleted": name}

    @app.get("/workspaces/{name}/organization")
    def organization(name: str) -> dict[str, Any]:
        try:
            return org_mod.read_org(name)
        except FileNotFoundError:
            raise HTTPException(404, f"{name} has no generated organization; packs ship "
                                     "their org inside the pack story")

    @app.get("/archetypes")
    def archetypes() -> list[dict[str, Any]]:
        return org_mod.archetypes()

    @app.get("/workspaces/{name}/discovery")
    def discovery(name: str) -> dict[str, Any]:
        if name not in list_packs():
            # Sketch 01: an empty class stays empty. A created org has no
            # migration artifacts to discover against, and this endpoint says
            # so instead of inventing findings or answering 404.
            try:
                org_mod.read_org(name)
            except FileNotFoundError:
                raise HTTPException(404, f"no workspace named {name}")
            return {"findings": [], "counts": {},
                    "note": "A created organization has no migration artifacts yet; "
                            "discovery reads merge logs, reconcile reports and checks, "
                            "none of which exist before a migration runs. Nothing is invented."}
        return discovery_mod.derive(_pack(name))

    @app.get("/workspaces/{name}/boards")
    def boards_list(name: str) -> list[dict[str, Any]]:
        return boards_mod.list_boards(_pack(name))

    @app.get("/workspaces/{name}/boards/{slug}")
    def board_get(name: str, slug: str) -> dict[str, Any]:
        try:
            return boards_mod.read(_pack(name), slug)
        except FileNotFoundError:
            raise HTTPException(404, f"no board {slug}")

    @app.put("/workspaces/{name}/boards/{slug}")
    def board_put(name: str, slug: str, body: BoardBody) -> dict[str, Any]:
        try:
            return boards_mod.write(_pack(name), slug, body.model_dump())
        except boards_mod.BoardError as e:
            raise HTTPException(422, str(e))

    @app.delete("/workspaces/{name}/boards/{slug}")
    def board_delete(name: str, slug: str) -> dict[str, Any]:
        try:
            boards_mod.delete(_pack(name), slug)
        except FileNotFoundError:
            raise HTTPException(404, f"no board {slug}")
        return {"deleted": slug}

    @app.post("/workspaces/{name}/boards/{slug}/run")
    def board_run(name: str, slug: str) -> dict[str, Any]:
        try:
            return boards_mod.run(_pack(name), slug)
        except FileNotFoundError:
            raise HTTPException(404, f"no board {slug}")

    @app.get("/auth/status")
    def auth_status() -> dict[str, Any]:
        return {"seeded": auth_mod.seeded(),
                "note": "Invite gate on the console door; the API stays locally open until "
                        "the hosted build. Seed accounts with: ironwright auth-set <email>"}

    @app.post("/auth/login")
    def auth_login(body: LoginBody) -> dict[str, Any]:
        token = auth_mod.login(body.email, body.password)
        if not token:
            raise HTTPException(401, "that email and password do not match an invited account")
        return {"token": token, **(auth_mod.me(token) or {})}

    @app.get("/auth/me")
    def auth_me(x_session: str = Header("")) -> dict[str, Any]:
        info = auth_mod.me(x_session)
        if not info:
            raise HTTPException(401, "no session")
        return info

    @app.post("/auth/logout")
    def auth_logout(x_session: str = Header("")) -> dict[str, Any]:
        auth_mod.logout(x_session)
        return {"ok": True}

    @app.get("/platform/map")
    def get_platform_map() -> dict[str, Any]:
        from .platform_map import platform_map
        return platform_map()

    @app.post("/workspaces/{name}/runs")
    def run(name: str, body: RunBody) -> dict[str, Any]:
        pack = _pack(name)
        stages = body.stages or STAGE_ORDER
        bad = [s for s in stages if s not in STAGES]
        if bad:
            raise HTTPException(422, f"unknown stages {bad}")
        tracer = Tracer(pack.paths.ensure().traces)
        args = argparse.Namespace(seed=body.seed if body.seed is not None
                                  else DEFAULT_SEEDS.get(name, 7), limit=None)
        report = []
        for stage in stages:
            t0 = time.perf_counter()
            STAGES[stage](pack, tracer, args)
            report.append({"stage": stage, "status": "ok",
                           "seconds": round(time.perf_counter() - t0, 2)})
        evals_path = pack.paths.reports / "evals.json"
        evals = json.loads(evals_path.read_text()) if evals_path.exists() else None
        return {"workspace": name, "stages": report,
                "evals": {"passed": evals.get("passed")} if evals else None,
                "verification": verification.derive(pack)}

    @app.get("/workspaces/{name}/approvals")
    def approvals(name: str) -> list[dict[str, Any]]:
        with _ontology(_pack(name)) as ont:
            return _pending_approvals(ont)

    @app.post("/workspaces/{name}/approvals/{seq}/decide")
    def decide(name: str, seq: int, body: DecideBody) -> dict[str, Any]:
        if body.decision not in ("approve", "deny"):
            raise HTTPException(422, "decision must be approve or deny")
        with _ontology(_pack(name)) as ont:
            row = next((p for p in _pending_approvals(ont) if p["seq"] == seq), None)
            if row is None:
                raise HTTPException(404, f"no pending approval with seq {seq}")
            if body.decision == "deny":
                ont.store.audit(actor=body.approved_by, on_behalf_of=row["actor"],
                                action=row["action"], status="denied",
                                params=row["params"], objects=[], before=None,
                                after=None, evidence=[], policy=None,
                                approved_by=body.approved_by, idempotency_key=None,
                                action_run=None)
                return {"status": "denied", "seq": seq}
            # A fresh idempotency key per approval row: the default key is
            # action+params, so approving a re-request would otherwise replay
            # the earlier run's cached result instead of executing.
            result = ont.act(row["action"], row["params"], actor=row["actor"],
                             approved_by=body.approved_by,
                             idempotency_key=f"approval-{seq}")
            return {"status": result.get("status"), "seq": seq, "result": result}

    @app.post("/workspaces/{name}/actions/{action}")
    def act(name: str, action: str, body: ActionBody) -> dict[str, Any]:
        with _ontology(_pack(name)) as ont:
            return ont.act(action, body.params, actor=body.actor,
                           on_behalf_of=body.on_behalf_of,
                           idempotency_key=body.idempotency_key)

    @app.get("/workspaces/{name}/audit/verify")
    def verify(name: str) -> dict[str, Any]:
        with _ontology(_pack(name)) as ont:
            rows = ont.store.con.execute("select count(*) from audit_log").fetchone()[0]
            return {"intact": ont.store.verify_audit_chain(), "rows": rows}

    @app.get("/workspaces/{name}/audit")
    def audit(name: str, limit: int = 50) -> list[dict[str, Any]]:
        with _ontology(_pack(name)) as ont:
            df = ont.store.con.execute(
                "select seq, ts, actor, on_behalf_of, action, status, approved_by, hash "
                "from audit_log order by seq desc limit ?", [limit]).fetchdf()
            return json.loads(df.to_json(orient="records"))

    @app.get("/workspaces/{name}/ontology")
    def ontology(name: str) -> dict[str, Any]:
        with _ontology(_pack(name)) as ont:
            return {"describe": ont.describe(), "stats": ont.store.stats()}

    @app.get("/workspaces/{name}/objects/{type_}")
    def objects(name: str, type_: str, limit: int = 50, offset: int = 0,
                x_principal: str = Header(CONSOLE_PRINCIPAL)) -> list[dict[str, Any]]:
        with _ontology(_pack(name)) as ont:
            return [ont.masked({**o, "_type": type_}, x_principal)
                    for o in ont.store.list(type_, limit, offset)]

    @app.get("/workspaces/{name}/objects/{type_}/{id_}")
    def obj(name: str, type_: str, id_: str,
            x_principal: str = Header(CONSOLE_PRINCIPAL)) -> dict[str, Any]:
        with _ontology(_pack(name)) as ont:
            o = ont.masked(ont.store.get(type_, id_), x_principal)
            if not o:
                raise HTTPException(404, f"no {type_} {id_}")
            xw = ont.store.con.execute(
                "select system, source_key, method, confidence from crosswalk where id = ?",
                [id_]).fetchall()
            return {**o,
                    "_crosswalk": [dict(zip(("system", "source_key", "method", "confidence"), r)) for r in xw],
                    "_neighbors": ont.store.neighbors(id_)}

    @app.get("/workspaces/{name}/objects/{type_}/{id_}/dashboard")
    def object_dashboard(name: str, type_: str, id_: str,
                         x_principal: str = Header(CONSOLE_PRINCIPAL)) -> dict[str, Any]:
        """Sketch 02: the governed object page. Props, identity, history,
        the audit trail touching this object, pending approvals on it, and
        per-action permits from the real policy engine for the caller."""
        pack = _pack(name)
        with _ontology(pack) as ont:
            o = ont.masked(ont.store.get(type_, id_), x_principal)
            if not o:
                raise HTTPException(404, f"no {type_} {id_}")
            xw = ont.store.con.execute(
                "select system, source_key, method, confidence from crosswalk where id = ?",
                [id_]).fetchall()
            hist = ont.store.con.execute(
                "select version, changed_by, action_run, ts from object_history "
                "where id = ? order by version", [id_]).fetchall()
            trail = []
            for row in ont.store.con.execute(
                    "select seq, actor, on_behalf_of, action, status, approved_by, objects, params "
                    "from audit_log where objects like ? or params like ? order by seq desc limit 50",
                    [f"%{id_}%", f"%{id_}%"]).fetchall():
                objs = (json.loads(row[6]) if row[6] else []) or []
                params = (json.loads(row[7]) if row[7] else {}) or {}
                # A denial touches no object, so it carries the id only in its
                # params; the trail must show denials or governance is invisible.
                if (any(x.get("id") == id_ for x in objs if isinstance(x, dict))
                        or id_ in [str(v) for v in params.values()]):
                    trail.append(dict(zip(("seq", "actor", "on_behalf_of", "action",
                                           "status", "approved_by"), row[:6])))
            pending = [p for p in _pending_approvals(ont)
                       if id_ in json.dumps(p.get("params", {}))]
            # The live combined engine, dry-run against THIS object with a
            # clock, so an escalation grant scoped to this id shows as the
            # permit it is until it expires. Each action's env carries the id
            # under that action's own object-typed param name, the same shape
            # act() builds.
            engine = orgpolicy_mod.live_engine(name)
            now = _utcnow()
            allowed = []
            for action, adef in (pack.ontology.get("actions", {}) or {}).items():
                pname = escalation_mod._object_param(name, "pack", action)
                env = {"params": {pname: id_}, "principal": x_principal, "now": now}
                d = engine.authorize(x_principal, f"action:{action}", env)
                allowed.append({"action": action, "allowed": d.allowed, "reason": d.reason,
                                "requires_approval": bool(adef.get("requires_approval"))})
            return {**o, "_crosswalk": [dict(zip(("system", "source_key", "method", "confidence"), r)) for r in xw],
                    "_neighbors": ont.store.neighbors(id_),
                    "_history": [dict(zip(("version", "changed_by", "action_run", "ts"), r)) for r in hist],
                    "_trail": trail, "_pending": pending, "_allowed_actions": allowed,
                    "_as": x_principal}

    @app.get("/workspaces/{name}/lake/tables")
    def lake_tables(name: str) -> list[dict[str, Any]]:
        lake = Lakehouse(_pack(name))
        out = []
        for ident in lake.tables():
            layer, tname = ident.split(".")
            data = lake.read(ident)
            out.append({"ident": ident, "layer": layer, "name": tname,
                        "rows": data.num_rows, "columns": data.column_names})
        return out

    @app.post("/workspaces/{name}/lake/query")
    def lake_query(name: str, body: QueryBody) -> dict[str, Any]:
        sql = _guard_select_only(body.sql)
        limit = max(1, min(body.limit, 500))
        con = Lakehouse(_pack(name)).connect()
        try:
            try:
                cur = con.execute(sql)
            except Exception as e:  # surface duckdb's message, not a 500
                raise HTTPException(400, str(e))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit)
            return {"columns": cols,
                    "rows": [[None if v is None else str(v) for v in r] for r in rows],
                    "truncated_at": limit if len(rows) == limit else None}
        finally:
            con.close()

    @app.get("/workspaces/{name}/reports/{report}")
    def reports(name: str, report: str) -> Any:
        if not re.fullmatch(r"[a-z_]+", report):
            raise HTTPException(422, "report names are lowercase words")
        path = _pack(name).paths.reports / f"{report}.json"
        if not path.exists():
            raise HTTPException(404, f"no report {report}")
        return json.loads(path.read_text())

    @app.post("/workspaces/{name}/ask")
    def ask(name: str, body: AskBody,
            x_principal: str = Header(CONSOLE_PRINCIPAL)) -> dict[str, Any]:
        pack = _pack(name)
        with _ontology(pack) as ont:
            entry = ask_mod.ask(pack, ont, body.question, principal=x_principal)
            entry["verification"] = verification.derive(pack)
            return entry

    @app.get("/workspaces/{name}/sources")
    def sources(name: str) -> list[dict[str, Any]]:
        """The connector registry, as declared: every legacy source with its
        system, format and layout. CDC and scheduled pulls are roadmap, and
        this endpoint does not pretend otherwise."""
        return _pack(name).spec.get("sources", [])

    @app.get("/workspaces/{name}/loads")
    def loads(name: str) -> dict[str, Any]:
        """Write-audit-publish, shown as it ran: every write with its audit
        branch, its checks, and whether it was published or quarantined."""
        pack = _pack(name)
        path = pack.paths.reports / "migrate.json"
        if not path.exists():
            raise HTTPException(404, "no migrate report; run the pipeline first")
        report = json.loads(path.read_text())
        lake = Lakehouse(pack)
        report["quarantine_tables"] = lake.quarantine_tables()
        return report

    @app.get("/workspaces/{name}/governance")
    def governance(name: str) -> dict[str, Any]:
        pack = _pack(name)
        actions = {
            a: {"requires_approval": bool(d.get("requires_approval")),
                "approver_roles": d.get("approver_roles", []),
                "description": d.get("description", "")}
            for a, d in (pack.ontology.get("actions", {}) or {}).items()
        }
        return {"rules": pack.policies.get("rules", []),
                "generated_rules": orgpolicy_mod.active_rules(name),
                "actions": actions,
                "note": "Default deny. Serving decisions combine the hand-written rules "
                        "with applied generated policy and unexpired escalation grants; "
                        "hand-written wins a conflict. The X-Principal header is an "
                        "identity claim, not authentication; policy is a correctness "
                        "control on this local build."}

    @app.post("/workspaces/{name}/governance/check")
    def governance_check(name: str, body: PolicyCheckBody) -> dict[str, Any]:
        _pack(name)
        env = {"now": _utcnow(), "principal": body.principal, **body.env}
        decision = orgpolicy_mod.live_engine(name).authorize(body.principal, body.action, env)
        return {"principal": body.principal, "action": body.action,
                "decision": decision.to_dict()}

    @app.get("/workspaces/{name}/principals")
    def principals(name: str) -> dict[str, Any]:
        pack = _pack(name)
        from_rules = sorted({r.get("principal") for r in pack.policies.get("rules", [])
                             if r.get("principal")})
        with _ontology(pack) as ont:
            from_audit = sorted({r[0] for r in ont.store.con.execute(
                "select distinct actor from audit_log").fetchall()})
        return {"policy_principals": from_rules, "seen_in_audit": from_audit,
                "note": "Principals are policy identities, not users. This build has no "
                        "authentication; you choose who you act as and the audit log records it."}

    @app.get("/workspaces/{name}/agents")
    def agents(name: str) -> dict[str, Any]:
        path = _pack(name).paths.reports / "agents.json"
        if not path.exists():
            raise HTTPException(404, "no agents report; run the pipeline first")
        return json.loads(path.read_text())

    @app.get("/workspaces/{name}/traces")
    def traces(name: str) -> list[dict[str, Any]]:
        tdir = _pack(name).paths.traces
        if not tdir.exists():
            return []
        out = []
        for f in sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            out.append({"trace_id": f.stem, "spans": sum(1 for _ in f.open()),
                        "mtime": f.stat().st_mtime})
        return out[:200]

    @app.get("/workspaces/{name}/traces/{trace_id}")
    def trace(name: str, trace_id: str) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[0-9a-f]+", trace_id):
            raise HTTPException(422, "trace ids are hex")
        path = _pack(name).paths.traces / f"{trace_id}.jsonl"
        if not path.exists():
            raise HTTPException(404, f"no trace {trace_id}")
        spans = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return sorted(spans, key=lambda s: s.get("start", 0))

    @app.get("/workspaces/{name}/ask/examples")
    def ask_examples(name: str,
                     x_principal: str = Header(CONSOLE_PRINCIPAL)) -> list[dict[str, Any]]:
        pack = _pack(name)
        with _ontology(pack) as ont:
            return ask_mod.examples(pack, ont, principal=x_principal)

    @app.get("/workspaces/{name}/ask/log")
    def ask_log(name: str) -> list[dict[str, Any]]:
        return ask_mod.read_log(_pack(name))

    @app.get("/workspaces/{name}/ask/metrics")
    def ask_metrics(name: str) -> dict[str, Any]:
        return ask_mod.metrics(_pack(name))

    @app.post("/workspaces/{name}/ask/replay")
    def ask_replay(name: str, body: ReplayBody) -> dict[str, Any]:
        pack = _pack(name)
        with _ontology(pack) as ont:
            try:
                return ask_mod.replay(pack, ont, body.index)
            except IndexError as e:
                raise HTTPException(404, str(e))

    app.include_router(orgpolicy_mod.router)
    app.include_router(escalation_mod.router)
    app.include_router(asof_router)
    app.include_router(otel_router)

    # The console is a committed build (console/dist), so a git clone needs
    # uv and nothing else at runtime. Absent (e.g. a bare pip install of the
    # package), the API still serves and / says where the console lives.
    if CONSOLE_DIST.exists():
        app.mount("/console", StaticFiles(directory=CONSOLE_DIST, html=True), name="console")

        @app.get("/", include_in_schema=False)
        def home() -> RedirectResponse:
            return RedirectResponse("/console/")

    return app
