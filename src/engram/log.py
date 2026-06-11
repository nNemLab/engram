"""Event log: append-only writes, typed reads.

Tamper-evidence (#45)
---------------------
Each event is part of a SHA-256 hash chain: its `event_hash` is computed over the
event's canonical fields plus `prev_hash` (the `event_hash` of the chain head at
insert time). Any retroactive edit, deletion, or reorder of a chained row is then
detectable by `maintenance.verify` re-walking the chain.

The chain begins at the schema-005 migration boundary: rows written before it keep
`event_hash = NULL` and are not chained (verify skips them). The first event
appended after the migration has `prev_hash = ''` (the genesis marker). Appends are
serialized through a single sqlite connection (see common.db._connect, which opens
one autocommit connection per process), so the INSERT-then-hash sequence below is
race-free without explicit locking.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .common.time import utcnow_iso

# Genesis marker: prev_hash of the first event appended after the 005 migration.
GENESIS_PREV_HASH = ""


@dataclass
class Event:
    id: int
    ts: str
    type: str
    payload: dict[str, Any]
    actor: str | None
    correlation_id: str | None


def _has_hash_chain(conn: sqlite3.Connection) -> bool:
    """True if the events table carries the 005 hash-chain columns."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    return "event_hash" in cols and "prev_hash" in cols


def canonical_event_hash(
    *,
    id: int,
    ts: str,
    type: str,
    payload: str,
    actor: str | None,
    correlation_id: str | None,
    prev_hash: str,
) -> str:
    """Deterministic SHA-256 over an event's canonical fields plus prev_hash.

    `payload` is the exact JSON string stored in the row (compact separators, as
    written by `append`). Null `actor`/`correlation_id` canonicalize to the empty
    string. Fields are joined with newlines — newlines cannot appear inside the
    compact JSON, the timestamp, or the type — so the encoding is unambiguous.
    verify recomputes this from the stored row to detect tampering.
    """
    parts = [
        str(id),
        ts,
        type,
        payload,
        actor or "",
        correlation_id or "",
        prev_hash,
    ]
    # The newline-join framing is unambiguous only because none of these fields
    # can contain a raw newline: id is numeric, ts is strict ISO, payload is
    # compact JSON (newlines escaped), and type/actor/correlation_id are
    # controlled enums/identifiers. If a future free-form field is added here,
    # switch to length-prefixed framing or hash a JSON array instead.
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _chain_head_hash(conn: sqlite3.Connection) -> str:
    """event_hash of the most recent chained row, or GENESIS_PREV_HASH if none."""
    row = conn.execute(
        "SELECT event_hash FROM events WHERE event_hash IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["event_hash"] if row else GENESIS_PREV_HASH


def append(
    conn: sqlite3.Connection,
    type: str,
    payload: dict[str, Any],
    actor: str | None = None,
    correlation_id: str | None = None,
) -> int:
    ts = utcnow_iso("ms")
    payload_json = json.dumps(payload, separators=(",", ":"))
    cur = conn.execute(
        "INSERT INTO events (ts, type, payload, actor, correlation_id) VALUES (?, ?, ?, ?, ?)",
        (ts, type, payload_json, actor, correlation_id),
    )
    event_id = int(cur.lastrowid or 0)

    # Hash-chain link (#45). Skipped on pre-005 databases lacking the columns.
    if _has_hash_chain(conn):
        prev_hash = _chain_head_hash(conn)
        event_hash = canonical_event_hash(
            id=event_id,
            ts=ts,
            type=type,
            payload=payload_json,
            actor=actor,
            correlation_id=correlation_id,
            prev_hash=prev_hash,
        )
        conn.execute(
            "UPDATE events SET prev_hash = ?, event_hash = ? WHERE id = ?",
            (prev_hash, event_hash, event_id),
        )
    return event_id


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        ts=row["ts"],
        type=row["type"],
        payload=json.loads(row["payload"]),
        actor=row["actor"],
        correlation_id=row["correlation_id"],
    )


def since(conn: sqlite3.Connection, last_id: int, types: list[str] | None = None,
          limit: int = 1000) -> Iterator[Event]:
    if types:
        q_marks = ",".join("?" * len(types))
        rows = conn.execute(
            f"SELECT * FROM events WHERE id > ? AND type IN ({q_marks}) ORDER BY id LIMIT ?",
            (last_id, *types, limit),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, limit),
        )
    for r in rows:
        yield _row_to_event(r)


def latest_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM events").fetchone()
    return int(row["id"])
