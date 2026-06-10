"""URLs adapter: fetch a manually curated list of URLs.

Use when a source has no sitemap and no API — operator picks the N pages
that matter. Same per-URL ETag / Last-Modified caching as sitemap.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import trafilatura

from . import Candidate, register
from ._http import AsyncRateLimiter, HTTPCacheEntry, fetch_with_politeness

logger = logging.getLogger("engram.poller.urls")


def _read_cache(cursor: dict) -> dict[str, HTTPCacheEntry]:
    cache: dict[str, HTTPCacheEntry] = {}
    for url, entry in (cursor.get("cache") or {}).items():
        cache[url] = HTTPCacheEntry(
            etag=entry.get("etag"),
            last_modified=entry.get("last_modified"),
        )
    return cache


class UrlsAdapter:
    name = "urls"

    def __init__(self, *, _client: httpx.AsyncClient | None = None,
                 user_agent: str = "engram/0.3 (+source-poller)") -> None:
        self._client = _client or httpx.AsyncClient(
            headers={"user-agent": user_agent}, timeout=30.0,
        )

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        cfg = json.loads(source.get("config") or "{}")
        urls: list[str] = cfg.get("urls", [])
        cursor = json.loads(source.get("cursor") or "{}")

        interval_ms = cfg.get("request_interval_ms", 1000)
        rate_limiter = AsyncRateLimiter(interval_ms=interval_ms)

        cache = _read_cache(cursor)
        new_cache: dict[str, HTTPCacheEntry] = {}

        for url in urls:
            entry = cache.get(url)
            try:
                result = await fetch_with_politeness(
                    self._client, url, cache=entry, rate_limiter=rate_limiter,
                )
            except httpx.HTTPStatusError as exc:
                logger.warning("urls adapter: %s returned %s; skipping",
                               url, exc.response.status_code)
                # Preserve previous cache entry so we retry conditionally next poll.
                if entry is not None:
                    new_cache[url] = entry
                continue

            if result is None:
                if entry is not None:
                    new_cache[url] = entry
                continue

            extracted = trafilatura.extract(result.body, include_comments=False) or result.body
            title_meta = trafilatura.extract_metadata(result.body)
            title_str = title_meta.title if title_meta and title_meta.title else None

            yield Candidate(
                source_url=url,
                body=extracted,
                title=title_str,
                fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                metadata={"etag": result.etag, "content_type": result.content_type},
            )
            new_cache[url] = HTTPCacheEntry(etag=result.etag, last_modified=result.last_modified)

        source["cursor"] = json.dumps({
            "cache": {
                url: {"etag": entry.etag, "last_modified": entry.last_modified}
                for url, entry in new_cache.items()
            },
        })


register(UrlsAdapter())
