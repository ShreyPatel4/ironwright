"""MCP server: the ontology's functions and actions as tools any agent can call.

Run it and point an MCP client (Claude Desktop, an AgentCore Gateway target, a
LangGraph or Strands agent) at it:

  ironwright mcp --pack retail

Tools are generated from ontology.yaml, so a new function or action becomes a
tool with no code. The agent identity comes from IRONWRIGHT_PRINCIPAL; every
call goes through the same policy engine and audit log as the REST API.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import setting
from .ontology.runtime import Ontology
from .packs import load_pack
from .tracing import Tracer


def build_server(pack_name: str) -> FastMCP:
    pack = load_pack(pack_name)
    ont = Ontology(pack, Tracer(pack.paths.traces))
    principal = setting("PRINCIPAL", "agent:mcp-client")
    mcp = FastMCP(f"ironwright-{pack_name}")

    @mcp.tool(description=f"Describe the {pack.title} ontology: object types, links, functions and actions.")
    def describe_ontology() -> str:
        return json.dumps(ont.describe(), default=str)

    @mcp.tool(description="Get one ontology object by type and id, with its neighbors.")
    def get_object(object_type: str, object_id: str) -> str:
        o = ont.masked(ont.store.get(object_type, object_id), principal)
        return json.dumps({**(o or {}), "_neighbors": ont.store.neighbors(object_id)} if o else {"error": "not found"},
                          default=str)

    @mcp.tool(description="List objects of a type.")
    def list_objects(object_type: str, limit: int = 20) -> str:
        return json.dumps([ont.masked({**o, "_type": object_type}, principal)
                           for o in ont.store.list(object_type, limit)], default=str)

    def make_fn(name: str, fdef: dict[str, Any]):
        def tool(args: dict[str, Any]) -> str:
            try:
                return json.dumps(ont.call(name, args, principal=principal), default=str)
            except Exception as e:
                return json.dumps({"error": str(e)})
        return tool

    def make_act(name: str, adef: dict[str, Any]):
        def tool(params: dict[str, Any], idempotency_key: str | None = None) -> str:
            res = ont.act(name, params, actor=principal, idempotency_key=idempotency_key)
            if res["status"] == "applied_pending_sync":
                res["outbox"] = ont.deliver_outbox()
            return json.dumps(res, default=str)
        return tool

    for name, fdef in pack.ontology.get("functions", {}).items():
        mcp.add_tool(make_fn(name, fdef), name=f"fn_{name}",
                     description=f"{fdef.get('description', '')} Args: {json.dumps(fdef.get('params', {}))}")
    for name, adef in pack.ontology.get("actions", {}).items():
        approval = " Requires human approval." if adef.get("requires_approval") else ""
        mcp.add_tool(make_act(name, adef), name=f"act_{name}",
                     description=f"{adef.get('description', '')}{approval} Params: {json.dumps(adef.get('params', {}))}")
    return mcp
