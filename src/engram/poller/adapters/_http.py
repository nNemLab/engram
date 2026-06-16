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

# Upper bound on how long we'll honor a server-supplied Retry-After. A hostile or
# misconfigured endpoint can send `Retry-After: 86400` (a full day); without a cap
# a single source would park its coroutine for that entire time. Mirrors the
# min() cap on the bare-5xx backoff path in fetch_with_politeness.
MAX_RETRY_AFTER_SECONDS = 300.0

# Fallback backoff for a bare 429 (Too Many Requests) that carries no Retry-After
# and no X-RateLimit-* hint. Without this a bare 429 falls through to
# raise_for_status and trips the source's circuit breaker instead of being
# retried like the throttle it is.
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 5.0


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


def _rate_limit_delay(resp: httpx.Response) -> float | None:
    """Seconds to wait before retrying a throttled response, or None if the
    response is not a retryable throttle.

    Honored signals, in priority order:
      * ``Retry-After`` on a 429 or 503 -- an explicit server-directed backoff.
      * ``X-RateLimit-Remaining: 0`` + ``X-RateLimit-Reset`` on a 403 or 429 --
        a GitHub-style primary rate limit; wait until the reset epoch.
      * A bare 429 with no actionable header -- a fixed default backoff so the
        request is retried instead of tripping the circuit breaker.

    Every returned delay is clamped to MAX_RETRY_AFTER_SECONDS so a hostile or
    misconfigured server can't park a coroutine indefinitely.
    """
    status = resp.status_code

    if status in (429, 503):
        ra = resp.headers.get("retry-after")
        if ra is not None:
            wait = _parse_retry_after(ra)
            if wait is not None:
                return min(wait, MAX_RETRY_AFTER_SECONDS)

    if status in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                delta = float(reset) - time.time()
            except ValueError:
                delta = None
            if delta is not None:
                return min(max(0.0, delta), MAX_RETRY_AFTER_SECONDS)

    if status == 429:
        return DEFAULT_RATE_LIMIT_BACKOFF_SECONDS

    return None


async def request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    rate_limiter: AsyncRateLimiter | None = None,
    max_retries: int = 2,
) -> httpx.Response:
    """GET ``url`` honoring rate-limit/throttle backoff, returning the raw Response.

    Unlike :func:`fetch_with_politeness` this neither parses the body nor calls
    ``raise_for_status`` -- callers that need both the JSON payload AND response
    headers (e.g. GitHub's ``Link`` pagination and ``X-RateLimit-*``) drive that
    themselves. Each attempt is spaced by the optional rate limiter; a throttled
    response (Retry-After, X-RateLimit-Reset, or a bare 429) is slept off and
    retried up to ``max_retries`` times before the response is returned as-is.
    """
    attempts = 0
    while True:
        if rate_limiter is not None:
            await rate_limiter.acquire()
        resp = await client.get(url, params=params, headers=headers)
        if attempts >= max_retries:
            return resp
        delay = _rate_limit_delay(resp)
        if delay is None:
            return resp
        attempts += 1
        await asyncio.sleep(delay)


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

    # Throttle path: an explicit Retry-After (only ever honored on 429/503), a
    # GitHub-style X-RateLimit-Reset, or a bare 429 with no header (fixed
    # backoff). _rate_limit_delay returns an already-clamped delay, or None when
    # the response isn't a retryable throttle.
    delay = _rate_limit_delay(resp)
    if delay is not None:
        await asyncio.sleep(delay)
        resp = await _do_request()
        retried = True

    # Bare 5xx without a throttle signal: backoff and retry once.
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
