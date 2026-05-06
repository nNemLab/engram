"""Sitemap adapter: parses sitemap.xml, applies include/exclude globs,
fetches pages with ETag honoring, yields Candidates."""
import json
from pathlib import Path

import httpx
import pytest

from engram.poller.adapters.sitemap import SitemapAdapter

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "tests" / "fixtures"


def _build_source(**overrides) -> dict:
    base = {
        "id": "test",
        "url": "https://docs.example.com/sitemap.xml",
        "config": json.dumps({
            "include": ["*/engine/*"],
            "exclude": ["*/macos/*"],
        }),
        "cursor": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_filter_includes_engine_excludes_macos(monkeypatch):
    """Adapter filters URLs through include/exclude globs."""
    sitemap_xml = (FIXTURE_DIR / "sitemap_minimal.xml").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        return httpx.Response(200, text=f"<html><body><h1>{request.url.path}</h1>page body</body></html>",
                              headers={"content-type": "text/html", "etag": f'"{request.url.path}"'})

    transport = httpx.MockTransport(handler)
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))

    cands = []
    async for c in adapter.fetch(_build_source()):
        cands.append(c)

    urls = [c.source_url for c in cands]
    assert "https://docs.example.com/engine/install/linux/" in urls
    assert "https://docs.example.com/engine/cli/" in urls
    assert all("/macos/" not in u for u in urls)
    assert all("/blog/" not in u for u in urls)
    assert len(cands) == 2


@pytest.mark.asyncio
async def test_etag_skips_unchanged(monkeypatch):
    """If a URL's etag matches the cursor, the adapter does not yield it again."""
    sitemap_xml = (FIXTURE_DIR / "sitemap_minimal.xml").read_text()

    target_url = "https://docs.example.com/engine/install/linux/"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        if request.headers.get("if-none-match") == '"abc"' and str(request.url) == target_url:
            return httpx.Response(304)
        return httpx.Response(200, text="page body",
                              headers={"content-type": "text/html", "etag": '"new"'})

    transport = httpx.MockTransport(handler)
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))

    src = _build_source(cursor=json.dumps({"etags": {target_url: '"abc"'}}))
    cands = []
    async for c in adapter.fetch(src):
        cands.append(c)

    urls = [c.source_url for c in cands]
    assert target_url not in urls
    assert "https://docs.example.com/engine/cli/" in urls


@pytest.mark.asyncio
async def test_updates_cursor_etags(monkeypatch):
    """After fetching, the adapter writes new etags into the source's cursor field."""
    sitemap_xml = (FIXTURE_DIR / "sitemap_minimal.xml").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        return httpx.Response(200, text="page",
                              headers={"content-type": "text/html",
                                       "etag": f'"etag-{request.url.path}"'})

    transport = httpx.MockTransport(handler)
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))

    src = _build_source()
    async for _ in adapter.fetch(src):
        pass
    new_cursor = json.loads(src["cursor"])
    assert "etags" in new_cursor
    assert new_cursor["etags"]["https://docs.example.com/engine/install/linux/"] == \
        '"etag-/engine/install/linux/"'
