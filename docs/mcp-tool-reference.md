# MCP tool reference

> Part of the [engram documentation](README.md).

All tools exposed by `engram-mcp`. Authoritative schemas live in code; this
is a quick reference.

## kb.*

- **`kb.write`** — Push content through the dedup gate. Returns
  `{outcome: 'new'|'exact_dup'|'near_dup'|'superseded'|'supersede_blocked', hash, merged_into?}`.
  Required: `body`. Optional: `title`, `source_url`, `source_tier`,
  `confidence`, `ttl_days`, `kind`.
- **`kb.get`** — Fetch by hash.
- **`kb.list`** — Recent entries, optionally filtered by `kind`.
- **`kb.tombstone`** — Soft-delete content by hash (sets `tombstoned = 1`; the row
  stays in the log and superseded revisions remain queryable).
- **`kb.contradictions`** — List unresolved (default) or all contradictions.
- **`kb.flag_contradiction`** — Mark two hashes as contradicting; emits a
  `contradicted` event.

## rag.*

- **`rag.query`** — Hybrid retrieval (dense + BM25, RRF-fused, confidence-ranked).
  Returns a calibration `verdict` (`STRONG`/`WEAK`/`NONE`) plus ranked `results`
  (snippet, `source_url`, `fetched_at`, score). Optional: `token_budget`, `level`
  (`snippet`/`full`), `since`. Logs a `retrieved` event per hit (drives
  demand-driven staleness via the reactor).
- **`rag.cite`** — Record that an answer was grounded in specific content hashes;
  weights those entries up in later ranking (citation-weighted retrieval).

## research.*

- **`research.fetch_url`** — Ingest a fetched URL's body through the gate.
  Caller does the actual HTTP fetch; this tool stamps provenance and
  dedups.
- **`research.ingest_url`** — Fetch a URL, extract its body, and store it through
  the gate. The sink for `search_web` / `fetch_arxiv` keepers.
- **`research.search_web`** — Self-hosted web search: SearXNG → parallel
  fetch → trafilatura extraction → cross-encoder rerank → top-k. Returns ranked
  URLs with relevance score, snippet, and extracted body length; pass the
  keepers to `research.ingest_url` to store them. Requires
  `research.searxng_url` to be configured. Optional: `k`, `max_candidates`.
- **`research.fetch_arxiv`** — arXiv API search over titles + abstracts,
  reranked by cross-encoder. Multi-word queries are auto-quoted (exact phrase)
  by default; set `quote_phrase=false` for broad keyword OR-search. Returns
  title, abstract, authors, published date, and PDF URL — pass the `pdf_url` to
  `research.ingest_url` to store the full paper. Optional: `k`, `rerank`,
  `quote_phrase`.

## playbook.*

- **`playbook.list`** — Lists `scratch/*.ipynb` (Jupyter) and `curated/*.py` (Marimo).
- **`playbook.run`** — Headless execution. Outputs land in
  `playbooks/runs/<run_id>/`. Returns run_id, exit_code, and tails of stdout/stderr.
- **`playbook.summarize`** — Push a summary string into the KB as
  `kind=playbook-summary`. The full notebook stays in `run_dir`; only the
  summary enters the vault.

## goals.*

- **`goals.set`** — Create or update an active investigation. Used by the
  agent to declare intent at session start (or by the human via Obsidian).
- **`goals.list`** — Active goals, ordered by priority.
- **`goals.resolve`** — Mark resolved.

## sources.*

Curated polled feeds. Each source picks one of four adapters, plus a URL,
optional filters, and a schedule. The poller daemon walks due sources, runs
each candidate through the dedup gate, and emits new revisions when content
changes (`superseded` outcome).

- **`sources.add`** — Register a new polled source. Required: `id`, `name`,
  `adapter`, `url`. Optional: `config` (adapter-specific), `schedule`
  (default per adapter), `source_tier` (default `vendor-doc`), `paused`.
- **`sources.list`** — All sources with state. Filters: `paused_only`,
  `with_errors`.
- **`sources.get`** — Full record by id (cursor truncated if large).
- **`sources.set`** — Update one or more fields on an existing source.
  `config` replaces existing wholesale (currently — see follow-ups).
- **`sources.remove`** — Delete a source row. Does NOT tombstone its content;
  `kb.tombstone` content first if you want a clean removal.
- **`sources.fetch_now`** — Force immediate poll on next 60s tick (sets
  `next_poll_at = NULL`).
- **`sources.health`** — Deterministic per-source health view: liveness, content
  counts, duplicate ratio, and a derived status (`ok` / `overdue` / `paused` /
  `erroring`).

### Adapter types

| Adapter | `url` | Default schedule | Required `config` keys |
|---|---|---|---|
| `sitemap` | `<...>/sitemap.xml` | `7d` | optional `include[]`, `exclude[]` (URL globs) |
| `github-repo` | `https://github.com/<org>/<repo>` | `1d` | optional `branch` (default `main`), `include[]`, `exclude[]` (path globs); auth: `$GITHUB_TOKEN` → `gh auth token` (keyring) → anonymous |
| `mediawiki-api` | wiki root (e.g. `https://elite-dangerous.fandom.com`) | `7d` | optional `namespaces[]` (default `[0]`), `include[]`/`exclude[]` (title globs), `max_pages_first_run` (default 1000) |
| `urls` | `""` (ignored) | `7d` | required `urls[]` |

All adapters accept an optional `config.request_interval_ms` to override the
per-adapter default rate limit.

## session.*

- **`session.prime`** — Return a priming block (active goals + recent
  high-confidence knowledge) to seed a new session. Call at session start.
- **`session.reflect`** — Return a deterministic reflection brief: unresolved
  contradictions, stale high-value entries, and idle active goals. No model calls.

## Adding a tool

1. In `src/engram/mcp_server/tools/<ns>.py`, add a handler to the dict
   returned by `register(conn)`.
2. Make sure the `register` function is imported in `tools/__init__.py`.
3. Restart the MCP server (Claude Code re-launches it on next session).
