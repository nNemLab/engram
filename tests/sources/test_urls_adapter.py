"""Urls adapter: iterate config.urls, fetch each via shared helper."""
import json

import httpx
import pytest

from engram.poller.adapters.urls import UrlsAdapter


def _src(urls, cursor=None) -> dict:
    return {
        "id": "test",
        "url": "",
        "config": json.dumps({"urls": urls, "request_interval_ms": 0}),
        "cursor": cursor,
    }


@pytest.mark.asyncio
async def test_three_urls_all_200_yields_three():
    def h(req):
        return httpx.Response(
            200, text=f"<html><body><h1>{req.url.path}</h1>page</body></html>",
            headers={"etag": f'"{req.url.path}"', "content-type": "text/html"},
        )
    transport = httpx.MockTransport(h)
    adapter = UrlsAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src([
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ])
    cands = [c async for c in adapter.fetch(src)]
    assert len(cands) == 3
    cache = json.loads(src["cursor"])["cache"]
    assert set(cache.keys()) == {
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    }


@pytest.mark.asyncio
async def test_all_304_yields_none():
    def h(req):
        return httpx.Response(304)
    transport = httpx.MockTransport(h)
    adapter = UrlsAdapter(_client=httpx.AsyncClient(transport=transport))
    cursor = json.dumps({
        "cache": {
            "https://example.com/a": {"etag": '"a"', "last_modified": None},
            "https://example.com/b": {"etag": '"b"', "last_modified": None},
        }
    })
    src = _src(["https://example.com/a", "https://example.com/b"], cursor=cursor)
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []
    cache = json.loads(src["cursor"])["cache"]
    # Cache preserved across 304s
    assert cache["https://example.com/a"]["etag"] == '"a"'
    assert cache["https://example.com/b"]["etag"] == '"b"'


@pytest.mark.asyncio
async def test_one_changed_one_304():
    def h(req):
        if "/changed" in req.url.path:
            return httpx.Response(200, text="new body",
                                  headers={"etag": '"new"', "content-type": "text/html"})
        return httpx.Response(304)
    transport = httpx.MockTransport(h)
    adapter = UrlsAdapter(_client=httpx.AsyncClient(transport=transport))
    cursor = json.dumps({"cache": {
        "https://example.com/changed":   {"etag": '"old"', "last_modified": None},
        "https://example.com/unchanged": {"etag": '"keep"', "last_modified": None},
    }})
    src = _src([
        "https://example.com/changed",
        "https://example.com/unchanged",
    ], cursor=cursor)
    cands = [c async for c in adapter.fetch(src)]
    assert len(cands) == 1
    assert cands[0].source_url == "https://example.com/changed"
    cache = json.loads(src["cursor"])["cache"]
    assert cache["https://example.com/changed"]["etag"] == '"new"'
    assert cache["https://example.com/unchanged"]["etag"] == '"keep"'


@pytest.mark.asyncio
async def test_empty_urls_no_op():
    def h(req):
        raise AssertionError("should not be called")
    transport = httpx.MockTransport(h)
    adapter = UrlsAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src([])
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []
    assert json.loads(src["cursor"]) == {"cache": {}}


@pytest.mark.asyncio
async def test_url_removed_from_config_drops_from_cursor():
    """If config.urls shrinks, dropped URLs disappear from the cursor cache."""
    def h(req):
        return httpx.Response(304)
    transport = httpx.MockTransport(h)
    adapter = UrlsAdapter(_client=httpx.AsyncClient(transport=transport))
    cursor = json.dumps({"cache": {
        "https://example.com/keep":   {"etag": '"k"', "last_modified": None},
        "https://example.com/dropped":{"etag": '"d"', "last_modified": None},
    }})
    src = _src(["https://example.com/keep"], cursor=cursor)
    [c async for c in adapter.fetch(src)]
    cache = json.loads(src["cursor"])["cache"]
    assert "https://example.com/keep" in cache
    assert "https://example.com/dropped" not in cache
