"""goals.* tools: declarative active investigations driving agentic behavior."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from ... import log as event_log


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:

    def set_(args: dict[str, Any]) -> dict[str, Any]:
        gid = args.get("id") or uuid.uuid4().hex[:12]
        text = args["text"]
        priority = int(args.get("priority", 0))
        metadata = json.dumps(args.get("metadata", {}))
        now = _now()
        conn.execute(
            "INSERT INTO goals (id, text, status, priority, metadata, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, priority=excluded.priority, "
            "metadata=excluded.metadata, updated_at=excluded.updated_at",
            (gid, text, priority, metadata, now, now),
        )
        event_log.append(conn, "goal_set", {"goal_id": gid, "text": text}, actor="agent")
        return {"id": gid}

    def list_(args: dict[str, Any]) -> list[dict[str, Any]]:
        status = args.get("status", "active")
        rows = conn.execute(
            "SELECT id, text, status, priority, metadata, created_at, updated_at "
            "FROM goals WHERE status = ? ORDER BY priority DESC, updated_at DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve(args: dict[str, Any]) -> dict[str, Any]:
        gid = args["id"]
        cur = conn.execute(
            "UPDATE goals SET status = 'resolved', updated_at = ? WHERE id = ?",
            (_now(), gid),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"error": "not found", "id": gid}
        event_log.append(conn, "goal_resolved", {"goal_id": gid}, actor="agent")
        return {"id": gid, "status": "resolved"}

    return {
        "goals.set": {
            "description": "Create or update an active investigation goal.",
            "input_schema": {
                "type": "object", "required": ["text"],
                "properties": {
                    "id":       {"type": "string"},
                    "text":     {"type": "string"},
                    "priority": {"type": "integer"},
                    "metadata": {"type": "object"},
                },
            },
            "handler": set_,
        },
        "goals.list": {
            "description": "List goals by status (default: active).",
            "input_schema": {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["active", "paused", "resolved"]}},
            },
            "handler": list_,
        },
        "goals.resolve": {
            "description": "Mark a goal resolved.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "handler": resolve,
        },
    }
