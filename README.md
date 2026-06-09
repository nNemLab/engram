# Engram

**A personal knowledge platform built around an append-only event log, projected into an Obsidian vault, accessed by agents over MCP.**

> **Status:** `v0.3.0-alpha.1` — dev build. APIs, schema, and on-disk layout may change without migration paths between alpha releases.

---

## What it is

Engram is a single-user knowledge system designed to be the long-term memory layer for an LLM agent (Claude Code in particular, but any MCP-capable client). It treats:

- **the event log as canonical** — every state change is an immutable event in SQLite,
- **the Obsidian vault as a projection** — markdown files are a materialized view, not source of truth,
- **the human and the agent as peers** — both write through the same dedup gate, both edit the same content.

If the FTS index, vector index, or vault gets corrupted, you replay the log and rebuild them. The log is the only thing you have to back up.

## What it is not

- **Not multi-user.** No auth, no concurrency model beyond SQLite's WAL.
- **Not cloud-hosted.** Runs entirely on your workstation. Self-hosted research uses a local SearXNG; embeddings run on CPU by default.
- **Not a vector database.** Hybrid retrieval over `sqlite-vec` + FTS5; RRF fused; ranked by source-tier × recency × confidence. If you need a real vector DB at scale, this is the wrong project.
- **Not a Claude API wrapper.** It exposes tools to a kernel (Claude Code or any MCP client). The kernel does the reasoning; Engram is storage and retrieval.

## Architecture

```
        ┌─────────────────────────────────────┐
        │         Claude Code (kernel)        │
        └──────────────────┬──────────────────┘
                           │ MCP stdio (one server, namespaced tools)
                           ▼
        ┌─────────────────────────────────────┐
        │  Event Log (SQLite, append-only)    │
        │  ingested · merged · superseded     │
        │  contradicted · retrieved · stale   │
        │  goal · edit · source_polled        │
        └──┬───────┬───────┬───────┬──────────┘
           │       │       │       │
           ▼       ▼       ▼       ▼
       Vault   RAG view  Reactor  Poller
       projector (vec+FTS) (handlers) (sources)
           │                │       │
           ▼                ▼       ▼
       ┌─────────┐     ┌─────────────┐  ┌──────────────┐
       │ Obsidian│────►│ Watcher     │  │ Adapters:    │
       │ (human) │     │ (edits→log) │  │ sitemap,     │
       └─────────┘     └─────────────┘  │ github-repo  │
                                        └──────────────┘
```

Every content write goes through `dedup.gate()` and produces an `ingested` event. The reactor embeds and post-checks for near-dups. The projector renders content rows to the vault. The watcher tails the vault filesystem so manual edits in Obsidian become authoritative.

Long form: [docs/architecture.md](docs/architecture.md).

## Major components

### Event log (`schema/001_initial.sql`, `src/engram/log.py`)
One append-only `events` table. Each daemon keeps a cursor in `daemon_cursors` and replays forward. Twelve event types defined; see [docs/event-log-schema.md](docs/event-log-schema.md) for the full taxonomy. The log is canonical — replaying from event 0 reconstructs the system.

### Dedup gate (`src/engram/dedup.py`)
The single entry point for any content write. Returns one of:
- `exact_dup` — SHA-256 collision, no-op.
- `superseded` — same `source_url` already has a live entry with different bytes; old row's `is_current=0`, new row inserted with bumped `revision`, `superseded` event emitted. The vault file overwrites in place.
- `near_dup` — cosine similarity ≥ 0.92 against an existing embedding, merge.
- `new` — inserted, `ingested` event emitted.

Near-dup at write-time requires a query embedding and is best-effort; the reactor does a post-hoc near-dup check after embedding to catch cases the caller didn't pre-embed.

### RAG (`src/engram/rag/`)
Hybrid retrieval. `chunk.py` splits markdown by structure with a sliding-window fallback. `embed.py` wraps `sentence-transformers/all-MiniLM-L6-v2` (384-dim, CPU-friendly; swappable via `config.yml`). `query.py` runs `vec0` ANN and FTS5 in parallel, fuses with Reciprocal Rank Fusion, and ranks by `rrf_score × confidence × source_tier_weight × recency_decay`. Each hit emits a `retrieved` event so the reactor can mark stale entries on demand.

### MCP server (`src/engram/mcp_server/`)
One stdio server exposing six tool namespaces. Tool handlers run in a worker thread (`asyncio.to_thread`) so a slow embed doesn't block the stdio loop. Holds one long-lived SQLite connection.

| Namespace | Tools |
|---|---|
| `kb` | `write`, `get`, `list`, `tombstone`, `contradictions`, `flag_contradiction` |
| `rag` | `query` |
| `research` | `search_web`, `fetch_url`, `ingest_url`, `fetch_arxiv` |
| `playbook` | `list`, `run`, `summarize` |
| `goals` | `set`, `list`, `resolve` |
| `sources` | `add`, `list`, `get`, `set`, `remove`, `fetch_now` |

Full reference: [docs/mcp-tool-reference.md](docs/mcp-tool-reference.md).

### Projector (`src/engram/projector/`)
Tails the log for `ingested`, `merged`, and `superseded` events. Renders content rows to markdown via per-kind renderers (`kb`, `episode`, `entity`, `research`, `playbook-summary`). For sourced content (non-null `source_url` + `source_id`), the vault filename is URL-derived so revisions overwrite the same file in place. Records rendered bytes in `vault_state` so the watcher can diff against them. Tombstoned content gets its vault file deleted.

### Watcher (`src/engram/watcher/`)
`watchdog` over the vault. On a debounced modify:
- If the path is in `vault_state`: diff against `rendered_body`, update `content.body`, emit a `vault_edit` event. The human's body becomes authoritative.
- If unknown: treat as inbox drop, run through the dedup gate as `kind='kb', actor='human'`.

### Reactor (`src/engram/reactor/`)
Tails the log. Two handlers wired:
- `on_ingested` — embed the new content, write to `embeddings`, run a post-hoc near-dup check that may emit `merged`.
- `on_retrieved` — if a hit is past 80% of its TTL, bump `staleness_score` and emit `refresh_requested`.

Add a handler by registering it in `handlers.HANDLERS`.

### Self-hosted research (`research/`, `src/engram/research/`)
- **SearXNG** in Docker (`research/searxng/`) provides web search without sending queries to a third party.
- **Cross-encoder reranker** (`ms-marco-MiniLM-L-6-v2`) re-orders SearXNG candidates before ingest.
- **arXiv fetcher** (`src/engram/research/arxiv.py`) pulls abstracts and PDFs.
- **`research_ingest_url`** runs server-side (host fetch + extract + dedup); **`research_fetch_url`** stamps a body the caller already fetched.

### Source curation (`src/engram/poller/`)
Polled, declarative source subscriptions. `sources.add` registers a feed; the `engram-poller` daemon picks it up on its 60 s tick, dispatches the named adapter, and pushes each candidate through the dedup gate. Four adapters in v0.3:

- **`sitemap`** — walks `sitemap.xml` (incl. sitemap-index files), filters URLs through include/exclude globs, fetches with ETag + Last-Modified conditional GETs, extracts via trafilatura.
- **`github-repo`** — branch HEAD lookup; first run walks the tree, subsequent runs use the GitHub `compare` API for incremental updates. Authenticates via `$GITHUB_TOKEN`, falling back to the `gh` CLI credential store (`gh auth token`), then to anonymous requests (60 req/hr) if neither is available.
- **`mediawiki-api`** — talks directly to a wiki's `/api.php` (Fandom, Wikipedia, PCGamingWiki, ED-Codex, anything MediaWiki). Discovers pages via `list=allpages` on first run; tracks updates via `list=recentchanges` on subsequent runs. Always sends `maxlag=5` and `assert=anon`. Bypasses Cloudflare HTML gating since the API endpoint isn't gated the same way.
- **`urls`** — manually curated list of URLs for sites with no sitemap and no API (Wikipedia single articles, Inara reference pages, dashboards). Same conditional-GET caching as sitemap.

All four adapters share a `fetch_with_politeness` helper that honors `Retry-After` on 429/503, sends both `If-None-Match` and `If-Modified-Since`, and rate-limits per source. When upstream content changes, the dedup gate's `superseded` outcome chains revisions: old rows get `is_current=0` (still queryable), the new row becomes current, the vault file overwrites in place. Per-adapter schedules and a 5-error circuit breaker. Specs: [v0.2 source curation](docs/superpowers/specs/2026-05-06-source-curation-design.md), [v0.3 adapter expansion](docs/superpowers/specs/2026-05-06-adapter-expansion-design.md).

CLI mirror: `bin/eos-source` for shell-side ops without going through MCP.

### Playbooks (`playbooks/`)
Two lanes:
- **Scratch (Jupyter):** notebooks under `playbooks/scratch/`, executed headlessly via `papermill`. Default for ad-hoc work.
- **Curated (Marimo):** reactive Python files under `playbooks/curated/`. For workflows you want to re-run reproducibly. (Lane exists; no curated playbooks ship in `v0.3.0-alpha.1`.)

`playbook.run` writes outputs to `playbooks/runs/<run_id>/`. `playbook.summarize` pushes a summary string into the KB as `kind=playbook-summary`; the full notebook stays in the run dir.

### Daemons (`systemd/`)
Four long-running processes installed as user systemd units:
- `engram-projector.service` — log → vault markdown
- `engram-watcher.service` — vault edits → log
- `engram-reactor.service` — embed-on-ingest, staleness, near-dup post-check
- `engram-poller.service` — scan due sources, dispatch adapters, gate candidates

Plus an `engram-daily-digest.timer` that synthesizes the last 24 h of events into an episode entry (with a per-source curation breakdown when sources are configured).

### Confidence model
```
confidence = source_tier_weight × recency_decay × stored_confidence
recency_decay = 0.5 ** (age_days / half_life_days)
```
Tier weights and half-life are configured in `config.yml`. Per-entry `ttl_days` overrides half-life for volatile topics. The retrieval ranker uses this so ranking stays correct without manual tuning.

## Quick start

```bash
# 1. Initialize (requires uv). Builds ~/.engram/.venv and installs engram into
#    it, then creates ~/.engram/{config.yml,.env,vault,db.sqlite}.
./bin/eos-init

# 2. Wire the MCP server into Claude Code.
claude mcp add -s user engram ~/.engram/.venv/bin/engram-mcp

# 3. Start the daemons (systemd user units; copy them in first — see docs/setup.md).
systemctl --user enable --now \
  engram-projector engram-watcher engram-reactor engram-poller engram-daily-digest.timer

# 4. Optional: register a polled source.
./bin/eos-source add docker-docs-linux \
  --name "Docker Docs (Linux)" --adapter sitemap \
  --url https://docs.docker.com/sitemap.xml \
  --include '*/engine/*' --include '*/desktop/install/linux*' \
  --schedule 7d
```

Full setup, troubleshooting, and round-trip verification: [docs/setup.md](docs/setup.md).

## Configuration

Single source: `~/.engram/config.yml` (override path with `$ENGRAM_CONFIG`). Template: [`config.example.yml`](config.example.yml). Secrets live in `~/.engram/.env`; template: [`.env.example`](.env.example).

The five things you may want to change:
- `paths.root` — where everything lives. Default `~/.engram`.
- `rag.embed_model` — swap for a smaller ONNX runtime if you want a lighter install. Contract: `encode(texts, normalize_embeddings=True) -> np.ndarray[float32]`.
- `confidence.source_tier_weights` — how much you trust each source class.
- `research.searxng_url` — point at your SearXNG instance.
- `playbooks.default_runtime` — `jupyter` or `marimo`.

## Repository layout

```
engram/
├── schema/                       # 001_initial.sql + 002_sources_and_revisions.sql
├── src/engram/
│   ├── common/                   # config, db connection, migration runner, paths
│   ├── log.py                    # event log read/write
│   ├── dedup.py                  # the gate: SHA-256 + cosine + supersede
│   ├── rag/                      # chunk, embed, hybrid query
│   ├── research/                 # SearXNG, cross-encoder, arXiv
│   ├── poller/                   # source-curation daemon
│   │   └── adapters/             # sitemap, github-repo
│   ├── mcp_server/               # one MCP server, namespaced tools
│   │   └── tools/                # kb / rag / research / playbook / goals / sources
│   ├── projector/                # log → vault markdown daemon
│   ├── watcher/                  # vault edits → log events
│   ├── reactor/                  # event-triggered handlers
│   └── cli/                      # eos-source CLI
├── playbooks/{scratch,curated}/  # Jupyter (default) / Marimo (curated)
├── research/searxng/             # SearXNG docker-compose + config
├── systemd/                      # user unit files for the four daemons + digest timer
├── vault-template/               # initial Obsidian vault layout
├── bin/                          # eos, eos-init, eos-mcp, eos-source, eos-status, ...
├── tests/                        # unit (sources/) + integration/
├── docs/                         # architecture, schema, MCP tools, setup, specs/, plans/
├── design/                       # original design artifacts (frozen)
└── scripts/                      # reconcile_vault, seed_starter_playbooks
```

## Versioning

Currently `v0.3.0-alpha.1` (adapter expansion). While in alpha:

- The event log schema and the on-disk layout under `~/.engram/` may change. Migrations are version-gated via `schema_version`; the `init_schema()` runner applies any `schema/NNN_*.sql` past the highest applied version on every connect.
- MCP tool signatures may add or remove parameters.
- No backward-compatibility guarantees between alpha releases.

Once the alpha series ends, this project follows [SemVer 2.0.0](https://semver.org/): breaking schema or MCP changes bump the major; new tools or new event types bump the minor; bug fixes bump the patch.

Releases live as git tags on `main`. See [`docs/superpowers/`](docs/superpowers/) for design specs and implementation plans.

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version of Engram as a network service, you must offer your users the modified source under the same license.
