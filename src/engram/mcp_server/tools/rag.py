"""rag.* tools: hybrid retrieval over the content store."""
from __future__ import annotations

import sqlite3
from typing import Any

from ...rag.query import hybrid_search


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:

    def query(args: dict[str, Any]) -> list[dict[str, Any]]:
        hits = hybrid_search(
            conn, args["query"],
            top_k=args.get("top_k"),
            exclude_source_tiers=args.get("exclude_source_tiers"),
            exclude_kinds=args.get("exclude_kinds"),
        )
        return [
            {
                "hash": h.hash,
                "title": h.title,
                "score": round(h.score, 4),
                "source_url": h.source_url,
                "confidence": h.confidence,
                "fetched_at": h.fetched_at,
                "snippet": (h.body[:400] + "…") if len(h.body) > 400 else h.body,
            }
            for h in hits
        ]

    return {
        "rag.query": {
            "description": "Hybrid retrieval (dense + BM25, RRF-fused, confidence-ranked). "
                           "Use exclude_source_tiers=['agent-derived'] when synthesizing to "
                           "avoid the synthesis-eats-synthesis loop.",
            "input_schema": {
                "type": "object", "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 12},
                    "exclude_source_tiers": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Tiers to filter out (e.g. ['agent-derived']).",
                    },
                    "exclude_kinds": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Content kinds to filter out (e.g. ['playbook-summary']).",
                    },
                },
            },
            "handler": query,
        },
    }
