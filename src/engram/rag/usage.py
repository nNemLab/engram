"""Citation usage signal (#39): `cited` events -> content_usage counters -> a
ranking factor. content_usage is a cache; the event log is canonical (rebuildable)."""
from __future__ import annotations

import json
import math
import sqlite3

from .. import log as event_log
from ..common.time import utcnow_iso


def record_cited(conn: sqlite3.Connection, hashes: list[str], *, query: str = "",
                 turn_id: str | None = None, actor: str = "agent") -> int:
    """Append a `cited` event (payload {hashes, query, turn_id}) and bump content_usage.

    Idempotent per (turn_id, hash): if the same hash was already cited under the same
    turn_id, it is not counted again — this lets the Phase-3 Stop-hook safely backstop
    the rag.cite tool. **If turn_id is None, NO deduplication is performed and every
    call counts** — callers that may fire more than once per turn (e.g. a tool + a
    backstop hook) MUST pass a stable turn_id.

    Returns the number of freshly-recorded hashes (those NOT skipped by dedup).
    """
    now = utcnow_iso("ms")
    # Filter out hashes already counted for this turn
    active: list[str] = []
    for h in hashes:
        if turn_id is not None:
            seen = conn.execute(
                "SELECT 1 FROM events WHERE type='cited' "
                "AND json_extract(payload,'$.turn_id')=? "
                "AND EXISTS (SELECT 1 FROM json_each(json_extract(payload,'$.hashes')) WHERE value=?) LIMIT 1",
                (turn_id, h),
            ).fetchone()
            if seen:
                continue
        active.append(h)
    if not active:
        return 0
    # One event per call, carrying all active hashes
    event_log.append(conn, "cited",
                     {"hashes": active, "query": query, "turn_id": turn_id}, actor=actor)
    for h in active:
        conn.execute(
            "INSERT INTO content_usage (content_hash, use_count, last_cited_at) VALUES (?,1,?) "
            "ON CONFLICT(content_hash) DO UPDATE SET use_count = use_count + 1, last_cited_at = ?",
            (h, now, now),
        )
    return len(active)


def rebuild_usage(conn: sqlite3.Connection) -> None:
    """Recompute content_usage from the canonical `cited` event log."""
    conn.execute("DELETE FROM content_usage")
    rows = conn.execute(
        "SELECT payload, ts FROM events WHERE type='cited' ORDER BY id"
    ).fetchall()
    for r in rows:
        payload = json.loads(r["payload"])
        # Support both old single-hash payloads and current multi-hash payloads
        hashes: list[str] = payload.get("hashes") or []
        if not hashes:
            single = payload.get("hash")
            if single:
                hashes = [single]
        for h in hashes:
            conn.execute(
                "INSERT INTO content_usage (content_hash, use_count, last_cited_at) VALUES (?,1,?) "
                "ON CONFLICT(content_hash) DO UPDATE SET use_count = use_count + 1, last_cited_at = ?",
                (h, r["ts"], r["ts"]),
            )


def usage_factor(conn: sqlite3.Connection, content_hash: str, *, weight: float,
                 half_life_days: int = 90) -> float:
    """Multiplicative ranking factor >= 1.0: 1 + weight * log1p(use_count) * recency."""
    row = conn.execute(
        "SELECT use_count, last_cited_at FROM content_usage WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    if not row or not row["use_count"]:
        return 1.0
    from datetime import UTC, datetime
    recency = 1.0
    if row["last_cited_at"]:
        try:
            ts = datetime.fromisoformat(row["last_cited_at"].replace("Z", "+00:00"))
            age = max(0.0, (datetime.now(UTC) - ts).total_seconds() / 86400.0)
            recency = 0.5 ** (age / max(1, half_life_days))
        except ValueError:
            pass
    return 1.0 + weight * math.log1p(row["use_count"]) * recency
