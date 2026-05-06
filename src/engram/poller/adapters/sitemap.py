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

logger = logging.getLogger("engram.poller.sitemap")
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


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
        etags: dict[str, str] = cursor.get("etags", {})

        urls = await self._collect_urls(source["url"])
        new_etags: dict[str, str] = {}
        for u in urls:
            if not matches_globs(u, include, exclude):
                continue
            previous = etags.get(u)
            cand = await self._fetch_one(u, previous_etag=previous)
            if cand is None:
                if previous:
                    new_etags[u] = previous
                continue
            etag, candidate = cand
            if etag:
                new_etags[u] = etag
            yield candidate

        source["cursor"] = json.dumps({
            "etags": new_etags,
            "last_seen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    async def _collect_urls(self, sitemap_url: str) -> list[str]:
        """Fetch sitemap.xml; if it's a sitemap-index, descend one level."""
        out: list[str] = []
        resp = await self._client.get(sitemap_url)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        if root.tag.endswith("sitemapindex"):
            for sm in root.findall("sm:sitemap/sm:loc", NS):
                if sm.text:
                    out.extend(await self._collect_urls(sm.text.strip()))
        else:
            for loc in root.findall("sm:url/sm:loc", NS):
                if loc.text:
                    out.append(loc.text.strip())
        return out

    async def _fetch_one(
        self, url: str, *, previous_etag: str | None
    ) -> tuple[str | None, Candidate] | None:
        headers: dict[str, str] = {}
        if previous_etag:
            headers["if-none-match"] = previous_etag
        resp = await self._client.get(url, headers=headers)
        if resp.status_code == 304:
            return None
        resp.raise_for_status()
        body_html = resp.text
        extracted = trafilatura.extract(body_html, include_comments=False) or body_html
        title = trafilatura.extract_metadata(body_html)
        title_str = title.title if title and title.title else None
        etag = resp.headers.get("etag")
        cand = Candidate(
            source_url=url,
            body=extracted,
            title=title_str,
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            metadata={"etag": etag, "content_type": resp.headers.get("content-type")},
        )
        return etag, cand


register(SitemapAdapter())
