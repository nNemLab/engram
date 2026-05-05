"""Single MCP server. All subsystems exposed as namespaced tools.

Tool naming: <namespace>.<verb>  (kb.write, rag.query, research.fetch, ...)

The server holds one long-lived sqlite connection; tools take it as their first arg.
Transport: stdio (Claude Code launches us as a subprocess).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..common.db import get_connection
from . import tools as toolmod


logger = logging.getLogger("engram.mcp")


def build_server() -> Server:
    server = Server("engram")
    conn = get_connection()
    registry = toolmod.build_registry(conn)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=name,
                description=spec.description,
                inputSchema=spec.input_schema,
            )
            for name, spec in registry.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        spec = registry.get(name)
        if not spec:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        try:
            result = await asyncio.to_thread(spec.handler, arguments or {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool failed: %s", name)
            return [TextContent(type="text", text=json.dumps({"error": str(exc), "tool": name}))]
        if isinstance(result, str):
            return [TextContent(type="text", text=result)]
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def _run() -> None:
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_run())
