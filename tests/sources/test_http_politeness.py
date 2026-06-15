"""fetch_with_politeness: Retry-After handling, conditional headers, rate-limiting."""
from unittest.mock import patch

import httpx
import pytest

from engram.poller.adapters._http import (
    MAX_RETRY_AFTER_SECONDS,
    AsyncRateLimiter,
    HTTPCacheEntry,
    fetch_with_politeness,
)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_200_returns_fetchresult():
    def h(req):
        return httpx.Response(200, text="hello",
                              headers={"etag": '"abc"', "last-modified": "Mon, 1 Jan 2026 00:00:00 GMT",
                                       "content-type": "text/html"})
    rl = AsyncRateLimiter(interval_ms=0)
    async with _client(h) as c:
        r = await fetch_with_politeness(c, "https://x/p", rate_limiter=rl)
    assert r is not None
    assert r.body == "hello"
    assert r.etag == '"abc"'
    assert r.last_modified == "Mon, 1 Jan 2026 00:00:00 GMT"


@pytest.mark.asyncio
async def test_304_returns_none():
    def h(req):
        assert req.headers.get("if-none-match") == '"abc"'
        return httpx.Response(304)
    cache = HTTPCacheEntry(etag='"abc"')
    rl = AsyncRateLimiter(interval_ms=0)
    async with _client(h) as c:
        r = await fetch_with_politeness(c, "https://x/p", cache=cache, rate_limiter=rl)
    assert r is None


@pytest.mark.asyncio
async def test_sends_both_conditional_headers():
    captured = {}
    def h(req):
        captured["if_none_match"] = req.headers.get("if-none-match")
        captured["if_modified_since"] = req.headers.get("if-modified-since")
        return httpx.Response(304)
    cache = HTTPCacheEntry(etag='"abc"', last_modified="Mon, 1 Jan 2026 00:00:00 GMT")
    rl = AsyncRateLimiter(interval_ms=0)
    async with _client(h) as c:
        await fetch_with_politeness(c, "https://x/p", cache=cache, rate_limiter=rl)
    assert captured["if_none_match"] == '"abc"'
    assert captured["if_modified_since"] == "Mon, 1 Jan 2026 00:00:00 GMT"


@pytest.mark.asyncio
async def test_retry_after_seconds_honored():
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, text="ok")
    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)
    rl = AsyncRateLimiter(interval_ms=0)
    with patch("engram.poller.adapters._http.asyncio.sleep", side_effect=fake_sleep):
        async with _client(h) as c:
            r = await fetch_with_politeness(c, "https://x/p", rate_limiter=rl)
    assert r is not None
    assert calls["n"] == 2
    assert 1.0 in sleeps  # honored


@pytest.mark.asyncio
async def test_retry_after_60_does_not_actually_sleep_in_test():
    """Ensures we use the patched asyncio.sleep — proves we'd sleep 60s in real life."""
    def h(req):
        return httpx.Response(429, headers={"retry-after": "60"})
    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)
    rl = AsyncRateLimiter(interval_ms=0)
    with patch("engram.poller.adapters._http.asyncio.sleep", side_effect=fake_sleep):
        async with _client(h) as c:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_with_politeness(c, "https://x/p", rate_limiter=rl)
    assert 60.0 in sleeps


@pytest.mark.asyncio
async def test_hostile_retry_after_is_capped():
    """A hostile `Retry-After: 86400` (a full day) is clamped to the max."""
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "86400"})
        return httpx.Response(200, text="ok")
    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)
    rl = AsyncRateLimiter(interval_ms=0)
    with patch("engram.poller.adapters._http.asyncio.sleep", side_effect=fake_sleep):
        async with _client(h) as c:
            r = await fetch_with_politeness(c, "https://x/p", rate_limiter=rl)
    assert r is not None
    assert calls["n"] == 2
    # 86400s requested, but the awaited backoff is clamped to the cap — a single
    # source can never park its coroutine for a day.
    assert max(sleeps) <= MAX_RETRY_AFTER_SECONDS
    assert MAX_RETRY_AFTER_SECONDS in sleeps
    assert 86400.0 not in sleeps


@pytest.mark.asyncio
async def test_503_without_retry_after_backs_off():
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="recovered")
    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)
    rl = AsyncRateLimiter(interval_ms=2000)
    with patch("engram.poller.adapters._http.asyncio.sleep", side_effect=fake_sleep):
        async with _client(h) as c:
            r = await fetch_with_politeness(c, "https://x/p", rate_limiter=rl)
    assert r is not None
    # backoff should be min(5.0, 2*2.0) = 4.0
    assert any(s == 4.0 for s in sleeps)


@pytest.mark.asyncio
async def test_persistent_4xx_propagates():
    def h(req):
        return httpx.Response(404)
    rl = AsyncRateLimiter(interval_ms=0)
    async with _client(h) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_with_politeness(c, "https://x/p", rate_limiter=rl)


@pytest.mark.asyncio
async def test_rate_limiter_spaces_calls():
    """Two acquisitions in a row are spaced by interval_ms."""
    rl = AsyncRateLimiter(interval_ms=50)
    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)
    with patch("engram.poller.adapters._http.asyncio.sleep", side_effect=fake_sleep):
        await rl.acquire()
        await rl.acquire()
    # Second acquire must have asked to sleep ~0.05s
    assert any(0 < s <= 0.06 for s in sleeps), f"sleeps={sleeps}"


@pytest.mark.asyncio
async def test_extra_params_appended():
    captured = {}
    def h(req):
        captured["query"] = str(req.url.query)
        return httpx.Response(200, text="ok")
    rl = AsyncRateLimiter(interval_ms=0)
    async with _client(h) as c:
        await fetch_with_politeness(
            c, "https://x/p", extra_params={"maxlag": "5", "format": "json"}, rate_limiter=rl
        )
    assert "maxlag=5" in captured["query"]
    assert "format=json" in captured["query"]
