"""Streamable-HTTP transport for the engram MCP server.

Wraps the existing build_server() registry in an ASGI app mounted at /mcp, so any
HTTP-capable MCP client (Claude Code via `--transport http`, etc.) can reach it.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from ..common.db import get_connection
from .server import build_server


def build_http_app(*, json_response: bool = False) -> Starlette:
    """ASGI app serving the engram MCP server over streamable HTTP at /mcp."""
    # Own the long-lived connection's lifecycle so the HTTP daemon closes it on
    # shutdown instead of leaking it + WAL sidecars (#138), symmetrically with the
    # stdio _run() path (#92). build_server() still wraps it in LockingConnection.
    conn = get_connection()
    server = build_server(conn)
    manager = StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=json_response,
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            async with manager.run():
                yield
        finally:
            # Close the DB connection after the session manager has torn down, so
            # in-flight requests can't touch a closed connection (#138).
            conn.close()

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)
