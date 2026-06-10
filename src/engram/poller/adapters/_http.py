"""Shared HTTP helper for adapters.

Provides:
  - AsyncRateLimiter: per-source token bucket, spaces requests by interval_ms.
  - HTTPCacheEntry: per-URL ETag + Last-Modified cache.
  - FetchResult: structured return shape.
  - fetch_with_politeness: one HTTP call with conditional headers, rate-limit
    acquisition, and Retry-After / 5xx backoff with one retry.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx


@dataclass
class HTTPCacheEntry:
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class FetchResult:
    body: str
    etag: str | None
    last_modified: str | None
    content_type: str | None


class AsyncRateLimiter:
    """Spaces successive acquire() calls by at least interval_ms milliseconds.

    A single _next_allowed_at timestamp; acquire() sleeps until that time, then
    advances it by interval_ms. Concurrent acquires serialize via the lock.
    """

    def __init__(self, interval_ms: int) -> None:
        self._interval = interval_ms / 1000.0
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed_at = now + self._interval


def _parse_retry_after(value: str) -> float | None:
    """Return seconds until retry. Accepts integer-seconds form or HTTP-date."""
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    delta = when.timestamp() - time.time()
    return max(0.0, delta)


async def fetch_with_politeness(
    client: httpx.AsyncClient,
    url: str,
    *,
    cache: HTTPCacheEntry | None = None,
    extra_params: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    rate_limiter: AsyncRateLimiter,
) -> FetchResult | None:
    """Fetch one URL with conditional headers, rate-limit, and bounded retries.

    Returns FetchResult on 200; None on 304.
    Raises httpx.HTTPStatusError on persistent 4xx and 5xx-after-retry.
    """
    headers: dict[str, str] = {}
    if cache:
        if cache.etag:
            headers["if-none-match"] = cache.etag
        if cache.last_modified:
            headers["if-modified-since"] = cache.last_modified
    if extra_headers:
        headers.update(extra_headers)

    async def _do_request() -> httpx.Response:
        await rate_limiter.acquire()
        return await client.get(url, headers=headers, params=extra_params)

    resp = await _do_request()
    retried = False

    # Retry-After path: 429 or 503 (or any 5xx) with the Retry-After header.
    if resp.status_code in (429, 503) and resp.headers.get("retry-after") is not None:
        wait = _parse_retry_after(resp.headers["retry-after"])
        if wait is not None:
            await asyncio.sleep(wait)
            resp = await _do_request()
            retried = True

    # Bare 5xx without Retry-After: backoff and retry once.
    if not retried and resp.status_code >= 500:
        backoff = min(5.0, 2.0 * rate_limiter._interval)
        await asyncio.sleep(backoff)
        resp = await _do_request()

    if resp.status_code == 304:
        return None

    resp.raise_for_status()

    return FetchResult(
        body=resp.text,
        etag=resp.headers.get("etag"),
        last_modified=resp.headers.get("last-modified"),
        content_type=resp.headers.get("content-type"),
    )
