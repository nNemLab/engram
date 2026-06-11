"""kb.* tools: write, get, list, contradiction surface."""
from __future__ import annotations

import sqlite3
from typing import Any

from ... import dedup
from ... import log as event_log


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:

    def write(args: dict[str, Any]) -> dict[str, Any]:
        result = dedup.gate(
            conn,
            body=args["body"],
            title=args.get("title"),
            source_url=args.get("source_url"),
            source_tier=args.get("source_tier", "agent-derived"),
            confidence=float(args.get("confidence", 0.5)),
            ttl_days=args.get("ttl_days"),
            kind=args.get("kind", "kb"),
            actor=args.get("actor", "agent"),
            correlation_id=args.get("correlation_id"),
        )
        return {"outcome": result.outcome, "hash": result.hash, "merged_into": result.merged_into}

    def get(args: dict[str, Any]) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT hash, title, body, source_url, source_tier, fetched_at, confidence, "
            "staleness_score, kind, vault_path FROM content WHERE hash = ? AND tombstoned = 0",
            (args["hash"],),
        ).fetchone()
        return dict(row) if row else None

    def list_(args: dict[str, Any]) -> list[dict[str, Any]]:
        kind = args.get("kind")
        limit = int(args.get("limit", 50))
        if kind:
            rows = conn.execute(
                "SELECT hash, title, kind, source_url, fetched_at FROM content "
                "WHERE tombstoned = 0 AND kind = ? ORDER BY updated_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT hash, title, kind, source_url, fetched_at FROM content "
                "WHERE tombstoned = 0 ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def contradictions(args: dict[str, Any]) -> list[dict[str, Any]]:
        only_unresolved = bool(args.get("unresolved", True))
        sql = ("SELECT id, hash_a, hash_b, detected_at, detected_by, resolved, resolution "
               "FROM contradictions")
        if only_unresolved:
            sql += " WHERE resolved = 0"
        sql += " ORDER BY detected_at DESC LIMIT 100"
        return [dict(r) for r in conn.execute(sql).fetchall()]

    def tombstone(args: dict[str, Any]) -> dict[str, Any]:
        """Permanently retire content from the live KB. Idempotent.

        Sets tombstoned=1, drops the embedding row, emits a `merged` event with
        hash_kept=null. Projector picks up the merged event and deletes the
        rendered vault file. Content body stays in the DB for audit; the row is
        excluded from rag.query, kb.list, kb.get."""
        if "hash" in args:
            hashes = [args["hash"]]
        elif "hashes" in args:
            hashes = list(args["hashes"])
        else:
            return {"error": "must supply 'hash' or 'hashes'"}
        if not hashes:
            return {"error": "no hashes provided"}

        reason = args.get("reason", "manual")
        note = args.get("note")
        actor = args.get("actor", "agent")

        results: list[dict[str, Any]] = []
        for h in hashes:
            row = conn.execute(
                "SELECT title FROM content WHERE hash = ? AND tombstoned = 0",
                (h,),
            ).fetchone()
            if not row:
                results.append({"hash": h, "outcome": "not_live"})
                continue
            conn.execute("UPDATE content SET tombstoned = 1 WHERE hash = ?", (h,))
            conn.execute("DELETE FROM embeddings WHERE content_hash = ?", (h,))
            payload = {"hash_tombstoned": h, "hash_kept": None, "reason": reason}
            if note:
                payload["note"] = note
            event_log.append(conn, "merged", payload, actor=actor)
            results.append({"hash": h, "outcome": "tombstoned", "title": row["title"]})
        return {"results": results,
                "tombstoned_count": sum(1 for r in results if r["outcome"] == "tombstoned")}

    def resolve_supersede(args: dict[str, Any]) -> dict[str, Any]:
        """Act on a blocked-supersede contradiction against a protected row (#54)."""
        return dedup.resolve_supersede(
            conn,
            args["hash"],
            args.get("choice"),
            tombstone_upstream=bool(args.get("tombstone_upstream", True)),
            actor=args.get("actor", "human"),
        )

    def flag_contradiction(args: dict[str, Any]) -> dict[str, Any]:
        cur = conn.execute(
            "INSERT INTO contradictions (hash_a, hash_b, detected_by) VALUES (?, ?, ?)",
            (args["hash_a"], args["hash_b"], args.get("detected_by", "agent")),
        )
        cid = cur.lastrowid
        event_log.append(
            conn, "contradicted",
            {"hash_a": args["hash_a"], "hash_b": args["hash_b"], "id": cid},
            actor="agent",
        )
        return {"id": cid}

    return {
        "kb.write": {
            "description": "Write content through the dedup gate. Returns outcome (new|exact_dup|near_dup) and hash.",
            "input_schema": {
                "type": "object",
                "required": ["body"],
                "properties": {
                    "body":        {"type": "string"},
                    "title":       {"type": "string"},
                    "source_url":  {"type": "string"},
                    "source_tier": {"type": "string",
                                    "enum": ["peer-reviewed", "vendor-doc", "agent-derived", "blog", "forum", "manual"]},
                    "confidence":  {"type": "number", "minimum": 0, "maximum": 1},
                    "ttl_days":    {"type": "integer"},
                    "kind":        {"type": "string",
                                    "enum": ["kb", "episode", "entity", "research", "playbook-summary"]},
                    "correlation_id": {"type": "string"},
                },
            },
            "handler": write,
        },
        "kb.get": {
            "description": "Fetch a content entry by hash.",
            "input_schema": {
                "type": "object", "required": ["hash"],
                "properties": {"hash": {"type": "string"}},
            },
            "handler": get,
        },
        "kb.list": {
            "description": "List recent content entries, optionally filtered by kind.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "kind":  {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
            "handler": list_,
        },
        "kb.contradictions": {
            "description": "List contradiction records (default: unresolved only).",
            "input_schema": {
                "type": "object",
                "properties": {"unresolved": {"type": "boolean", "default": True}},
            },
            "handler": contradictions,
        },
        "kb.tombstone": {
            "description": "Permanently retire one or more KB entries (single 'hash' or 'hashes' list). "
                           "Sets tombstoned=1, drops embeddings, emits a merged event with hash_kept=null; "
                           "projector then deletes the vault file. Idempotent — already-tombstoned hashes "
                           "return outcome='not_live'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "hash":   {"type": "string"},
                    "hashes": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string",
                               "description": "Short tag, e.g. 'manual_purge', 'off_topic', 'superseded'."},
                    "note":   {"type": "string",
                               "description": "Optional free-form context for the audit log."},
                    "actor":  {"type": "string", "default": "agent"},
                },
            },
            "handler": tombstone,
        },
        "kb.resolve_supersede": {
            "description": "Resolve a blocked-supersede contradiction on a human-edited "
                           "(protected) sourced row. 'accept_upstream' promotes the pending "
                           "upstream revision to current, re-projects the vault file, clears "
                           "protection, and marks the contradiction resolved (kept_b). "
                           "'keep_mine' keeps the human version, marks it resolved (kept_a), and "
                           "by default tombstones the rejected upstream revision. Pass the "
                           "protected row's hash (the contradiction's hash_a).",
            "input_schema": {
                "type": "object", "required": ["hash", "choice"],
                "properties": {
                    "hash":   {"type": "string",
                               "description": "Hash of the protected (human) row — hash_a of the contradiction."},
                    "choice": {"type": "string", "enum": ["accept_upstream", "keep_mine"]},
                    "tombstone_upstream": {"type": "boolean", "default": True,
                                           "description": "keep_mine only: tombstone the rejected upstream revision."},
                    "actor":  {"type": "string", "default": "human"},
                },
            },
            "handler": resolve_supersede,
        },
        "kb.flag_contradiction": {
            "description": "Flag two content hashes as contradicting; emits contradicted event.",
            "input_schema": {
                "type": "object", "required": ["hash_a", "hash_b"],
                "properties": {
                    "hash_a": {"type": "string"},
                    "hash_b": {"type": "string"},
                    "detected_by": {"type": "string"},
                },
            },
            "handler": flag_contradiction,
        },
    }
