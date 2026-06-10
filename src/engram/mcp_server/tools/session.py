"""session.* tools: priming the kernel with goals + relevant memory at session start."""
from __future__ import annotations

import sqlite3
from typing import Any


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    def prime_(args: dict[str, Any]) -> dict[str, Any]:
        from ...rag.prime import prime
        return prime(conn, cwd=args.get("cwd"),
                     token_budget=int(args.get("token_budget", 1500)))

    def reflect_(args: dict[str, Any]) -> dict[str, Any]:
        from ...rag.reflect import reflect
        kw: dict[str, Any] = {}
        if "stale_threshold" in args:
            kw["stale_threshold"] = float(args["stale_threshold"])
        if "confidence_threshold" in args:
            kw["confidence_threshold"] = float(args["confidence_threshold"])
        if "idle_days" in args:
            kw["idle_days"] = int(args["idle_days"])
        if "sample" in args:
            kw["sample"] = int(args["sample"])
        return reflect(conn, **kw)

    return {
        "session.prime": {
            "description": "Return a priming block (active goals + recent high-confidence "
                           "knowledge) to seed a new session. Call at session start.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "cwd":          {"type": "string"},
                    "token_budget": {"type": "integer"},
                },
            },
            "handler": prime_,
        },
        "session.reflect": {
            "description": "Return a deterministic reflection brief: unresolved "
                           "contradictions, stale high-value entries, and idle active "
                           "goals. Surfaces what needs attention; no model calls.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "stale_threshold":      {"type": "number"},
                    "confidence_threshold": {"type": "number"},
                    "idle_days":            {"type": "integer"},
                    "sample":               {"type": "integer"},
                },
            },
            "handler": reflect_,
        },
    }
