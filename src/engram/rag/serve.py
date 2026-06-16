"""Phase 2: the warm retrieval daemon. A tiny loopback JSON API over the Phase-1
grounding/prime core, so the ambient hook (Phase 3) gets sub-second retrieval
without cold-importing torch each turn. Plain JSON — no MCP handshake."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .grounding import ground
from .prime import prime

logger = logging.getLogger("engram.rag.serve")


def build_serve_app(conn: sqlite3.Connection | None = None) -> Starlette:
    """ASGI app exposing /healthz, /grounding, /prime, /cite over one warm sqlite DB.

    Pass `conn` for tests; production opens one from config in the lifespan.
    Shared-connection paths stay serialized by an async lock. `/grounding` and
    `/prime` are offloaded to worker threads so sentence-transformer inference
    never blocks the event loop. For those routes we open a fresh sqlite
    connection inside each worker thread (same DB file) to avoid thread-affinity
    pitfalls on injected test connections that keep sqlite's default
    `check_same_thread=True`, while allowing independent requests to overlap."""
    state: dict[str, Any] = {"conn": conn, "db_path": ""}
    if state["conn"] is not None:
        row = state["conn"].execute("PRAGMA database_list").fetchone()
        state["db_path"] = row[2] if row else ""
    lock = asyncio.Lock()

    def _call_with_fresh_conn(fn: Callable[..., Any], kw: dict[str, Any]) -> Any:
        from ..common.db import open_readonly_connection

        conn_ = open_readonly_connection(Path(state["db_path"]))
        try:
            return fn(conn_, **kw)
        finally:
            conn_.close()

    async def _run(fn: Callable[..., Any], *, offload: bool = False, **kw: Any) -> Any:
        # Offloaded calls run against their own short-lived connection and do
        # not share mutable state, so don't hold the shared-connection lock.
        if offload and state["db_path"]:
            return await asyncio.to_thread(_call_with_fresh_conn, fn, kw)

        async with lock:
            if not offload:
                return fn(state["conn"], **kw)
            # In-memory DBs have no reopenable file path; keep thread-bound
            # execution on-loop and serialized for correctness.
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
        try:
            out = await _run(ground, offload=True, query=query, token_budget=body.get("token_budget"))
        except Exception as exc:
            logger.exception("/grounding failed", extra={"cause": str(exc)})
            return JSONResponse({"error": "internal grounding failure"}, status_code=500)
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
        try:
            out = await _run(prime, offload=True, cwd=cwd, token_budget=tb)
        except Exception as exc:
            logger.exception("/prime failed", extra={"cause": str(exc)})
            return JSONResponse({"error": "internal prime failure"}, status_code=500)
        return JSONResponse(out)

    async def cite(req: Request) -> JSONResponse:
        try:
            body = await req.json()
            hashes = body["hashes"]
            if not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes):
                raise ValueError
        except Exception:
            return JSONResponse({"error": "hashes (list of strings) required"}, status_code=400)

        def _resolve_and_record(conn):
            from .usage import record_cited

            full = []
            for h in hashes:
                rows = conn.execute(
                    "SELECT hash FROM content WHERE hash = ? OR hash LIKE ? || '%' LIMIT 2",
                    (h, h),
                ).fetchall()
                if len(rows) == 1:
                    full.append(rows[0]["hash"])
            if not full:
                return 0
            return record_cited(conn, full, query=body.get("query", ""), turn_id=body.get("turn_id"))

        n = await _run(_resolve_and_record)
        return JSONResponse({"cited": n})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if state["conn"] is None:
            from ..common.db import get_connection

            state["conn"] = get_connection()
        row = state["conn"].execute("PRAGMA database_list").fetchone()
        state["db_path"] = row[2] if row else ""
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
            Route("/cite", cite, methods=["POST"]),
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
