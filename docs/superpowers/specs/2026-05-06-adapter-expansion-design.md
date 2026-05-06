# Adapter expansion: mediawiki-api + urls — design spec

**Date:** 2026-05-06
**Status:** approved (brainstorm), pending implementation plan
**Target version:** v0.3.0-alpha.1

## Problem

The v0.2 source-curation system has two adapters: `sitemap` and `github-repo`. Real-world testing on Elite Dangerous documentation revealed a class of authoritative sources that neither adapter can reach:

- **MediaWiki-based wikis** (Fandom, PCGamingWiki, ED-Codex, etc.) gate their HTML and `sitemap.xml` paths via Cloudflare. Our default user-agent gets a hard 403. The `/api.php` endpoint, designed for programmatic access, returns 200 — but no adapter speaks MediaWiki API.
- **Sitemap-less small sites** (Inara reference pages, ed.tools, Wikipedia single articles) are accessible but publish no sitemap. The operator knows the few URLs that matter; we have no way to declare them.

Without a remedy, "official wiki / highly respected community" sources are mostly unreachable, and the curation feature only covers software docs (Docker, Kubernetes, PyTorch) — not the long tail of subject-matter wikis or curated reference pages.

## Goals

1. New `mediawiki-api` adapter: declarative subscription to any MediaWiki wiki via its API. Discovers pages via `list=allpages`; tracks updates via `list=recentchanges`; fetches content via `action=parse`.
2. New `urls` adapter: declarative subscription to a manually curated list of URLs.
3. Politeness floor strong enough to not get blocked: per-adapter sane defaults, `Retry-After` honoring across all adapters, MediaWiki `maxlag=5`, identifying User-Agent with operator contact.
4. **Incremental fetch invariant:** after first poll, subsequent polls fetch only changed pages. Strengthen the existing per-adapter signals with `If-Modified-Since` fallback for sources that don't expose ETags.
5. Worked example: Elite Dangerous Fandom wiki + a curated URL list of reference pages (Wikipedia article, ed.tools, Inara reference page).

## Non-goals (v0.3)

- RSS/Atom adapter (deferred — separate use case).
- Generic-crawl adapter (depth-limited link following). Defer until A+B prove insufficient.
- Headless-browser fetch (Playwright). Heavy dep, defer.
- Per-host concurrency cap across multiple sources to the same host.
- Adaptive rate-limiting based on observed latency.
- robots.txt parsing.
- Deletion propagation (URL disappearing from a feed → tombstone).
- Per-page TTL forcing periodic re-verification.

## Design decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| MediaWiki discovery | `list=allpages` first run, `list=recentchanges` subsequent | Standard MediaWiki bot pattern; cheap incremental updates. |
| MediaWiki content extraction | `action=parse&prop=text` (rendered HTML) → trafilatura | Wiki markup doesn't render cleanly in Obsidian; HTML works with our existing extractor. |
| MediaWiki source URL | Title-based: `<wiki>/wiki/<page_title>` | Matches what users see; renames create new entries (intentional). |
| MediaWiki first-run cap | 1000 pages default, configurable | Protects against accidentally walking a 50k-page wiki. |
| MediaWiki politeness | 1.5 s default; `maxlag=5` always; `assert=anon` | Below Fandom's 60/min/IP soft limit; honors WMF guideline. |
| URLs adapter | Trivial — iterate `config.urls` | Escape hatch for sitemap-less sources. |
| Conditional fetch | Both `If-None-Match` (ETag) and `If-Modified-Since` | Strictly more conservative than ETag-only; many sites only expose one. |
| Shared HTTP helper | Lift current sitemap fetcher into `adapters/_http.py` | Avoid the third copy when we add the urls adapter. |
| User-Agent contact | Configurable in `config.yml` under `poller.http.contact`; soft default if unset | Friendly to providers, easy for operator to set, doesn't block startup. |

## Architecture

```
                ┌─────────────────────────────────────┐
                │  sources table (unchanged)          │
                └──────────────────┬──────────────────┘
                                   │
                          ┌────────┴───────┐
                          │ engram-poller  │
                          └────────┬───────┘
                                   │ dispatch by adapter name
                ┌──────────────┬───┴──────┬──────────────┐
                ▼              ▼          ▼              ▼
         ┌──────────┐  ┌─────────────┐ ┌──────┐  ┌────────────────┐
         │ sitemap  │  │ github-repo │ │ urls │  │ mediawiki-api  │
         │  (v0.2)  │  │   (v0.2)    │ │ NEW  │  │      NEW       │
         └────┬─────┘  └──────┬──────┘ └──┬───┘  └────────┬───────┘
              │               │           │               │
              └───────┬───────┴───────────┴───────────────┘
                      │ all adapters use the same Candidate type
                      ▼
         ┌──────────────────────────────────┐
         │ shared HTTP helper:              │  ETag + Last-Modified
         │ adapters/_http.py NEW            │  Retry-After honoring
         │ fetch_with_politeness()          │  Configurable rate-limit
         └──────────────────────────────────┘
                      │
                      ▼  (Candidates)
                dedup.gate (unchanged from v0.2)
```

**No schema changes.** All v0.2 invariants on `sources`, `content.revision/is_current/superseded_by/source_id` apply unchanged. New event types: none — the existing `source_polled` / `source_error` cover the new adapters.

## Component: shared HTTP helper (`src/engram/poller/adapters/_http.py`)

Lifted from the existing `sitemap.py::_fetch_one`, generalized:

```python
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

async def fetch_with_politeness(
    client: httpx.AsyncClient,
    url: str,
    *,
    cache: HTTPCacheEntry | None = None,
    extra_params: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    rate_limiter: AsyncRateLimiter,
) -> FetchResult | None:
    """Returns FetchResult on 200; None on 304.

    Honors Retry-After on 429 and 503 (one retry).
    Sends If-None-Match and If-Modified-Since when cache is supplied.
    Acquires from rate_limiter before each request (including retries).
    Raises httpx.HTTPStatusError on persistent 4xx and on 5xx after one retry.
    """
```

`AsyncRateLimiter`: simple token bucket sized from `request_interval_ms`. One instance per source, lives on the adapter.

The `sitemap` adapter is refactored to call `fetch_with_politeness`. Its existing tests stay green; the helper becomes independently testable.

## Component: `mediawiki-api` adapter

### Config

```yaml
adapter: mediawiki-api
url: https://elite-dangerous.fandom.com   # wiki root; api.php is appended
config:
  namespaces: [0]              # default [0] = main content
  include: ["*"]               # title globs
  exclude: ["File:*", "User:*"]
  max_pages_first_run: 1000
  request_interval_ms: 1500    # optional override
schedule: 7d
source_tier: vendor-doc
```

### API endpoints used

All requests go to `<url>/api.php` with `format=json&maxlag=5&assert=anon`.

| Purpose | Action | Notable params |
|---|---|---|
| List all pages | `query&list=allpages` | `aplimit=500&apnamespace=<n>&apcontinue=<token>` |
| List changes since cursor | `query&list=recentchanges` | `rcend=<ISO ts>&rcnamespace=<n>&rclimit=500&rcprop=title|timestamp|ids|type` |
| Fetch page content | `parse&page=<title>&prop=text&disableeditsection=1` | Returns rendered HTML in `parse.text["*"]` |

### Discovery flow

```python
async def fetch(self, source):
    cursor = json.loads(source.get("cursor") or "{}")
    last_rc_at = cursor.get("last_rc_at")  # ISO timestamp or None
    namespaces = config.get("namespaces", [0])

    if last_rc_at is None:
        # First run: walk all pages, capped.
        titles = await self._list_all_pages(namespaces, max=config["max_pages_first_run"])
    else:
        # Incremental: only changed pages since cursor.
        titles = await self._list_recent_changes(namespaces, since=last_rc_at)

    titles = [t for t in titles if matches_globs(t, include, exclude)]

    new_rc_at = utcnow_iso()
    for title in titles:
        html = await self._parse(title)
        if html is None:
            continue
        body = trafilatura.extract(html) or html
        yield Candidate(
            source_url=f"{wiki_base}/wiki/{quote(title.replace(' ', '_'))}",
            body=body,
            title=title,
            fetched_at=utcnow_iso(),
            metadata={"page_id": ...},  # from parse response
        )

    source["cursor"] = json.dumps({
        "last_rc_at": new_rc_at,
        "api_endpoint": f"{wiki_base}/api.php",
    })
```

### Politeness

- All requests via `fetch_with_politeness` with the source's rate-limiter.
- `maxlag=5` and `assert=anon` are always sent.
- 503 with `Retry-After` (the maxlag overload signal) → honored automatically by the helper.

### Edge cases

- **Wiki returns API errors as 200 with `{"error": {...}}`** → check the response body, raise as transient if `error.code in {"maxlag", "ratelimited"}` else persistent.
- **Page deleted between recentchanges and parse** → `parse` returns `{"error": {"code": "missingtitle"}}` → skip silently.
- **Continuation tokens** → loop until no `continue` field in the response.
- **Title case sensitivity** → MediaWiki normalizes titles. Use the title as returned by the list/recentchanges API, not the user's input.

## Component: `urls` adapter

### Config

```yaml
adapter: urls
url: ""                  # ignored (kept for sources-table NOT NULL)
config:
  urls:
    - https://en.wikipedia.org/wiki/Elite_Dangerous
    - https://ed.tools/
    - https://inara.cz/elite/galaxy/
schedule: 7d
source_tier: vendor-doc
```

### Behavior

- Iterate `config.urls`. No discovery, no globs.
- For each URL, call `fetch_with_politeness` with the cached `HTTPCacheEntry` for that URL.
- On 304 → don't yield; preserve cache.
- On 200 → extract via trafilatura, yield Candidate; update cache with new etag/last-modified.
- Cursor: `{cache: {url: {etag, last_modified}}}`.

### Empty list

`urls: []` → fetch returns immediately with zero candidates and no errors. No-op poll.

## Politeness defaults

Configured in `~/.engram/config.yml` under `poller.<adapter>.request_interval_ms`. Per-source overrides in `config.request_interval_ms`.

| Adapter | Default | Sustained | Notes |
|---|---|---|---|
| `sitemap` | 1000 ms | 60/min | unchanged from v0.2 |
| `github-repo` | 100 ms | 600/min | depends on `$GITHUB_TOKEN`; one-shot warning if unset |
| `mediawiki-api` | 1500 ms | 40/min | below Fandom 60/min cap, well under WMF burst guideline |
| `urls` | 1000 ms | 60/min | same as sitemap |

User-Agent default: `engram-poller/0.3.0 (+<contact>)`. `<contact>` from `config.yml` `poller.http.contact`. If unset: `+engram (private deployment, set poller.http.contact in config.yml)`. Logs a one-time warning on poller startup if unset.

`fetch_with_politeness` honors:
- `Retry-After: <seconds>` and `Retry-After: <HTTP-date>` on 429 and 503 (one retry, then propagate).
- bare 5xx without `Retry-After`: backoff `min(5s, 2 * request_interval_ms / 1000)`, retry once.

## Incremental fetch invariant

**Promise:** after a source's first successful poll, subsequent polls fetch only pages where upstream has changed. Layered defense:

| Layer | Mechanism |
|---|---|
| 1. Network skip | Adapter signals: ETag, Last-Modified, recentchanges, git compare. |
| 2. Network strengthening | Both `If-None-Match` and `If-Modified-Since` sent when present. Either suffices server-side. |
| 3. Content dedup | Gate's `exact_dup` outcome catches anything that slips through layer 1+2. No DB write. |
| 4. Cursor durability | `sources.cursor` updated atomically with `last_polled_at`. Crash → retry from previous cursor. |

### Per-adapter realization

| Adapter | Steady-state cost (no upstream changes) |
|---|---|
| `sitemap` | 1 sitemap.xml fetch + N conditional GETs (all 304). Zero candidates yielded. |
| `github-repo` | 1 branch HEAD lookup. If `last_sha == head_sha` → 0 file fetches. |
| `mediawiki-api` | 1 `recentchanges` call returning empty list → 0 `parse` calls. |
| `urls` | N conditional GETs (all 304). Zero candidates yielded. |

### Cursor schemas

| Adapter | Cursor JSON |
|---|---|
| `sitemap` | `{"cache": {url: {etag, last_modified}}, "last_seen_at": "..."}` |
| `github-repo` | `{"last_sha": "..."}` |
| `mediawiki-api` | `{"last_rc_at": "ISO ts", "api_endpoint": "..."}` |
| `urls` | `{"cache": {url: {etag, last_modified}}}` |

(`sitemap` cursor schema migrates from v0.2's `{"etags": {url: etag}}` to the unified `cache` form. Migration is in-place: on first poll after upgrade, the adapter reads either shape and writes the new shape.)

## Tests

`tests/sources/test_http_politeness.py`:
- 429 with `Retry-After: 1` → sleep mocked → retry → success → returns response.
- 429 with `Retry-After: 60` → asserts mocked sleep was called with 60.
- 503 without `Retry-After` → backoff, retry once, then propagate.
- 200 with no cache headers → no sleep called.
- 304 → returns None.
- `If-None-Match` and `If-Modified-Since` both sent when cache supplies them.

`tests/sources/test_mediawiki_adapter.py`:
- First run: mock `list=allpages` returns 7 titles across pagination; assert 7 Candidates yielded with correct `<wiki>/wiki/<title>` URLs.
- First run with `max_pages_first_run=3` → 3 Candidates.
- Subsequent run: cursor populated, mock `list=recentchanges` returns 2 titles; assert 2 Candidates, no `allpages` call.
- Namespace filter: only configured namespaces are queried.
- Title glob: `exclude: ["File:*"]` filters out File: namespace titles even when API returns them.
- `apcontinue` pagination loops until exhausted.
- API error response (200 with `{"error": {"code": "maxlag"}}`) → classified as retryable, propagated.
- `parse` 404 (missingtitle) → page silently skipped, run continues.

`tests/sources/test_urls_adapter.py`:
- 3 URLs, all return 200 with body → 3 Candidates.
- Same 3 URLs second poll, all 304 → 0 Candidates, cache preserved.
- One URL changes etag → 1 Candidate, others skipped.
- Empty `urls: []` → 0 Candidates, no errors.

`tests/sources/test_incremental_fetch.py`:
- Sitemap, no upstream changes → second poll yields 0 Candidates, asserts conditional GETs were sent.
- Sitemap with `Last-Modified` only (no ETag) → second poll honors `If-Modified-Since` → 304 → skip.
- MediaWiki, 0 changes → second poll's `parse` call count == 0.
- MediaWiki, 1 change → second poll's `parse` call count == 1.
- URLs, all unchanged → second poll's body-fetch count matches len(urls), all 304.

`tests/integration/test_mediawiki_end_to_end.py`:
- Local httpd serves `/api.php` returning canned JSON for `query` and `parse`.
- `sources.add` for `mediawiki-api`, run `poll_one` → assert revision=1 entries ingested.
- Modify the canned `recentchanges` to report 1 changed page; run `poll_one` again → assert exactly 1 `superseded` event for that title.

## Worked example — Elite Dangerous

```python
# Source 1: Fandom wiki via MediaWiki API
sources.add(
    id="ed-fandom",
    name="Elite Dangerous Wiki (Fandom)",
    adapter="mediawiki-api",
    url="https://elite-dangerous.fandom.com",
    config={
        "namespaces": [0],
        "include": ["*"],
        "exclude": ["File:*", "User:*", "Category:*", "Template:*"],
        "max_pages_first_run": 500,
    },
    schedule="7d",
)

# Source 2: hand-picked reference pages (no sitemap)
sources.add(
    id="ed-references",
    name="Elite Dangerous reference pages",
    adapter="urls",
    url="",
    config={
        "urls": [
            "https://en.wikipedia.org/wiki/Elite_Dangerous",
            "https://ed.tools/",
        ],
    },
    schedule="7d",
)

sources.fetch_now("ed-fandom")
sources.fetch_now("ed-references")
```

**Pass criteria:**
- ed-fandom: ≥100 wiki pages ingested as `revision=1, is_current=1`, vault rendered under `030-research/`.
- ed-references: 2 pages ingested.
- Second poll (forced via `fetch_now`): zero new ingests, zero superseded (no upstream changes between runs).
- No `source_error` events; circuit breaker never trips.

## Files touched

**New:**
- `src/engram/poller/adapters/_http.py` — shared `fetch_with_politeness` + `AsyncRateLimiter` + cache types.
- `src/engram/poller/adapters/mediawiki_api.py` — adapter.
- `src/engram/poller/adapters/urls.py` — adapter.
- `tests/sources/test_http_politeness.py`
- `tests/sources/test_mediawiki_adapter.py`
- `tests/sources/test_urls_adapter.py`
- `tests/sources/test_incremental_fetch.py`
- `tests/integration/test_mediawiki_end_to_end.py`
- `tests/fixtures/mediawiki/allpages_response.json`
- `tests/fixtures/mediawiki/recentchanges_response.json`
- `tests/fixtures/mediawiki/parse_response.json`

**Edited:**
- `src/engram/poller/adapters/__init__.py` — eager-import the two new adapters.
- `src/engram/poller/adapters/sitemap.py` — refactor to call `fetch_with_politeness`; cursor migrates to unified `cache` shape.
- `src/engram/mcp_server/tools/sources.py` — `add` schema's `adapter` enum gains `mediawiki-api` and `urls`; `DEFAULT_SCHEDULE` gains entries for both.
- `src/engram/cli/eos_source.py` — `--adapter` choices gain the two new values.
- `src/engram/common/config.py` — `poller.http.contact`, `poller.mediawiki_api.request_interval_ms`, `poller.urls.request_interval_ms` config keys.
- `docs/mcp-tool-reference.md` — document new adapter values + their config shapes.
- `docs/setup.md` — note the contact config and the two new adapter examples.
- `README.md` — adapter table grows from 2 to 4 entries; bump status banner to v0.3.0-alpha.1 after merge.

## Estimated effort

About 4–6 hours wall-clock at the v0.2 task cadence:
1. Refactor `_fetch_one` → `_http.py` shared helper (with `Last-Modified` support and `Retry-After` handling) + tests. ~45 min.
2. Sitemap adapter migration to the new helper + cursor schema. ~20 min.
3. `urls` adapter + tests. ~30 min.
4. `mediawiki-api` adapter discovery (allpages + parse) + tests. ~75 min.
5. `mediawiki-api` adapter incremental path (recentchanges) + tests. ~45 min.
6. Integration test (end-to-end with fake api.php). ~45 min.
7. Wire into MCP enum, CLI choices, config, docs. ~30 min.
8. Live smoke test against ED Fandom + URL list. ~30 min.

## Open questions

None blocking. The cursor migration on `sitemap` (etags → cache) is the only piece of mild risk; mitigated by the adapter reading either shape on first poll after upgrade.

## Outcome — live smoke test (2026-05-06)

Both adapters validated against real Elite Dangerous sources after restarting the poller:

```
ed-fandom (mediawiki-api):
  url=https://elite-dangerous.fandom.com
  config={namespaces:[0], max_pages_first_run:100, exclude:["File:*","User:*","Category:*","Template:*"]}
  result: 100 live entries (cap reached), 0 errors, 0 superseded
  cursor: last_rc_at populated, api_endpoint=https://elite-dangerous.fandom.com/api.php

ed-references (urls):
  config={urls:["https://en.wikipedia.org/wiki/Elite_Dangerous", "https://ed.tools/"]}
  result: 2 live entries, 0 errors
  cursor: cache populated for both URLs (etag, last_modified)
```

Confirms:
- MediaWiki API endpoint is **not** Cloudflare-gated — the adapter walked Fandom's allpages and parsed every page successfully where the v0.2 sitemap adapter got 403.
- URL adapter handles arbitrary single-page targets (Wikipedia article, dashboard page).
- All ED content rendered into the vault under `030-research/`. Vault now has 286 files (Docker docs from v0.2 + Engram operational entries + ED entries).
- trafilatura logged "empty HTML tree" warnings on a few pages; the adapter's `or html` fallback ensured those pages still ingested with raw HTML body. Worth tracking but non-fatal.

The 100-page first-run cap was deliberately set low for the smoke; production would use the default 1000 or remove the cap. With the 1500ms rate limit, 100 pages took ~2.5 minutes; 1000 pages would take ~25 min.
