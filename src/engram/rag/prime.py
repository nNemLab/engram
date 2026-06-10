"""Session priming (#42): assemble active goals + recent high-confidence entries
into a context block. Deterministic; no model calls. Used by the session.prime MCP
tool and (Phase 2) the daemon /prime endpoint."""
from __future__ import annotations

import sqlite3
from typing import Any


def prime(conn: sqlite3.Connection, *, cwd: str | None = None,
          token_budget: int = 1500, max_goals: int = 5, max_entries: int = 5) -> dict[str, Any]:
    goals = conn.execute(
        "SELECT text, priority FROM goals WHERE status='active' "
        "ORDER BY priority DESC, updated_at DESC LIMIT ?", (max_goals,),
    ).fetchall()
    entries = conn.execute(
        "SELECT title, hash FROM content WHERE tombstoned=0 "
        "ORDER BY confidence DESC, fetched_at DESC LIMIT ?", (max_entries,),
    ).fetchall()
    if not goals and not entries:
        return {"block": ""}
    lines = ["## Engram session priming"]
    if goals:
        lines.append("**Active goals:**")
        lines += [f"- {g['text']}" for g in goals]
    if entries:
        lines.append("**Recent high-confidence knowledge:**")
        lines += [f"- {e['title'] or '(untitled)'} `[{e['hash'][:12]}]`" for e in entries]
    block = "\n".join(lines)
    # crude budget guard
    if len(block) // 4 > token_budget:
        block = block[: token_budget * 4]
    return {"block": block}
