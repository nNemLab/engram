"""MCP tools: sources.* namespace for CRUD on the sources registry."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ...common.time import utcnow_iso
from ...sources.health import source_health

DEFAULT_SCHEDULE = {
    "sitemap":       "7d",
    "github-repo":   "1d",
    "mediawiki-api": "7d",
    "urls":          "7d",
}


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
        try:
            cur = conn.execute("DELETE FROM sources WHERE id = ?", (args["id"],))
            conn.commit()
            return {"removed": cur.rowcount > 0}
        except sqlite3.IntegrityError:
            return {
                "error": "source has content; tombstone content first or set source_id to NULL",
                "id": args["id"]
            }

    def fetch_now(args: dict[str, Any]) -> dict[str, Any]:
        cur = conn.execute(
            "UPDATE sources SET next_poll_at = NULL, updated_at = ? WHERE id = ?",
            (utcnow_iso("s"), args["id"]),
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
            # Merge provided config into existing config rather than replacing it
            cur = conn.execute(
                "SELECT config FROM sources WHERE id = ?", (args["id"],)
            ).fetchone()
            if cur is None:
                return {"error": "not found", "id": args["id"]}
            existing_cfg = json.loads(cur[0]) if cur[0] else {}
            merged_cfg = {**existing_cfg, **args["config"]}
            fields.append("config = ?")
            params.append(json.dumps(merged_cfg))
            updated.append("config")
        if "source_tier" in args:
            fields.append("source_tier = ?")
            params.append(args["source_tier"])
            updated.append("source_tier")
        if not fields:
            return {"updated_fields": []}
        fields.append("updated_at = ?")
        params.append(utcnow_iso("s"))
        params.append(args["id"])
        cur = conn.execute(
            f"UPDATE sources SET {', '.join(fields)} WHERE id = ?", params
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"error": "not found", "id": args["id"]}
        return {"updated_fields": updated}

    def health(args: dict[str, Any]) -> dict[str, Any]:
        records = source_health(conn)
        sid = args.get("id")
        if sid:
            records = [r for r in records if r["id"] == sid]
        return {"sources": records}

    return {
        "sources.add": {
            "description": "Register a new polled source.",
            "input_schema": {
                "type": "object",
                "required": ["id", "name", "adapter", "url"],
                "properties": {
                    "id":          {"type": "string"},
                    "name":        {"type": "string"},
                    "adapter":     {"type": "string", "enum": ["sitemap", "github-repo", "mediawiki-api", "urls"]},
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
        "sources.health": {
            "description": "Deterministic per-source health view (liveness, "
                           "content counts, dup ratio, derived status). Read-only.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Optional: filter to one source."},
                },
            },
            "handler": health,
        },
    }
