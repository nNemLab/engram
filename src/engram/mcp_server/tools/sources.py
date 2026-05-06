"""MCP tools: sources.* namespace for CRUD on the sources registry."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

DEFAULT_SCHEDULE = {"sitemap": "7d", "github-repo": "1d"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "config" in d and isinstance(d["config"], str):
        try:
            d["config"] = json.loads(d["config"])
        except (TypeError, ValueError):
            pass
    return d


def register(conn: sqlite3.Connection) -> dict[str, dict]:

    def add(args: dict[str, Any]) -> dict[str, Any]:
        adapter = args["adapter"]
        if adapter not in DEFAULT_SCHEDULE:
            return {"error": f"unknown adapter: {adapter}"}
        schedule = args.get("schedule") or DEFAULT_SCHEDULE[adapter]
        config = json.dumps(args.get("config") or {})
        conn.execute(
            "INSERT INTO sources "
            "(id, name, adapter, url, config, schedule, source_tier, paused, next_poll_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                args["id"],
                args["name"],
                adapter,
                args["url"],
                config,
                schedule,
                args.get("source_tier") or "vendor-doc",
                1 if args.get("paused") else 0,
            ),
        )
        conn.commit()
        return {"id": args["id"], "next_poll_at": None}

    def list_(args: dict[str, Any]) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources WHERE 1=1"
        params: list[Any] = []
        if args.get("paused_only"):
            sql += " AND paused = 1"
        if args.get("with_errors"):
            sql += " AND error_count > 0"
        sql += " ORDER BY id"
        return [_row_to_dict(r) for r in conn.execute(sql, params)]

    def get_(args: dict[str, Any]) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (args["id"],)).fetchone()
        if not row:
            return {"error": "not found"}
        d = _row_to_dict(row)
        if d.get("cursor") and isinstance(d["cursor"], str) and len(d["cursor"]) > 2048:
            d["cursor"] = d["cursor"][:2048] + "...[truncated]"
        return d

    def remove(args: dict[str, Any]) -> dict[str, Any]:
        cur = conn.execute("DELETE FROM sources WHERE id = ?", (args["id"],))
        conn.commit()
        return {"removed": cur.rowcount > 0}

    def fetch_now(args: dict[str, Any]) -> dict[str, Any]:
        cur = conn.execute(
            "UPDATE sources SET next_poll_at = NULL, updated_at = ? WHERE id = ?",
            (_utcnow_iso(), args["id"]),
        )
        conn.commit()
        return {"triggered": cur.rowcount > 0, "id": args["id"]}

    def set_(args: dict[str, Any]) -> dict[str, Any]:
        fields = []
        params: list[Any] = []
        updated: list[str] = []
        if "paused" in args:
            fields.append("paused = ?")
            params.append(1 if args["paused"] else 0)
            updated.append("paused")
        if "schedule" in args:
            fields.append("schedule = ?")
            params.append(args["schedule"])
            updated.append("schedule")
        if "config" in args:
            fields.append("config = ?")
            params.append(json.dumps(args["config"]))
            updated.append("config")
        if "source_tier" in args:
            fields.append("source_tier = ?")
            params.append(args["source_tier"])
            updated.append("source_tier")
        if not fields:
            return {"updated_fields": []}
        fields.append("updated_at = ?")
        params.append(_utcnow_iso())
        params.append(args["id"])
        conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        return {"updated_fields": updated}

    return {
        "sources.add": {
            "description": "Register a new polled source.",
            "input_schema": {
                "type": "object",
                "required": ["id", "name", "adapter", "url"],
                "properties": {
                    "id":          {"type": "string"},
                    "name":        {"type": "string"},
                    "adapter":     {"type": "string", "enum": ["sitemap", "github-repo"]},
                    "url":         {"type": "string"},
                    "config":      {"type": "object"},
                    "schedule":    {"type": "string"},
                    "source_tier": {"type": "string"},
                    "paused":      {"type": "boolean"},
                },
            },
            "handler": add,
        },
        "sources.list": {
            "description": "List configured sources.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "paused_only": {"type": "boolean"},
                    "with_errors": {"type": "boolean"},
                },
            },
            "handler": list_,
        },
        "sources.get": {
            "description": "Get one source's full record.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "handler": get_,
        },
        "sources.remove": {
            "description": "Delete a source. Does not tombstone its content.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "handler": remove,
        },
        "sources.fetch_now": {
            "description": "Force immediate poll on next daemon tick.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "handler": fetch_now,
        },
        "sources.set": {
            "description": "Update one or more fields on an existing source.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {
                    "id":          {"type": "string"},
                    "paused":      {"type": "boolean"},
                    "schedule":    {"type": "string"},
                    "config":      {"type": "object"},
                    "source_tier": {"type": "string"},
                },
            },
            "handler": set_,
        },
    }
