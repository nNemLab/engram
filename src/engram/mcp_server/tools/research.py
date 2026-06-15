"""research.* tools — outbound search + ingest, gated through dedup.

Tools:
  - research.fetch_url    — agent supplies a body; gate it as kind=research
  - research.search_web   — SearXNG → fetch → extract → cross-encoder rerank → top-k
  - research.fetch_arxiv  — arXiv API search + rerank by abstract
  - research.ingest_url   — convenience: fetch URL ourselves (httpx + trafilatura) → gate
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from ... import dedup
from ...common.config import load_config


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:

    def fetch_url(args: dict[str, Any]) -> dict[str, Any]:
        body = args["body"]
        url = args["url"]
        result = dedup.gate(
            conn,
            body=body,
            title=args.get("title"),
            source_url=url,
            source_tier=args.get("source_tier", "blog"),
            confidence=float(args.get("confidence", 0.5)),
            ttl_days=args.get("ttl_days", 180),
            kind="research",
            actor="agent",
        )
        if result.outcome == "new":
            conn.execute(
                "UPDATE content SET fetched_at = ? WHERE hash = ?",
                (_now_iso(), result.hash),
            )
        return {"outcome": result.outcome, "hash": result.hash}

    def ingest_url(args: dict[str, Any]) -> dict[str, Any]:
        """Fetch + extract on the server side, then gate. Saves a roundtrip
        when the agent already knows the URL it wants."""
        import trafilatura

        from ...research import safe_fetch
        url = args["url"]
        try:
            r = safe_fetch.get(url, timeout=25,
                               headers={"User-Agent": "engram-research/0.1"})
        except safe_fetch.UnsafeURLError as e:
            return {"error": str(e)}
        r.raise_for_status()

        # Guard: only HTML/text responses reach trafilatura.
        ct = r.headers.get("content-type", "").lower()
        body_bytes = r.content
        if "text" not in ct:
            return {
                "error": f"unsupported content-type {ct!r} for {url} "
                         "(expected HTML/text — use a text-based URL or "
                         "provide body directly via research.fetch_url)",
            }
        # Sanity check: refuse obvious binary blobs that slip through
        # (e.g. a content-disposition response returning raw PDF bytes
        # with a misleading content-type).
        if body_bytes[:4] == b"%PDF":
            return {
                "error": f"refusing PDF response for {url} "
                         "(PDF extraction is not supported by ingest_url — "
                         "use a text-based URL or provide body via "
                         "research.fetch_url)",
            }

        body = trafilatura.extract(
            r.text, include_comments=False, include_tables=True,
        )
        if not body:
            return {"error": f"trafilatura returned no content for {url}"}
        meta = trafilatura.extract_metadata(r.text)
        title = args.get("title") or (meta.title if meta else None) or url
        result = dedup.gate(
            conn, body=body, title=title, source_url=url,
            source_tier=args.get("source_tier", "blog"),
            confidence=float(args.get("confidence", 0.5)),
            ttl_days=args.get("ttl_days", 180),
            kind="research", actor="agent",
        )
        if result.outcome == "new":
            conn.execute(
                "UPDATE content SET fetched_at = ? WHERE hash = ?",
                (_now_iso(), result.hash),
            )
        return {"outcome": result.outcome, "hash": result.hash, "title": title,
                "extracted_chars": len(body)}

    def search_web(args: dict[str, Any]) -> dict[str, Any]:
        from ...research import web
        cfg = load_config()
        k = int(args.get("k", cfg.research.web_default_k))
        max_cand = int(args.get("max_candidates", cfg.research.web_max_candidates))
        try:
            hits = web.search(args["query"], k=k, max_candidates=max_cand)
        except RuntimeError as e:
            return {"error": str(e)}
        return {
            "results": [
                {
                    "url": h.url,
                    "title": h.title,
                    "score": round(h.score, 3),
                    "snippet": h.snippet,
                    "body_chars": len(h.body),
                    "engines": h.engines,
                }
                for h in hits
            ],
        }

    def search_arxiv(args: dict[str, Any]) -> dict[str, Any]:
        from ...research import arxiv as arxiv_mod
        cfg = load_config()
        if not cfg.research.arxiv_enabled:
            return {"error": "arxiv disabled in config"}
        k = int(args.get("k", cfg.research.arxiv_default_k))
        do_rerank = bool(args.get("rerank", True))
        quote_phrase = bool(args.get("quote_phrase", True))
        results = arxiv_mod.search(
            args["query"], k=k, do_rerank=do_rerank, quote_phrase=quote_phrase,
        )
        return {
            "results": [
                {
                    "arxiv_id": r.arxiv_id,
                    "title": r.title,
                    "abstract": r.abstract,
                    "authors": r.authors,
                    "published": r.published,
                    "pdf_url": r.pdf_url,
                    "abs_url": r.abs_url,
                    "score": round(r.score, 3),
                }
                for r in results
            ],
        }

    return {
        "research.fetch_url": {
            "description": "Ingest fetched web content (caller supplies body) through the dedup gate.",
            "input_schema": {
                "type": "object", "required": ["url", "body"],
                "properties": {
                    "url":   {"type": "string"},
                    "body":  {"type": "string"},
                    "title": {"type": "string"},
                    "source_tier": {"type": "string"},
                    "confidence":  {"type": "number"},
                    "ttl_days":    {"type": "integer"},
                },
            },
            "handler": fetch_url,
        },
        "research.ingest_url": {
            "description": "Server-side fetch + trafilatura extract + gate. One-shot URL ingest. "
                           "Only accepts HTML/text content-types; rejects PDFs and other binary "
                           "responses with a structured error.",
            "input_schema": {
                "type": "object", "required": ["url"],
                "properties": {
                    "url":   {"type": "string"},
                    "title": {"type": "string"},
                    "source_tier": {"type": "string"},
                    "confidence":  {"type": "number"},
                    "ttl_days":    {"type": "integer"},
                },
            },
            "handler": ingest_url,
        },
        "research.search_web": {
            "description": "Self-hosted web search: SearXNG → fetch → extract → cross-encoder rerank → top-k. "
                           "Returns ranked URLs with extracted body length; pass URLs to research.ingest_url to store.",
            "input_schema": {
                "type": "object", "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 8},
                    "max_candidates": {"type": "integer", "default": 20},
                },
            },
            "handler": search_web,
        },
        "research.fetch_arxiv": {
            "description": "Search arXiv (titles + abstracts), reranked by cross-encoder. "
                           "Multi-word queries are auto-quoted (exact phrase) by default — set "
                           "quote_phrase=false for broad keyword OR-search. "
                           "Returns abstracts only. For full papers, download the PDF separately "
                           "and use research.fetch_url with the extracted text.",
            "input_schema": {
                "type": "object", "required": ["query"],
                "properties": {
                    "query":        {"type": "string"},
                    "k":            {"type": "integer", "default": 5},
                    "rerank":       {"type": "boolean", "default": True},
                    "quote_phrase": {"type": "boolean", "default": True},
                },
            },
            "handler": search_arxiv,
        },
    }
