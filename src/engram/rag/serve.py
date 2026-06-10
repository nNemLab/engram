"""Phase 2: the warm retrieval daemon. A tiny loopback JSON API over the Phase-1
grounding/prime core, so the ambient hook (Phase 3) gets sub-second retrieval
without cold-importing torch each turn. Plain JSON — no MCP handshake."""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import AsyncIterator, Callable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .grounding import ground
from .prime import prime


def build_serve_app(conn: sqlite3.Connection | None = None) -> Starlette:
    """ASGI app exposing /healthz, /grounding, /prime over a single warm sqlite
    connection. Pass `conn` for tests; production opens one from config in the
    lifespan. Requests are serialized (one shared read connection) and the sync
    grounding/prime work runs synchronously under an async lock (fast SQLite
    reads; the connection stays loop-bound)."""
    state: dict[str, Any] = {"conn": conn}
    lock = asyncio.Lock()

    async def _run(fn: Callable[..., Any], **kw: Any) -> Any:
        # Run synchronously under the lock — grounding/prime are fast SQLite reads
        # that complete in <50 ms and must not cross thread boundaries (sqlite3
        # connections are not thread-safe by default).
        async with lock:
            return fn(state["conn"], **kw)

    async def healthz(_req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def grounding(req: Request) -> JSONResponse:
        try:
            body = await req.json()
            query = body["query"]
            if not isinstance(query, str) or not query.strip():
                raise ValueError
        except Exception:
            return JSONResponse({"error": "query (non-empty string) required"}, status_code=400)
        out = await _run(ground, query=query, token_budget=body.get("token_budget"))
        return JSONResponse(out)

    async def prime_(req: Request) -> JSONResponse:
        try:
            body = await req.json()
            tb = int(body.get("token_budget", 1500))
            cwd = body.get("cwd")
        except Exception:
            return JSONResponse(
                {"error": "invalid body (expected a JSON object; token_budget must be an integer)"},
                status_code=400,
            )
        out = await _run(prime, cwd=cwd, token_budget=tb)
        return JSONResponse(out)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if state["conn"] is None:
            from ..common.db import get_connection
            state["conn"] = get_connection()
        # Pre-warm the embedder so the first real /grounding is fast.
        with contextlib.suppress(Exception):
            from .embed import _get_model
            await asyncio.to_thread(_get_model)
        yield

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/grounding", grounding, methods=["POST"]),
            Route("/prime", prime_, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def serve(host: str = "127.0.0.1", port: int | None = None) -> None:
    """Run the grounding daemon (blocking). Loopback-only by default."""
    import uvicorn

    from ..common.config import load_config
    if port is None:
        port = load_config().grounding.port
    uvicorn.run(build_serve_app(), host=host, port=port)
