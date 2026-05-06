# MCP tool reference

All tools exposed by `engram-mcp`. Authoritative schemas live in code; this
is a quick reference.

## kb.*

- **`kb.write`** — Push content through the dedup gate. Returns
  `{outcome: 'new'|'exact_dup'|'near_dup', hash, merged_into?}`.
  Required: `body`. Optional: `title`, `source_url`, `source_tier`,
  `confidence`, `ttl_days`, `kind`.
- **`kb.get`** — Fetch by hash.
- **`kb.list`** — Recent entries, optionally filtered by `kind`.
- **`kb.contradictions`** — List unresolved (default) or all contradictions.
- **`kb.flag_contradiction`** — Mark two hashes as contradicting; emits a
  `contradicted` event.

## rag.*

- **`rag.query`** — Hybrid retrieval. Returns ranked hits with snippets,
  source_url, fetched_at, and combined score. Logs a `retrieved` event per hit
  (drives demand-driven staleness via the reactor).

## research.*

- **`research.fetch_url`** — Ingest a fetched URL's body through the gate.
  Caller does the actual HTTP/Firecrawl call; this tool stamps provenance and
  dedups.
- **`research.search_web`** — Stub. Wire to Tavily/SearXNG.
- **`research.fetch_arxiv`** — Stub. Wire to arxiv CLI / Semantic Scholar.

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

Curated polled feeds. Each source picks an adapter (`sitemap` or `github-repo`),
a URL, optional include/exclude globs, and a schedule. The poller daemon walks
due sources, runs each candidate through the dedup gate, and emits new revisions
when content changes (`superseded` outcome).

- **`sources.add`** — Register a new polled source. Required: `id`, `name`,
  `adapter`, `url`. Optional: `config` (adapter-specific globs / branch),
  `schedule` (default per-adapter: `7d` for sitemap, `1d` for github-repo),
  `source_tier` (default `vendor-doc`), `paused`.
- **`sources.list`** — All sources with state. Filters: `paused_only`,
  `with_errors`.
- **`sources.get`** — Full record by id (cursor truncated if large).
- **`sources.set`** — Update one or more fields on an existing source.
  `config` replaces existing wholesale (currently — see follow-ups).
- **`sources.remove`** — Delete a source row. Does NOT tombstone its content;
  `kb.tombstone` content first if you want a clean removal.
- **`sources.fetch_now`** — Force immediate poll on next 60s tick (sets
  `next_poll_at = NULL`).

## Adding a tool

1. In `src/engram/mcp_server/tools/<ns>.py`, add a handler to the dict
   returned by `register(conn)`.
2. Make sure the `register` function is imported in `tools/__init__.py`.
3. Restart the MCP server (Claude Code re-launches it on next session).
