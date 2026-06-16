"""Self-hosted web search: SearXNG → fetch → extract → rerank → top-k.

Replaces hosted search/extraction services. The reranker layer is what makes the
output usable as LLM context — raw SearXNG results are SERP-rank order, not
relevance-ranked for the specific query.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
import trafilatura

from ..common.config import load_config
from . import rerank, safe_fetch


@dataclass
class WebResult:
    url: str
    title: str
    snippet: str          # SearXNG-provided
    body: str             # extracted full text (may be empty if extraction failed)
    score: float          # cross-encoder relevance score
    engines: list[str]    # which SearXNG engines returned this URL


logger = logging.getLogger("engram.research.web")

_DEFAULT_TIMEOUT = 25.0
_FETCH_TIMEOUT = 12.0
_MAX_FETCH_CONCURRENCY = 6
_USER_AGENT = "engram-research/0.1 (+self-hosted)"
_ALLOWED_MEDIA_TYPES = {"text/html", "text/plain"}


async def _searxng_query(client: httpx.AsyncClient, base_url: str, q: str,
                         max_candidates: int) -> list[dict]:
    r = await client.get(
        f"{base_url.rstrip('/')}/search",
        params={"q": q, "format": "json", "safesearch": "0"},
        timeout=_DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    results = data.get("results", [])
    if not isinstance(results, list):
        return []
    return results[:max_candidates]


async def _fetch_one(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await safe_fetch.get_async(client, url, timeout=_FETCH_TIMEOUT,
                                       headers={"User-Agent": _USER_AGENT})
        r.raise_for_status()
        media_type = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if r.status_code == 200 and media_type in _ALLOWED_MEDIA_TYPES:
            return r.text
    except Exception as exc:
        logger.warning("fetch failed; returning empty body", extra={"url": url, "cause": str(exc)},
                       exc_info=True)
        return ""
    logger.warning("fetch yielded unsupported response; returning empty body",
                   extra={"url": url})
    return ""


def _extract(html: str, *, url: str | None = None) -> str:
    if not html:
        return ""
    try:
        return trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception as exc:
        logger.warning("extract failed; returning empty body",
                       extra={"url": url, "cause": str(exc)}, exc_info=True)
        return ""


async def _search_async(query: str, k: int, max_candidates: int) -> list[WebResult]:
    cfg = load_config()
    if not cfg.research.searxng_url:
        raise RuntimeError(
            "research.searxng_url is not set. Configure it in ~/.engram/config.yml "
            "or bring up the local SearXNG (research/searxng/docker-compose.yml)."
        )

    async with httpx.AsyncClient() as client:
        raw = await _searxng_query(client, cfg.research.searxng_url, query, max_candidates)
        if not raw:
            return []

        # Parallel fetch of every candidate body with a bounded fan-out.
        semaphore = asyncio.Semaphore(_MAX_FETCH_CONCURRENCY)

        async def _fetch_bounded(url: str) -> str:
            async with semaphore:
                return await _fetch_one(client, url)

        usable_raw = [r for r in raw if isinstance(r, dict) and r.get("url")]
        seen_urls: set[str] = set()
        deduped_raw: list[dict] = []
        for r in usable_raw:
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            deduped_raw.append(r)

        bodies = await asyncio.gather(*[_fetch_bounded(r["url"]) for r in deduped_raw])

    extracted = [_extract(b, url=r["url"]) for r, b in zip(deduped_raw, bodies)]

    # Build candidate WebResult list. Use snippet as fallback when extraction
    # produced nothing — better to rerank a snippet than to drop the entry.
    candidates: list[WebResult] = []
    for r, body in zip(deduped_raw, extracted):
        text_for_rerank = body if body else (r.get("content") or "")
        if not text_for_rerank.strip():
            continue
        candidates.append(WebResult(
            url=r["url"],
            title=r.get("title") or r["url"],
            snippet=(r.get("content") or "")[:400],
            body=body,
            score=0.0,
            engines=r.get("engines") or [],
        ))

    if not candidates:
        return []

    scores = rerank.score(query, [c.body or c.snippet for c in candidates])
    for c, s in zip(candidates, scores):
        c.score = s

    candidates.sort(key=lambda r: r.score, reverse=True)
    return candidates[:k]


def search(query: str, *, k: int = 8, max_candidates: int = 20) -> list[WebResult]:
    """Synchronous entry point. Spins a fresh event loop for each call —
    fine for MCP tool handlers that already run in a worker thread."""
    return asyncio.run(_search_async(query, k=k, max_candidates=max_candidates))
