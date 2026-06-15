"""#113: the MCP server must not serialize non-DB tool work.

PR #112 held the process-wide DB lock around the entire tool handler in
`build_server._invoke`, so a slow non-DB handler (network fetch in research.*,
subprocess in playbook.*) blocked every other tool call for its full duration.
The fix narrows the lock: the server wraps its shared connection in a
`LockingConnection` proxy (each DB access is serialized) and stops wrapping the
whole handler, so handlers that do no DB work run concurrently.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

from mcp.types import CallToolRequest, CallToolRequestParams

from engram.common.db import LockingConnection


def _sleepy_registry(_conn):
    """A registry of one non-DB handler that just sleeps (no connection use)."""
    from engram.mcp_server.tools import ToolSpec

    def slow(_args):
        time.sleep(0.3)  # simulated non-DB work (network/subprocess)
        return {"ok": True}

    return {"t.slow": ToolSpec(description="", input_schema={"type": "object"}, handler=slow)}


async def test_server_wraps_connection_and_overlaps_non_db_calls(monkeypatch):
    import engram.mcp_server.server as srv

    captured: dict = {}
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr(srv, "get_connection", lambda: raw)

    def fake_build_registry(conn):
        captured["conn"] = conn
        return _sleepy_registry(conn)

    monkeypatch.setattr(srv.toolmod, "build_registry", fake_build_registry)

    server = srv.build_server()

    # The registry (and therefore every tool handler) must receive the
    # lock-serialized proxy, not the bare shared connection -- that is what keeps
    # all shared-connection access under the DB lock once the broad lock is gone.
    assert isinstance(captured["conn"], LockingConnection)

    handler = server.request_handlers[CallToolRequest]

    def call():
        return handler(
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="t.slow", arguments={}),
            )
        )

    start = time.monotonic()
    await asyncio.gather(call(), call())
    elapsed = time.monotonic() - start

    # Two 0.3s non-DB handlers serialized would take >= 0.6s; overlapping ~0.3s.
    assert elapsed < 0.5, f"non-DB tool calls were serialized (elapsed={elapsed:.3f}s)"
