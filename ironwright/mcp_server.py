"""MCP server: the ontology's functions and actions as tools any agent can call.

Run it and point an MCP client (Claude Desktop, an AgentCore Gateway target, a
LangGraph or Strands agent) at it:

  ironwright mcp --pack retail

Tools are generated from ontology.yaml, so a new function or action becomes a
tool with no code, and there are no others. The server runs on stdio, so it binds
nothing. The identity comes from IRONWRIGHT_PRINCIPAL, must be agent:*, and is
never a tool argument; every call goes through the same policy engine and audit
log as the REST API.
"""
from __future__ import annotations

import json
import re
from typing import Any

from mcp.server.mcpserver import MCPServer

from .api import kill_switch_on
from .config import setting
from .ontology.runtime import ActionError, Ontology
from .packs import load_pack
from .tracing import Tracer


def build_server(pack_name: str) -> MCPServer:
    pack = load_pack(pack_name)
    ont = Ontology(pack, Tracer(pack.paths.traces))
    principal = setting("PRINCIPAL", "agent:mcp-client")
    if not re.fullmatch(r"agent:\S+", principal):
        raise ValueError(f"the MCP server runs as an agent:* principal, not {principal!r}")
    mcp = MCPServer(f"ironwright-{pack_name}")

    def make_fn(name: str, fdef: dict[str, Any]):
        def tool(args: dict[str, Any]) -> str:
            try:
                return json.dumps(ont.call(name, args, principal=principal), default=str)
            except (PermissionError, ActionError) as e:
                return json.dumps({"error": str(e)})
        return tool

    def make_act(name: str, adef: dict[str, Any]):
        def tool(params: dict[str, Any], idempotency_key: str | None = None) -> str:
            if kill_switch_on():
                return json.dumps({"error": "kill switch on: actions are off"})
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
