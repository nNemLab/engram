"""MediaWiki API adapter.

Discovers pages via action=query&list=allpages (first run) or list=recentchanges
(incremental). Fetches content via action=parse&prop=text. Always sends maxlag=5
and assert=anon.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
import trafilatura

from . import Candidate, matches_globs, register
from ._http import AsyncRateLimiter, fetch_with_politeness

logger = logging.getLogger("engram.poller.mediawiki_api")

DEFAULT_INTERVAL_MS = 1500
ALWAYS_PARAMS = {"format": "json", "maxlag": "5", "assert": "anon"}


class MediaWikiApiAdapter:
    name = "mediawiki-api"

    def __init__(self, *, _client: httpx.AsyncClient | None = None,
                 user_agent: str = "engram/0.3 (+source-poller)") -> None:
        self._client = _client or httpx.AsyncClient(
            headers={"user-agent": user_agent}, timeout=30.0,
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client (poller shutdown path, #92)."""
        await self._client.aclose()

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        cfg = json.loads(source.get("config") or "{}")
        namespaces: list[int] = cfg.get("namespaces", [0])
        include: list[str] = cfg.get("include", ["*"])
        exclude: list[str] = cfg.get("exclude", [])
        max_pages = int(cfg.get("max_pages_first_run", 1000))
        interval_ms = int(cfg.get("request_interval_ms", DEFAULT_INTERVAL_MS))

        rate_limiter = AsyncRateLimiter(interval_ms=interval_ms)
        wiki_root = source["url"].rstrip("/")
        api_endpoint = f"{wiki_root}/api.php"

        cursor = json.loads(source.get("cursor") or "{}")
        last_rc_at = cursor.get("last_rc_at")

        new_rc_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Discover titles
        titles: list[str] = []
        if last_rc_at is None:
            # First run: walk all pages, capped by max_pages_first_run.
            for ns in namespaces:
                async for title in self._list_allpages(api_endpoint, ns, rate_limiter):
                    if not matches_globs(title, include, exclude):
                        continue
                    titles.append(title)
                    if len(titles) >= max_pages:
                        break
                if len(titles) >= max_pages:
                    break
        else:
            # Subsequent run: only changed pages since cursor.
            async for title in self._list_recentchanges(
                api_endpoint, namespaces, since=last_rc_at, rate_limiter=rate_limiter,
            ):
                if not matches_globs(title, include, exclude):
                    continue
                titles.append(title)

        for title in titles:
            html = await self._parse_page(api_endpoint, title, rate_limiter)
            if html is None:
                continue
            extracted = trafilatura.extract(html, include_comments=False) or html
            if not extracted or not extracted.strip():
                continue
            url_title = quote(title.replace(" ", "_"), safe=":/_()")
            yield Candidate(
                source_url=f"{wiki_root}/wiki/{url_title}",
                body=extracted,
                title=title,
                fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                metadata={"page_id": None},
            )

        source["cursor"] = json.dumps({
            "last_rc_at": new_rc_at,
            "api_endpoint": api_endpoint,
        })

    async def _list_allpages(
        self, api_endpoint: str, namespace: int, rate_limiter: AsyncRateLimiter,
    ) -> AsyncIterator[str]:
        cont: dict[str, str] = {}
        while True:
            params = {
                **ALWAYS_PARAMS,
                "action": "query",
                "list": "allpages",
                "aplimit": "500",
                "apnamespace": str(namespace),
            }
            params.update(cont)
            result = await fetch_with_politeness(
                self._client, api_endpoint, extra_params=params, rate_limiter=rate_limiter,
            )
            if result is None:  # 304 not expected for API; defensive
                return
            data = json.loads(result.body)
            _check_api_error(data)
            for row in data.get("query", {}).get("allpages", []):
                yield row["title"]
            if "continue" not in data:
                return
            # Copy ALL keys from data["continue"] (some MW versions add companion tokens)
            cont = {k: str(v) for k, v in data["continue"].items()}

    async def _list_recentchanges(
        self, api_endpoint: str, namespaces: Iterable[int], since: str,
        rate_limiter: AsyncRateLimiter,
    ) -> AsyncIterator[str]:
        """Yield unique page titles changed since `since` (ISO timestamp).

        Uses rcdir=newer with rcstart=<since> so we walk forward in time and the
        last entry's timestamp is the new cursor. Filters to type ∈ {edit, new}.
        Yields each title at most once per call (deduped across edits).
        """
        seen: set[str] = set()
        for ns in namespaces:
            cont: dict[str, str] = {}
            while True:
                params = {
                    **ALWAYS_PARAMS,
                    "action": "query",
                    "list": "recentchanges",
                    "rcdir": "newer",
                    "rcstart": since,
                    "rcnamespace": str(ns),
                    "rclimit": "500",
                    "rcprop": "title|timestamp|ids|type",
                }
                params.update(cont)
                result = await fetch_with_politeness(
                    self._client, api_endpoint, extra_params=params, rate_limiter=rate_limiter,
                )
                if result is None:
                    return
                data = json.loads(result.body)
                _check_api_error(data)
                for row in data.get("query", {}).get("recentchanges", []):
                    if row.get("type") not in ("edit", "new"):
                        continue
                    title = row.get("title")
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    yield title
                if "continue" not in data:
                    break
                cont = {k: str(v) for k, v in data["continue"].items()}

    async def _parse_page(
        self, api_endpoint: str, title: str, rate_limiter: AsyncRateLimiter,
    ) -> str | None:
        params = {
            **ALWAYS_PARAMS,
            "action": "parse",
            "page": title,
            "prop": "text",
            "disableeditsection": "1",
        }
        try:
            result = await fetch_with_politeness(
                self._client, api_endpoint, extra_params=params, rate_limiter=rate_limiter,
            )
        except httpx.HTTPStatusError:
            raise
        if result is None:
            return None
        data = json.loads(result.body)
        err = data.get("error")
        if err:
            code = err.get("code", "")
            if code == "missingtitle":
                logger.debug("mediawiki_api: page %r deleted between list and parse; skipping", title)
                return None
            # Re-raise as exception so the poller's classify_error handles it.
            raise RuntimeError(f"MediaWiki API error {code}: {err.get('info', '')}")
        return data.get("parse", {}).get("text", {}).get("*", "")


def _check_api_error(data: dict) -> None:
    """Raise on any API-level error in a query response."""
    err = data.get("error")
    if err:
        raise RuntimeError(f"MediaWiki API error {err.get('code')}: {err.get('info', '')}")


register(MediaWikiApiAdapter())
