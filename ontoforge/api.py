"""REST API over the ontology. Apps use this; agents use the same calls through MCP.

  GET  /                      pack summary and what the ontology contains
  GET  /objects/{type}        list objects
  GET  /objects/{type}/{id}   one object with provenance, crosswalk and neighbors
  POST /functions/{name}      run a governed read          body: {"args": {...}}
  POST /actions/{name}        run a governed write         body: {"params": {...}, "idempotency_key": "..."}
  GET  /audit                 latest audit records
  GET  /reports/{name}        migrate | reconcile | ontology | agents | evals

The caller's identity comes from the X-Principal header (default user:anonymous),
standing in for a real identity provider. Every call passes the policy engine.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .ontology.runtime import ActionError, Ontology
from .packs import load_pack
from .tracing import Tracer


class FnBody(BaseModel):
    args: dict[str, Any] = {}


class ActBody(BaseModel):
    params: dict[str, Any] = {}
    idempotency_key: str | None = None
    on_behalf_of: str | None = None
    approved_by: str | None = None


def create_app(pack_name: str) -> FastAPI:
    pack = load_pack(pack_name)
    tracer = Tracer(pack.paths.traces)
    ont = Ontology(pack, tracer)
    lock = threading.Lock()   # one DuckDB writer
    app = FastAPI(title=f"ontoforge: {pack.title}", description=pack.spec.get("story", ""))

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"pack": pack.name, "title": pack.title, "decision": pack.spec.get("decision"),
                "ontology": ont.describe(), "stats": ont.store.stats()}

    @app.get("/objects/{type_}")
    def objects(type_: str, limit: int = 50, offset: int = 0,
                x_principal: str = Header("user:anonymous")) -> list[dict[str, Any]]:
        with lock:
            return [ont.masked({**o, "_type": type_}, x_principal) for o in ont.store.list(type_, limit, offset)]

    @app.get("/objects/{type_}/{id_}")
    def obj(type_: str, id_: str, x_principal: str = Header("user:anonymous")) -> dict[str, Any]:
        with lock:
            o = ont.masked(ont.store.get(type_, id_), x_principal)
            if not o:
                raise HTTPException(404, f"no {type_} {id_}")
            xw = ont.store.con.execute("select system, source_key, method, confidence from crosswalk where id = ?",
                                       [id_]).fetchall()
            hist = ont.store.con.execute("select version, changed_by, action_run, ts from object_history "
                                         "where id = ? order by version", [id_]).fetchall()
            return {**o, "_crosswalk": [dict(zip(("system", "source_key", "method", "confidence"), r)) for r in xw],
                    "_neighbors": ont.store.neighbors(id_),
                    "_history": [dict(zip(("version", "changed_by", "action_run", "ts"), r)) for r in hist]}

    @app.post("/functions/{name}")
    def fn(name: str, body: FnBody, x_principal: str = Header("user:anonymous")) -> dict[str, Any]:
        with lock:
            try:
                return ont.call(name, body.args, principal=x_principal)
            except PermissionError as e:
                raise HTTPException(403, str(e))
            except ActionError as e:
                raise HTTPException(400, str(e))

    @app.post("/actions/{name}")
    def act(name: str, body: ActBody, x_principal: str = Header("user:anonymous")) -> dict[str, Any]:
        with lock:
            try:
                res = ont.act(name, body.params, actor=x_principal, on_behalf_of=body.on_behalf_of,
                              idempotency_key=body.idempotency_key, approved_by=body.approved_by)
            except ActionError as e:
                raise HTTPException(400, str(e))
            if res["status"] in ("applied_pending_sync",):
                res["outbox"] = ont.deliver_outbox()
            return res

    @app.get("/audit")
    def audit(limit: int = 50) -> list[dict[str, Any]]:
        with lock:
            df = ont.store.con.execute("select * from audit_log order by seq desc limit ?", [limit]).fetchdf()
        return json.loads(df.to_json(orient="records"))

    @app.get("/reports/{name}")
    def report(name: str) -> Any:
        p = pack.paths.reports / f"{name}.json"
        if not p.exists():
            raise HTTPException(404, "run the pipeline first")
        return json.loads(p.read_text())

    return app
