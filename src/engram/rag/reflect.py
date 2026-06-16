"""Reflection brief (#42): deterministic attention stats over existing tables.

Surfaces what needs human attention — unresolved contradictions, valuable
entries going stale, and idle active goals — as plain SQL/Python aggregates.
No model calls, no network. Sibling of rag.prime; used by the session.reflect
MCP tool and the eos-reflect CLI."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..common.time import utcnow_iso


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 'Z'-suffixed UTC timestamp into an aware datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def reflect(conn: sqlite3.Connection, *,
            stale_threshold: float = 0.5,
            confidence_threshold: float = 0.6,
            idle_days: int = 10,
            sample: int = 5) -> dict[str, Any]:
    # --- unresolved contradictions -------------------------------------
    uc_count = conn.execute(
        "SELECT COUNT(*) FROM contradictions WHERE resolved = 0"
    ).fetchone()[0]
    uc_rows = conn.execute(
        "SELECT id, hash_a, hash_b, detected_at, detected_by "
        "FROM contradictions WHERE resolved = 0 "
        "ORDER BY detected_at DESC, id DESC LIMIT ?", (sample,),
    ).fetchall()
    unresolved = {
        "count": uc_count,
        "sample": [dict(r) for r in uc_rows],
    }

    # --- stale high-value content --------------------------------------
    shv_count = conn.execute(
        "SELECT COUNT(*) FROM content WHERE tombstoned = 0 "
        "AND staleness_score >= ? AND confidence >= ?",
        (stale_threshold, confidence_threshold),
    ).fetchone()[0]
    shv_rows = conn.execute(
        "SELECT hash, title, staleness_score, confidence FROM content "
        "WHERE tombstoned = 0 AND staleness_score >= ? AND confidence >= ? "
        "ORDER BY staleness_score DESC, hash ASC LIMIT ?",
        (stale_threshold, confidence_threshold, sample),
    ).fetchall()
    stale_high_value = {
        "count": shv_count,
        "sample": [dict(r) for r in shv_rows],
    }

    # --- idle active goals ---------------------------------------------
    now = _parse_iso(utcnow_iso())
    if now is None:
        now = datetime.now(UTC)
    active = conn.execute(
        "SELECT id, text, updated_at FROM goals WHERE status = 'active'"
    ).fetchall()
    idle = []
    for g in active:
        updated = _parse_iso(g["updated_at"])
        if updated is None:
            continue
        days = (now - updated).total_seconds() / 86400.0
        if days >= idle_days:
            idle.append({
                "id": g["id"],
                "text": g["text"],
                "updated_at": g["updated_at"],
                "days_idle": int(days),
            })
    idle.sort(key=lambda r: r["days_idle"], reverse=True)
    idle_goals = {"count": len(idle), "sample": idle[:sample]}

    return {
        "unresolved_contradictions": unresolved,
        "stale_high_value": stale_high_value,
        "idle_goals": idle_goals,
        "brief": _render_brief(unresolved, stale_high_value, idle_goals),
    }


def _render_brief(unresolved: dict[str, Any], stale: dict[str, Any],
                  idle: dict[str, Any]) -> str:
    parts: list[str] = []
    if unresolved["count"]:
        parts.append(f"{unresolved['count']} contradictions unresolved")
    if stale["count"]:
        parts.append(f"{stale['count']} stale high-value entries")
    if idle["count"]:
        g = idle["sample"][0]
        parts.append(f"goal '{g['text']}' idle {g['days_idle']}d"
                     + (f" (+{idle['count'] - 1} more)" if idle["count"] > 1 else ""))
    if not parts:
        return "Nothing needs attention."
    return " · ".join(parts)
