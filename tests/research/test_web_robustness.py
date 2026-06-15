import asyncio
from types import SimpleNamespace

import httpx

from engram.research import web


async def test_searxng_query_malformed_json_returns_empty_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="this is not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await web._searxng_query(client, "https://searx.local", "query", 10)

    assert results == []


async def test_search_async_dedups_urls_before_fetch(monkeypatch):
    raw = [
        {"url": "https://example.com/a", "title": "a-1", "content": "c1"},
        {"url": "https://example.com/a", "title": "a-2", "content": "c2"},
        {"url": "https://example.com/b", "title": "b", "content": "c3"},
    ]
    fetched_urls: list[str] = []

    async def fake_searxng_query(client, base_url, q, max_candidates):
        return raw

    async def fake_fetch_one(client, url):
        fetched_urls.append(url)
        return f"body:{url}"

    monkeypatch.setattr(web, "_searxng_query", fake_searxng_query)
    monkeypatch.setattr(web, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(web, "_extract", lambda html: html)
    monkeypatch.setattr(web.rerank, "score", lambda query, passages: [1.0] * len(passages))
    monkeypatch.setattr(
        web,
        "load_config",
        lambda: SimpleNamespace(research=SimpleNamespace(searxng_url="https://searx.local")),
    )

    hits = await web._search_async("query", k=8, max_candidates=20)

    assert len(hits) == 2
    assert fetched_urls == ["https://example.com/a", "https://example.com/b"]


async def test_search_async_bounds_fetch_fanout(monkeypatch):
    raw = [{"url": f"https://example.com/{i}", "title": f"t{i}", "content": f"c{i}"} for i in range(20)]

    max_in_flight = 0
    in_flight = 0
    lock = asyncio.Lock()

    async def fake_searxng_query(client, base_url, q, max_candidates):
        return raw[:max_candidates]

    async def fake_fetch_one(client, url):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return "body text"

    monkeypatch.setattr(web, "_searxng_query", fake_searxng_query)
    monkeypatch.setattr(web, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(web, "_extract", lambda html: html)
    monkeypatch.setattr(web.rerank, "score", lambda query, passages: [1.0] * len(passages))
    monkeypatch.setattr(
        web,
        "load_config",
        lambda: SimpleNamespace(research=SimpleNamespace(searxng_url="https://searx.local")),
    )

    hits = await web._search_async("query", k=8, max_candidates=20)

    assert len(hits) == 8
    assert max_in_flight <= web._MAX_FETCH_CONCURRENCY
