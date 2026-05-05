# Architecture

## Principle

The event log is canonical. Everything else (the vault, the FTS5 index, the
vec0 embeddings, the entity graph) is a **materialized view**. If any of those
get corrupted you can rebuild them from the log.

## Data flow

```
        ┌─────────────────────────────────────┐
        │         Claude Code (kernel)        │
        └──────────────────┬──────────────────┘
                           │ MCP stdio (one server, namespaced tools)
                           ▼
        ┌─────────────────────────────────────┐
        │  Event Log (SQLite, append-only)    │
        │  ingested · merged · contradicted   │
        │  retrieved · stale · goal · edit    │
        └──┬───────┬───────┬──────────────────┘
           │       │       │
           ▼       ▼       ▼
       Vault   RAG view  Reactor
       projector (vec+FTS) (handlers)
           │                │
           ▼                ▼
       ┌─────────┐     ┌─────────────┐
       │ Obsidian│────►│ Watcher     │
       │ (human) │     │ (edits→log) │
       └─────────┘     └─────────────┘
```

## Components

### Event log (`schema/001_initial.sql`, `src/engram/log.py`)

One table, append-only. Every state change is an event. Daemons keep cursors
in `daemon_cursors`. Replaying from event 0 reconstructs the system.

### Dedup gate (`src/engram/dedup.py`)

The single entry point for any content write. Three outcomes:

- `exact_dup` — SHA-256 collision, no-op
- `near_dup` — cosine similarity ≥ 0.92 against an existing embedding, merge
- `new` — inserted, `ingested` event emitted

Near-dup check at write-time is best-effort (requires a query embedding).
The reactor does a post-hoc near-dup check after embedding, emitting a `merged`
event if needed. This catches dupes whether or not the caller pre-embedded.

### RAG (`src/engram/rag/`)

- `chunk.py` — markdown-aware splitter, falls back to sliding token windows.
- `embed.py` — sentence-transformers, lazy-loaded, normalized float32.
- `query.py` — vec0 + FTS5 in parallel, fused with RRF, ranked by:
  `rrf_score × confidence × source_tier_weight × recency_decay`.

The retrieval log writes a `retrieved` event per hit; the reactor uses these
to mark stale entries (demand-driven refresh).

### MCP server (`src/engram/mcp_server/`)

One server. Tools namespaced as `<ns>.<verb>`:

- `kb.write`, `kb.get`, `kb.list`, `kb.contradictions`, `kb.flag_contradiction`
- `rag.query`
- `research.fetch_url`, `research.search_web`, `research.fetch_arxiv`
- `playbook.list`, `playbook.run`, `playbook.summarize`
- `goals.set`, `goals.list`, `goals.resolve`

Holds one long-lived sqlite connection. Tool handlers run in a worker thread
(via `asyncio.to_thread`) so a slow embed doesn't block the stdio loop.

### Projector (`src/engram/projector/`)

Tails the log for `ingested` and `merged` events. Renders content rows to
markdown via per-kind renderers. Writes `vault_state(path, hash, body, ts)` so
the watcher knows what was the last-rendered version of each file.

### Watcher (`src/engram/watcher/`)

`watchdog` over the vault. On a debounced modify:
- If the path is known to `vault_state`: diff against `rendered_body`, update
  `content.body`, refresh `vault_state.rendered_body`, emit `vault_edit`.
- If unknown: treat as inbox drop, run through `dedup.gate(kind='kb', actor='human')`.

This is the sync-back path. Manual edits in Obsidian become authoritative.

### Reactor (`src/engram/reactor/`)

Tails the log. Two handlers wired:

- `on_ingested` — embed the new content, write to `embeddings`, post-hoc near-dup
  check that may emit `merged`.
- `on_retrieved` — if a retrieved entry is past 80% of its TTL, bump
  `staleness_score` and emit `refresh_requested`.

Add handlers by registering them in `handlers.HANDLERS`.

## Confidence model

```
confidence = source_tier_weight × recency_decay × stored_confidence

recency_decay = 0.5 ** (age_days / half_life_days)
```

Tier weights configurable in `config.yml`. Half-life defaults to 365 days; per-
entry `ttl_days` overrides it for volatile topics. The retrieval ranker uses
this so ranking is automatically correct without manual tuning.

## Failure modes and recoveries

| Failure | Recovery |
|---|---|
| Vault file accidentally deleted | Projector renders it again on next ingest/merge tick (it's just a view). |
| FTS5 / embeddings corrupted | Drop the tables; replay log from 0 (handlers re-embed and re-index). |
| Watcher crashed during human edit | Edit becomes authoritative on watcher restart; no event recorded. Live with it or replay vault → log via a one-shot reconciliation script. |
| Wrong merge | Manually clear `tombstoned`, emit a corrective event. The log preserves the bad merge for audit. |
