"""rag.* tools: hybrid retrieval over the content store."""
from __future__ import annotations

import sqlite3
from typing import Any


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:

    def cite(args: dict[str, Any]) -> dict[str, Any]:
        from ...rag.usage import record_cited
        hashes = args["hashes"]
        return {"cited": record_cited(conn, hashes, query=args.get("query", ""), turn_id=args.get("turn_id"))}

    def query(args: dict[str, Any]) -> dict[str, Any]:
        from ...common.config import load_config
        from ...rag.grounding import classify
        from ...rag.query import hybrid_search
        hits = hybrid_search(
            conn, args["query"],
            top_k=args.get("k") or args.get("top_k"),
            since=args.get("since"),
            level=args.get("level", "snippet"),
            exclude_source_tiers=args.get("exclude_source_tiers"),
            exclude_kinds=args.get("exclude_kinds"),
        )
        verdict = classify(hits, load_config().grounding)
        results = [
            {"hash": h.hash, "title": h.title, "score": round(h.score, 4),
             "source_url": h.source_url, "snippet": h.body}
            for h in hits
        ]
        budget = args.get("token_budget")
        if budget:
            kept, used = [], 0
            for r in results:
                cost = (len((r["title"] or "") + (r["snippet"] or "")) + 3) // 4
                if kept and used + cost > budget:
                    break
                kept.append(r)
                used += cost
            results = kept
        return {
            "verdict": verdict,
            "results": results,
        }

    return {
        "rag.cite": {
            "description": "Record that the answer was grounded in these content hashes "
                           "(feeds usage-weighted ranking). Call when you use retrieved memory.",
            "input_schema": {
                "type": "object", "required": ["hashes"],
                "properties": {
                    "hashes":  {"type": "array", "items": {"type": "string"}},
                    "query":   {"type": "string"},
                    "turn_id": {"type": "string"},
                },
            },
            "handler": cite,
        },
        "rag.query": {
            "description": "Hybrid retrieval (dense + BM25, RRF-fused, confidence-ranked). "
                           "Returns a calibration verdict (STRONG/WEAK/NONE) alongside results. "
                           "Use exclude_source_tiers=['agent-derived'] when synthesizing to "
                           "avoid the synthesis-eats-synthesis loop.",
            "input_schema": {
                "type": "object", "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 12,
                          "description": "Maximum number of hits to return."},
                    "token_budget": {
                        "type": "integer",
                        "description": "Soft cap on total tokens across all snippets (advisory).",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["snippet", "section", "full"],
                        "default": "snippet",
                        "description": "Body truncation level: snippet (~320 chars), "
                                       "section or full (untruncated).",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 datetime; exclude content fetched before this.",
                    },
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
