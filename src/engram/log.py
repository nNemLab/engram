"""Event log: append-only writes, typed reads."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class Event:
    id: int
    ts: str
    type: str
    payload: dict[str, Any]
    actor: str | None
    correlation_id: str | None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def append(
    conn: sqlite3.Connection,
    type: str,
    payload: dict[str, Any],
    actor: str | None = None,
    correlation_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (ts, type, payload, actor, correlation_id) VALUES (?, ?, ?, ?, ?)",
        (_now(), type, json.dumps(payload, separators=(",", ":")), actor, correlation_id),
    )
    return int(cur.lastrowid or 0)


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
