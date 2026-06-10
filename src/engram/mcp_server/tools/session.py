"""session.* tools: priming the kernel with goals + relevant memory at session start."""
from __future__ import annotations

import sqlite3
from typing import Any


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    def prime_(args: dict[str, Any]) -> dict[str, Any]:
        from ...rag.prime import prime
        return prime(conn, cwd=args.get("cwd"),
                     token_budget=int(args.get("token_budget", 1500)))

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
    }
