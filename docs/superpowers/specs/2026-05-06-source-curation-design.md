# Source curation — design spec

**Date:** 2026-05-06
**Status:** approved (brainstorm), pending implementation plan
**Target version:** v0.2.0-alpha (next minor)

## Problem

Engram today ingests one URL at a time via `research.ingest_url`. There is no way to declare "keep this set of pages curated" — no recurring poll, no update detection, no supersede-on-change. The dedup gate detects when bytes change but treats every revision as a new entry, accumulating stale doc versions indefinitely. To passively curate something like "the latest Docker docs for Linux," the operator currently has to wrap a shell loop around the MCP and remember to re-run it, with no automation of update detection or stale-revision cleanup.

## Goals

1. Declare a source once via MCP; the system polls it on schedule.
2. When a page changes upstream, the new revision becomes current; the old revision is preserved (queryable via `as_of`) but no longer rendered to the vault.
3. Per-source URL/path filtering so "Linux only" works without ingesting the rest of the site.
4. Two adapter types in v1: `sitemap` (covers any site that publishes one) and `github-repo` (covers any public docs-as-markdown repo).
5. Worked example: `https://docs.docker.com/sitemap.xml` with `include: ["*/engine/*", "*/desktop/install/linux*"]` ingests the relevant Linux pages and stays current.

## Non-goals (v1)

- RSS/Atom feeds (next adapter; same plumbing).
- Saved-query recurring search (same plumbing, different adapter).
- Crawling sites without a sitemap (future "site-crawl" adapter).
- Authenticated sources beyond a single GitHub token in env.
- Cross-source content dedup (the existing near-dup gate handles incidental overlap).
- Browsable revision history *in the vault* (revisions are in DB and queryable; not rendered).

## Design decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Adapters in v1 | sitemap + github-repo | Covers ~all official tool docs. RSS deferred. |
| Update semantics | Version chain (revisions kept) | Operator may need old docs for old tools. |
| Polling cadence | Per-source schedule, sane defaults | Docker docs ≠ Hacker News. |
| Source storage | DB table, MCP-managed | Keeps Engram's "config-as-data" thesis. |
| Filter language | Uniform glob include/exclude | Mental model is the same across adapters. |
| Vault projection | Current revision only | Vault stays clean; history queryable via MCP. |
| Where polling runs | Standalone `engram-poller` daemon | Decoupled blast radius from reactor. |

## Architecture

```
                ┌─────────────────────────────────────┐
                │  sources table (db.sqlite)          │
                │  id · adapter · url · filters       │
                │  schedule · next_poll_at · cursor   │
                │  last_etag · errors · paused        │
                └──────────────────┬──────────────────┘
                                   │  reads
                                   ▼
                          ┌────────────────┐
                          │ engram-poller  │ wakes every 60s, picks
                          │   (daemon)     │ due rows, dispatches
                          └────────┬───────┘
                                   │
                ┌──────────────────┼──────────────────┐
                ▼                  ▼                  ▼
         ┌──────────────┐  ┌──────────────┐   (more adapters
         │ sitemap      │  │ github-repo  │    later: rss, etc)
         │ adapter      │  │ adapter      │
         └──────┬───────┘  └──────┬───────┘
                │                 │
                │ candidate URLs / paths,
                │ filtered, fetched, extracted
                ▼
         ┌──────────────────────────────────┐
         │ dedup.gate(source_url=...)       │ existing gate, plus new
         │ outcomes:                        │ outcome `superseded`
         │  new | exact_dup | near_dup |    │ when same source_url
         │  superseded                      │ already has live entry
         └──────────────┬───────────────────┘
                        │ event(s)
                        ▼
                  [Event Log]
                        │
              ┌─────────┼─────────┐
              ▼         ▼         ▼
          Reactor  Projector  Watcher  (existing)
```

### Per-poll-cycle flow

1. Poller scans `sources` for rows where `paused=0 AND (next_poll_at IS NULL OR next_poll_at <= now())`.
2. For each due source: instantiate the named adapter, pass it the row.
3. Adapter yields `(source_url, body, fetched_at, metadata)` tuples and updates its cursor in-place on the source row.
4. Each tuple goes through `dedup.gate(kind='research', source_tier=<source.source_tier>, source_url=<url>, source_id=<source.id>, ...)`.
5. Gate's new logic: when `source_url` matches an existing **live** entry with a different content hash → emit `superseded`, set old row's `is_current=0`, insert new row with `is_current=1` and incremented `revision`.
6. Projector handles `superseded` like a re-render: the new content overwrites the vault file at the old canonical path.

## Schema changes — `schema/002_sources_and_revisions.sql`

### New `sources` table

```sql
CREATE TABLE sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    adapter         TEXT NOT NULL,                -- 'sitemap' | 'github-repo'
    url             TEXT NOT NULL,
    config          TEXT NOT NULL DEFAULT '{}',   -- JSON, adapter-specific
    schedule        TEXT NOT NULL,                -- '7d', '1d', '6h'
    source_tier     TEXT NOT NULL DEFAULT 'vendor-doc',
    paused          INTEGER NOT NULL DEFAULT 0,
    next_poll_at    TEXT,
    last_polled_at  TEXT,
    last_success_at TEXT,
    cursor          TEXT,                         -- adapter-specific (etags, git sha)
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sources_due ON sources(next_poll_at) WHERE paused = 0;
```

### `content` table additions

```sql
ALTER TABLE content ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE content ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1;
ALTER TABLE content ADD COLUMN superseded_by TEXT REFERENCES content(hash);
ALTER TABLE content ADD COLUMN source_id TEXT REFERENCES sources(id);

CREATE INDEX idx_content_url_current ON content(source_url, is_current);
CREATE INDEX idx_content_source ON content(source_id, is_current);
```

### Invariants

- For any given `source_url`, exactly one row has `is_current=1` (live version), unless tombstoned (zero).
- `revision` increments per source_url; first ingest is rev 1.
- `superseded_by` on an old revision points to the hash that replaced it; latest revision in a chain has `superseded_by=NULL`.
- `tombstoned=1` and `is_current=0` are distinct: tombstoned = manually retired; not-current = older revision, still queryable for `as_of`.

### `as_of` queries

Reconstructed from the events log (no extra columns). Algorithm: for a given `source_url` and `as_of` timestamp, find the latest `ingested` or `superseded` event with that `source_url` whose `created_at <= as_of` and return the corresponding `hash_new` (or original hash for `ingested`). Slower for repeated queries; acceptable at v1 since `as_of` is rare. Optimization (`valid_from`/`valid_to` columns) deferred unless usage justifies it.

### Schedule grammar

The `schedule` field is a duration string parsed as `<int><unit>` where unit ∈ `{s, m, h, d, w}` (seconds, minutes, hours, days, weeks). Examples: `30m`, `6h`, `1d`, `7d`, `2w`. No cron expressions in v1; absolute clock-time scheduling is deferred. Implementation: `parse_interval(s) -> timedelta`, errors on unknown units.

### New event type

```
superseded | dedup gate | {hash_old, hash_new, source_url, revision}
```

## Components

### New: poller daemon (`src/engram/poller/`)

```
src/engram/poller/
├── __init__.py
├── __main__.py         # entry point: engram-poller
├── poller.py           # main loop
└── adapters/
    ├── __init__.py     # registry + Adapter protocol + Candidate dataclass
    ├── sitemap.py
    └── github_repo.py
```

Main loop pseudocode:

```python
async def run():
    conn = open_db()
    while not stop:
        due = conn.execute(
            "SELECT * FROM sources WHERE paused=0 "
            "AND (next_poll_at IS NULL OR next_poll_at <= ?)",
            (utcnow_iso(),)
        ).fetchall()
        for src in due:
            try:
                await poll_one(conn, src)
            except Exception as e:
                record_error(conn, src, e)
        await asyncio.sleep(60)
```

### Adapter interface

```python
class Adapter(Protocol):
    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        """Yield Candidate(...) for each new/changed page.
        Adapter manages its own cursor (reads source['cursor'],
        mutates source row to write new cursor before returning)."""

@dataclass
class Candidate:
    source_url: str
    body: str
    title: str | None
    fetched_at: str
    metadata: dict   # adapter-specific (git sha, etag, etc.)

ADAPTERS = {
    'sitemap':     SitemapAdapter(),
    'github-repo': GitHubRepoAdapter(),
}
```

Glob filtering (`include`/`exclude` from `source.config`) lives **inside the adapter** so each can short-circuit efficiently.

### Sitemap adapter

- Fetch `sitemap.xml`; follow sitemap-index files to sub-sitemaps.
- Apply include/exclude globs to each `<loc>`.
- For surviving URLs: HTTP GET with `If-None-Match: <etag>` (per-URL etags stored in `cursor` JSON). Parse with trafilatura. Yield `Candidate`.
- New `cursor`: `{etags: {url: etag}, last_seen_at: timestamp}`.
- Polite: 1 req/sec, configurable.

### GitHub-repo adapter

- Read `repo` (`docker/docs`) and `branch` (default `main`) from config.
- If `cursor.last_sha` exists: `GET /repos/{repo}/compare/{last_sha}...{branch}` → list of changed files.
- Otherwise: walk the tree at HEAD.
- Apply include/exclude globs to file paths.
- For each surviving markdown file: fetch raw content. Yield `Candidate` with `source_url = https://github.com/{repo}/blob/{sha}/{path}`.
- New `cursor`: `{last_sha: <head>}`.
- Token: `$GITHUB_TOKEN` from env. Without it, anonymous (60 req/hr — fine for a single small repo, daily cadence).

### Dedup-gate change (`src/engram/dedup.py`)

New supersede branch, runs **before** near-dup, only when caller passed `source_url` AND a live entry already exists for that URL. Pseudocode in components section above.

`kb.write` callers without a `source_url` are unaffected.

### Vault path stability for sourced content

For content with a non-null `source_url`, the projector derives the vault filename from the **URL** (slug of the URL's path tail), **not** the content hash. This guarantees successive revisions of the same source_url render to the same path on disk. Filename format: `<slug>-<source_id_short>.md` where `<slug>` is the path-tail slugified and `<source_id_short>` is the first 8 chars of `source_id` (disambiguates if two sources happen to have URLs with the same path tail). Content without a source_url keeps the existing `<title-slug>-<hash_short>.md` scheme.

### Projector change (`src/engram/projector/projector.py`)

New handler for `superseded` events — render the new hash to the vault path the old hash occupied, then mark the old `vault_state` row dead:

```python
def on_superseded(event):
    hash_old = event.payload['hash_old']
    hash_new = event.payload['hash_new']
    old_vault = vault_state_for(hash_old)
    new_content = content_get(hash_new)
    rendered = render(new_content, kind=new_content.kind)
    write_atomic(old_vault.path, rendered)
    update_vault_state(path=old_vault.path, hash=hash_new, body=rendered)
    delete_vault_state_row(hash_old)
```

Result: same file path on disk, new content. Obsidian sees one file, updated body. Old revision lives in DB only.

## MCP surface — `sources.*` namespace

Six tools.

### `sources.add`
```
inputs:
  id          string  required, kebab-case (e.g. "docker-docs-linux")
  name        string  required
  adapter     enum    required, "sitemap" | "github-repo"
  url         string  required
  config      object  optional (adapter-specific globs/branch/etc.)
  schedule    string  optional (default per adapter)
  source_tier enum    optional (default "vendor-doc")
  paused      bool    optional (default false)
returns: {id, next_poll_at}
```

### `sources.list`
```
inputs:
  paused_only bool  optional
  with_errors bool  optional
returns: [{id, name, adapter, url, schedule, paused,
           last_polled_at, last_success_at,
           error_count, last_error, next_poll_at}]
```

### `sources.get`
```
inputs:  {id}
returns: {full row, cursor truncated if large}
```

### `sources.remove`
Removes the source row. Does NOT tombstone its content (use `kb.tombstone` explicitly to purge).
```
inputs:  {id}
returns: {removed: true}
```

### `sources.fetch_now`
Immediate poll, ignoring schedule.
```
inputs:  {id}
returns: {triggered: true, run_id}
```

### `sources.set`
Update one or more fields on an existing source. Pause/resume = `paused: true|false`.
```
inputs:
  id          string  required
  paused      bool    optional
  schedule    string  optional
  config      object  optional (replaces existing wholesale)
  source_tier string  optional
returns: {updated_fields: [...]}
```

### CLI mirror — `bin/eos-source`

Same six operations, for shell use without going through MCP:

```bash
eos-source add <id> --adapter sitemap --url ... --include '*/engine/*'
eos-source list [--with-errors]
eos-source fetch-now <id>
eos-source set <id> --paused
eos-source remove <id>
```

### New event types

```
source_polled  | poller | {source_id, candidates_seen, ingested, superseded, errors}
source_error   | poller | {source_id, error, retryable}
source_circuit_broken | poller | {source_id, error_count}
```

## Configuration & defaults

### Per-adapter defaults

| Adapter | `schedule` | `source_tier` | Required config |
|---|---|---|---|
| `sitemap`     | `7d` | `vendor-doc` | `url` (sitemap.xml) |
| `github-repo` | `1d` | `vendor-doc` | `url` (form `github.com/org/repo`) |

### Polite-fetch defaults (`~/.engram/config.yml` under `poller:`)

```yaml
poller:
  sitemap:
    request_interval_ms: 1000
  github_repo:
    request_interval_ms: 100
  http:
    user_agent: "engram/0.1.x (+source-poller)"
    timeout_seconds: 30
```

### Secrets

- `GITHUB_TOKEN` — optional, in `~/.engram/.env`. Without it, github-repo runs anonymous (60 req/hr).
- No other new env vars in v1.

## Error model

| Class | Examples | Response |
|---|---|---|
| **Transient** | HTTP 5xx, network timeout, DNS | Retry exp-backoff up to 3× per URL within the run. Skip on persistent failure. `source_error retryable=true`. |
| **Persistent** | HTTP 4xx, parse error, schema mismatch | No retry within run. `source_error retryable=false`. Source stays active; next scheduled poll re-tries. |
| **Circuit-break** | `error_count >= 5` consecutive failed runs | Set `paused=1`, emit `source_circuit_broken`. Manual `sources.fetch_now` resets the counter. |

### Atomicity

Per poll run, all state updates happen in **one transaction at the end**: dedup-gate effects, cursor update, `last_polled_at`/`last_success_at`/`next_poll_at`/`error_count`. Daemon crash mid-poll → next tick re-processes from previous cursor; gate's `exact_dup` absorbs duplicate writes. Idempotent re-processing, not at-most-once.

## Observability

### Daily-digest playbook addition

New section:

```
## Source curation
- ✓ docker-docs-linux: 7 new, 2 superseded (last poll 04:12, next 2026-05-13 04:00)
- ✓ k8s-docs:         0 changes (last poll 04:14, next 2026-05-13 04:00)
- ⚠ pytorch-docs:     5 errors (sitemap returned 503; retrying tomorrow)
- ⛔ flask-blog:       circuit-broken (paused after 5 consecutive failures)
```

Sourced from `events` filtered by type `source_polled` / `source_error` / `source_circuit_broken` over the digest window, plus `sources` table state.

## Testing

### Unit tests (`tests/sources/`)

- `test_dedup_supersede.py` — gate returns `superseded` when source_url matches a live row with different bytes; revision increments; old row's `is_current=0`; `superseded_by` set; `superseded` event emitted.
- `test_dedup_no_supersede.py` — gate returns `new` for novel source_url; `exact_dup` for identical bytes at same source_url.
- `test_sitemap_adapter.py` — fixture sitemap.xml + mock HTTP; verify include/exclude filtering; verify ETag respected.
- `test_github_adapter.py` — fixture compare-API response; verify only changed files walked.
- `test_glob_filter.py` — covers `**`, mixed include/exclude (exclude wins on conflict).
- `test_error_classification.py` — 5xx is retryable, 404 is persistent, network timeout is retryable.
- `test_circuit_break.py` — 5 consecutive failures set `paused=1` and emit event.

### Integration test (`tests/integration/test_poller_end_to_end.py`)

Single test, full daemon + temp DB:

1. `sources.add` for a fixture sitemap (served from `tmp_path` via `http.server`).
2. Step poller; assert N candidates ingested as `revision=1, is_current=1`; vault files written by projector.
3. Modify one fixture file; bump its `<lastmod>`.
4. Step poller again; assert: one `superseded` event; old hash has `is_current=0, superseded_by=<new>`; vault file overwritten; chain has 2 revisions for that URL; `kb.list` returns only the current one.
5. `rag.query` for a term in the new content returns the new hash; with `as_of=<between revisions>`, returns the old hash.

### Fixtures

- `tests/fixtures/sitemap_minimal.xml` — 3 URLs, 2 matching a Linux glob.
- `tests/fixtures/github_compare_response.json` — captured GitHub API response.
- `tests/fixtures/docs_v1/` and `tests/fixtures/docs_v2/` — markdown corpora the sitemap test serves between runs to simulate updates.

### Out of scope for v1 testing

- Multi-day stability tests.
- Adversarial input (malicious sitemap, oversized pages).
- Cross-source dedup correctness at scale.

## Worked example — first source: Docker docs (Linux)

```python
sources.add(
    id="docker-docs-linux",
    name="Docker Docs (Linux)",
    adapter="sitemap",
    url="https://docs.docker.com/sitemap.xml",
    config={
        "include": ["*/engine/*", "*/desktop/install/linux*"],
        "exclude": ["*/manuals/desktop/install/(mac|windows)*"],
    },
    schedule="7d",
)
sources.fetch_now("docker-docs-linux")
# Wait for the run, then:
sources.list()                      # last_success_at populated, error_count=0
kb.list(kind="research", limit=5)   # Docker docs entries
ls ~/.engram/vault/030-research/    # rendered markdown
```

**Pass criteria:** ≥30 pages ingested, no errors, all `revision=1, is_current=1`, vault populated.

## Files touched

**New:**
- `src/engram/poller/{__init__,__main__,poller}.py`
- `src/engram/poller/adapters/{__init__,sitemap,github_repo}.py`
- `schema/002_sources_and_revisions.sql`
- `systemd/engram-poller.service`
- `bin/eos-source`
- `tests/sources/*`, `tests/integration/test_poller_end_to_end.py`, `tests/fixtures/*`

**Edited:**
- `src/engram/dedup.py` — supersede branch.
- `src/engram/projector/projector.py` — `superseded` event handler.
- `src/engram/mcp_server/tools/__init__.py` — register new namespace.
- `src/engram/mcp_server/tools/sources.py` — new file, six tools.
- `pyproject.toml` — add `engram-poller` console script.
- `playbooks/scratch/daily-digest.ipynb` — Source curation section.

## Open questions

None blocking. The only deferred decision (`as_of` query path: events-walk vs. valid_from/valid_to columns) is reversible and can be revisited if usage shows the perf hit matters.
