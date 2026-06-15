"""Single MCP server. All subsystems exposed as namespaced tools.

Tool naming: <namespace>.<verb>  (kb.write, rag.query, research.fetch, ...)

The server holds one long-lived sqlite connection; tools take it as their first arg.
Transport: stdio (Claude Code launches us as a subprocess).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..common.db import db_lock, get_connection
from . import tools as toolmod

logger = logging.getLogger("engram.mcp")


def build_server(conn: sqlite3.Connection | None = None) -> Server:
    server = Server("engram")
    if conn is None:
        conn = get_connection()
    registry = toolmod.build_registry(conn)
    lock = db_lock()

    def _invoke(handler: Any, args: dict[str, Any]) -> Any:
        # Tool handlers run on `asyncio.to_thread` worker threads and all share
        # the one long-lived `conn`. Hold the process-wide DB lock for the whole
        # handler so concurrent tool calls never drive the single connection at
        # once (#83) — the multi-statement write paths (dedup gate, resolve,
        # sources) stay atomic and never race into `database is locked`.
        with lock:
            return handler(args)

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
            result = await asyncio.to_thread(_invoke, spec.handler, arguments or {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool failed: %s", name)
            return [TextContent(type="text", text=json.dumps({"error": str(exc), "tool": name}))]
        if isinstance(result, str):
            return [TextContent(type="text", text=result)]
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def _run() -> None:
    # Own the long-lived connection's lifecycle so the stdio daemon closes it on
    # shutdown instead of leaking it + WAL sidecars (#92).
    conn = get_connection()
    try:
        server = build_server(conn)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_run())
