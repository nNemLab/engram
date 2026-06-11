"""Episodic timeline reconstruction (#40).

A deterministic, time-ordered walk over the lifecycle events of stored content:

  - ``ingested``  : a new piece of content entered the store.
  - ``vault_edit``: a human edited a vault note (a new revision).
  - ``superseded``: a newer revision replaced an older one.

These three event types, read in chronological order, reconstruct *how* the
knowledge in the store came to be — the "what happened, in what order" view
that the semantic/keyword ``rag.query`` surface cannot express.

This is purely additive: it reads the existing append-only event log and does
not touch the ranker. An optional ``query`` scopes the walk to the lineage of
the content a topic search surfaces; without it, the walk is unscoped (bounded
only by the optional ``since``/``until`` time window).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

# The lifecycle event types that make up an episodic timeline. Other event
# types (retrieved, cited, merged, goal_*, ...) are operational noise here.
TIMELINE_EVENT_TYPES = ("ingested", "vault_edit", "superseded")

# Payload keys that carry a content hash, across the three event types.
_HASH_KEYS = ("hash", "hash_old", "hash_new")


@dataclass
class TimelineEntry:
    id: int
    ts: str
    event: str
    payload: dict[str, Any]


def _topic_hashes(conn: sqlite3.Connection, query: str, k: int) -> set[str]:
    """Resolve a topic query to the set of content hashes it surfaces.

    Imported lazily inside the function so unscoped timeline walks never pay the
    cost of importing the embedder. Failures degrade to an empty set, which the
    caller treats as "no topic match" (an empty timeline) rather than an error.
    """
    try:
        from .query import hybrid_search
        hits = hybrid_search(conn, query, top_k=k, log_retrieval=False)
    except Exception:
        return set()
    return {h.hash for h in hits}


def _payload_hashes(payload: dict[str, Any]) -> set[str]:
    return {payload[key] for key in _HASH_KEYS if isinstance(payload.get(key), str)}


def reconstruct_timeline(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    top_k: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
) -> list[TimelineEntry]:
    """Return lifecycle events in chronological order.

    query   : optional topic. When given, only events touching the content the
              topic search surfaces (and that content's revision lineage) are
              returned. When omitted, all lifecycle events in range are walked.
    top_k   : how many topic hits to seed the lineage from (defaults to a small
              fixed fan-out). Ignored when ``query`` is None.
    since   : ISO-8601 datetime; exclude events with ts < since (inclusive lower).
    until   : ISO-8601 datetime; exclude events with ts >= until (exclusive upper).
    limit   : hard cap on returned entries (oldest-first).
    """
    type_marks = ",".join("?" * len(TIMELINE_EVENT_TYPES))
    clauses = [f"type IN ({type_marks})"]
    params: list[Any] = list(TIMELINE_EVENT_TYPES)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if until:
        clauses.append("ts < ?")
        params.append(until)

    scope: set[str] | None = None
    if query is not None:
        scope = _topic_hashes(conn, query, top_k or 12)
        if not scope:
            return []

    # Order by (ts, id): id is the tie-breaker so same-millisecond events keep
    # their insertion order. We over-fetch when scoping (the WHERE can't filter
    # by hash — that lives in JSON) and apply the cap after the in-Python filter.
    rows = conn.execute(
        f"SELECT id, ts, type, payload FROM events WHERE {' AND '.join(clauses)} "
        f"ORDER BY ts, id",
        params,
    )

    out: list[TimelineEntry] = []
    for r in rows:
        payload = json.loads(r["payload"])
        if scope is not None and not (_payload_hashes(payload) & scope):
            continue
        out.append(TimelineEntry(id=r["id"], ts=r["ts"], event=r["type"], payload=payload))
        if len(out) >= limit:
            break
    return out
