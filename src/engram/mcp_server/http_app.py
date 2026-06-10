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

from .server import build_server


def build_http_app(*, json_response: bool = False) -> Starlette:
    """ASGI app serving the engram MCP server over streamable HTTP at /mcp."""
    server = build_server()
    manager = StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=json_response,
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)
