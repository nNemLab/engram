"""Sitemap adapter: walks a site's sitemap.xml, optionally honoring sitemap-index
files, applies URL globs, fetches matching pages with ETag-based 304 handling,
extracts text via trafilatura, yields Candidates."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator
from xml.etree import ElementTree as ET

import httpx
import trafilatura

from . import Adapter, Candidate, matches_globs, register
from ._http import AsyncRateLimiter, FetchResult, HTTPCacheEntry, fetch_with_politeness

logger = logging.getLogger("engram.poller.sitemap")
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _read_cache(cursor: dict) -> dict[str, HTTPCacheEntry]:
    """Read cache from cursor in either v0.2 (etags) or v0.3 (cache) shape."""
    cache: dict[str, HTTPCacheEntry] = {}
    if "cache" in cursor:
        for url, entry in cursor["cache"].items():
            cache[url] = HTTPCacheEntry(
                etag=entry.get("etag"),
                last_modified=entry.get("last_modified"),
            )
    elif "etags" in cursor:
        # v0.2 legacy
        for url, etag in cursor["etags"].items():
            cache[url] = HTTPCacheEntry(etag=etag)
    return cache


class SitemapAdapter:
    name = "sitemap"

    def __init__(self, *, _client: httpx.AsyncClient | None = None,
                 user_agent: str = "engram/0.1 (+source-poller)") -> None:
        self._client = _client or httpx.AsyncClient(
            headers={"user-agent": user_agent}, timeout=30.0,
        )

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        cfg = json.loads(source.get("config") or "{}")
        include = cfg.get("include", [])
        exclude = cfg.get("exclude", [])
        cursor = json.loads(source.get("cursor") or "{}")

        interval_ms = cfg.get("request_interval_ms", 1000)
        self._rate_limiter = AsyncRateLimiter(interval_ms=interval_ms)

        cache = _read_cache(cursor)

        urls = await self._collect_urls(source["url"])
        new_cache: dict[str, HTTPCacheEntry] = {}
        for u in urls:
            if not matches_globs(u, include, exclude):
                continue
            entry = cache.get(u)
            result = await fetch_with_politeness(
                self._client, u, cache=entry, rate_limiter=self._rate_limiter
            )
            if result is None:
                # 304 — preserve existing cache entry
                if entry is not None:
                    new_cache[u] = entry
                continue
            # Extract text
            body_html = result.body
            extracted = trafilatura.extract(body_html, include_comments=False) or body_html
            title = trafilatura.extract_metadata(body_html)
            title_str = title.title if title and title.title else None
            cand = Candidate(
                source_url=u,
                body=extracted,
                title=title_str,
                fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                metadata={"etag": result.etag, "content_type": result.content_type},
            )
            new_cache[u] = HTTPCacheEntry(etag=result.etag, last_modified=result.last_modified)
            yield cand

        source["cursor"] = json.dumps({
            "cache": {
                url: {"etag": entry.etag, "last_modified": entry.last_modified}
                for url, entry in new_cache.items()
            },
            "last_seen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    async def _collect_urls(self, sitemap_url: str, _depth: int = 0) -> list[str]:
        """Fetch sitemap.xml; if it's a sitemap-index, descend one level.

        Bounded to depth 1 (top-level index → leaf sitemaps). A sitemap-index
        nested inside another sitemap-index is rejected to avoid runaway
        crawls on malformed or adversarial sitemaps.
        """
        if _depth > 1:
            logger.warning("sitemap recursion depth exceeded at %s; skipping", sitemap_url)
            return []
        out: list[str] = []
        resp = await self._client.get(sitemap_url)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        if root.tag.endswith("sitemapindex"):
            for sm in root.findall("sm:sitemap/sm:loc", NS):
                if sm.text:
                    out.extend(await self._collect_urls(sm.text.strip(), _depth + 1))
        else:
            for loc in root.findall("sm:url/sm:loc", NS):
                if loc.text:
                    out.append(loc.text.strip())
        return out


register(SitemapAdapter())
