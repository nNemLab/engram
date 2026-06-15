"""Cross-adapter test of the incremental-fetch invariant.

After a first successful poll, subsequent polls with no upstream changes
must fetch zero candidate bodies. Each adapter realizes this differently:
  - sitemap, urls: 304 via If-None-Match / If-Modified-Since
  - mediawiki-api: empty recentchanges
  - github-repo: same last_sha
"""
import json
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures"


def _src(adapter, **overrides):
    base = {
        "id": "test",
        "url": "https://example.com",
        "config": json.dumps({"request_interval_ms": 0}),
        "cursor": None,
    }
    base.update({"adapter": adapter})
    base.update(overrides)
    return base


# ---------- sitemap ----------

@pytest.mark.asyncio
async def test_sitemap_no_changes_yields_zero(monkeypatch):
    """Second poll with no upstream changes: 0 candidates, all conditional GETs return 304."""
    from engram.poller.adapters.sitemap import SitemapAdapter

    sitemap_xml = (FIX / "sitemap_minimal.xml").read_text()

    def h(req):
        path = req.url.path
        if path.endswith("/sitemap_minimal.xml") or path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        # First call: no cache → 200 with etag
        # Second call: client sends If-None-Match → 304
        if req.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, text=f"page {path}",
                              headers={"etag": f'"e-{path}"', "content-type": "text/html"})

    transport = httpx.MockTransport(h)

    src = {
        "id": "t", "url": "https://docs.example.com/sitemap.xml",
        "config": json.dumps({"include": ["**/engine/**"], "request_interval_ms": 0}),
        "cursor": None,
    }
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))
    first = [c async for c in adapter.fetch(src)]
    assert len(first) > 0
    # Cursor is now populated with cache entries

    # Second poll: same fixture, but client should send If-None-Match → 304
    adapter2 = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))
    second = [c async for c in adapter2.fetch(src)]
    assert second == []


@pytest.mark.asyncio
async def test_sitemap_last_modified_only_honored():
    """Sitemap source returns Last-Modified but no ETag → If-Modified-Since used."""
    from engram.poller.adapters.sitemap import SitemapAdapter

    sitemap_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
                   '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                   '<url><loc>https://docs.example.com/engine/install/linux/</loc></url>'
                   '</urlset>')

    def h(req):
        if req.url.path.endswith("/sitemap.xml"):
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        if req.headers.get("if-modified-since"):
            return httpx.Response(304)
        return httpx.Response(200, text="body",
                              headers={"last-modified": "Mon, 1 Jan 2026 00:00:00 GMT",
                                       "content-type": "text/html"})

    transport = httpx.MockTransport(h)
    src = {
        "id": "t", "url": "https://docs.example.com/sitemap.xml",
        "config": json.dumps({"include": ["**/engine/**"], "request_interval_ms": 0}),
        "cursor": None,
    }
    adapter1 = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))
    first = [c async for c in adapter1.fetch(src)]
    assert len(first) == 1
    # second poll: client should send If-Modified-Since → 304
    adapter2 = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))
    second = [c async for c in adapter2.fetch(src)]
    assert second == []


# ---------- mediawiki ----------

@pytest.mark.asyncio
async def test_mediawiki_empty_recentchanges_zero_parse_calls():
    from engram.poller.adapters.mediawiki_api import MediaWikiApiAdapter

    parse_calls = {"n": 0}
    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=json.dumps({
                "batchcomplete": "",
                "query": {"recentchanges": []},
            }))
        if action == "parse":
            parse_calls["n"] += 1
            return httpx.Response(200, text=json.dumps({"parse": {"title": "x", "text": {"*": "x"}}}))
        return httpx.Response(404)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = {
        "id": "t", "url": "https://wiki.example.com",
        "config": json.dumps({"namespaces": [0], "request_interval_ms": 0}),
        "cursor": json.dumps({"last_rc_at": "2026-05-05T00:00:00Z"}),
    }
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []
    assert parse_calls["n"] == 0


@pytest.mark.asyncio
async def test_mediawiki_one_change_yields_one_parse_call():
    from engram.poller.adapters.mediawiki_api import MediaWikiApiAdapter

    rc = json.dumps({
        "batchcomplete": "",
        "query": {"recentchanges": [
            {"type": "edit", "ns": 0, "title": "Engine", "pageid": 1,
             "revid": 100, "timestamp": "2026-05-06T10:00:00Z"},
        ]},
    })
    parse_resp = json.dumps({"parse": {"title": "Engine", "text": {"*": "<p>engine</p>"}}})
    parse_calls = {"n": 0}

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=rc)
        if action == "parse":
            parse_calls["n"] += 1
            return httpx.Response(200, text=parse_resp)
        return httpx.Response(404)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = {
        "id": "t", "url": "https://wiki.example.com",
        "config": json.dumps({"namespaces": [0], "request_interval_ms": 0}),
        "cursor": json.dumps({"last_rc_at": "2026-05-05T00:00:00Z"}),
    }
    cands = [c async for c in adapter.fetch(src)]
    assert len(cands) == 1
    assert parse_calls["n"] == 1


# ---------- urls ----------

@pytest.mark.asyncio
async def test_urls_all_unchanged_yields_zero():
    from engram.poller.adapters.urls import UrlsAdapter

    body_calls = {"n": 0}
    def h(req):
        if req.headers.get("if-none-match"):
            return httpx.Response(304)
        body_calls["n"] += 1
        return httpx.Response(200, text="body",
                              headers={"etag": '"v1"', "content-type": "text/html"})

    transport = httpx.MockTransport(h)
    src = {
        "id": "t", "url": "",
        "config": json.dumps({
            "urls": ["https://a.example/x", "https://b.example/y"],
            "request_interval_ms": 0,
        }),
        "cursor": None,
    }
    a1 = UrlsAdapter(_client=httpx.AsyncClient(transport=transport))
    first = [c async for c in a1.fetch(src)]
    assert len(first) == 2
    assert body_calls["n"] == 2

    a2 = UrlsAdapter(_client=httpx.AsyncClient(transport=transport))
    second = [c async for c in a2.fetch(src)]
    assert second == []
    # Bodies were not transferred again — second poll's 200s should have been zero
    # (every URL responded 304 because its cached etag was sent).
    # body_calls counts only 200 responses, so it should still be 2 after second poll.
    assert body_calls["n"] == 2
