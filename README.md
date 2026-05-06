# Engram

**A personal knowledge platform built around an append-only event log, projected into an Obsidian vault, accessed by agents over MCP.**

> **Status:** `v0.1.0-alpha.1` — base version, dev build. Not a release. APIs, schema, and on-disk layout may change without migration paths until `v0.1.0`.

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

Every content write goes through `dedup.gate()` and produces an `ingested` event. The reactor embeds and post-checks for near-dups. The projector renders content rows to the vault. The watcher tails the vault filesystem so manual edits in Obsidian become authoritative.

Long form: [docs/architecture.md](docs/architecture.md).

## Major components

### Event log (`schema/001_initial.sql`, `src/engram/log.py`)
One append-only `events` table. Each daemon keeps a cursor in `daemon_cursors` and replays forward. Twelve event types defined; see [docs/event-log-schema.md](docs/event-log-schema.md) for the full taxonomy. The log is canonical — replaying from event 0 reconstructs the system.

### Dedup gate (`src/engram/dedup.py`)
The single entry point for any content write. Returns one of:
- `exact_dup` — SHA-256 collision, no-op.
- `near_dup` — cosine similarity ≥ 0.92 against an existing embedding, merge.
- `new` — inserted, `ingested` event emitted.

Near-dup at write-time requires a query embedding and is best-effort; the reactor does a post-hoc near-dup check after embedding to catch cases the caller didn't pre-embed.

### RAG (`src/engram/rag/`)
Hybrid retrieval. `chunk.py` splits markdown by structure with a sliding-window fallback. `embed.py` wraps `sentence-transformers/all-MiniLM-L6-v2` (384-dim, CPU-friendly; swappable via `config.yml`). `query.py` runs `vec0` ANN and FTS5 in parallel, fuses with Reciprocal Rank Fusion, and ranks by `rrf_score × confidence × source_tier_weight × recency_decay`. Each hit emits a `retrieved` event so the reactor can mark stale entries on demand.

### MCP server (`src/engram/mcp_server/`)
One stdio server exposing five tool namespaces. Tool handlers run in a worker thread (`asyncio.to_thread`) so a slow embed doesn't block the stdio loop. Holds one long-lived SQLite connection.

| Namespace | Tools |
|---|---|
| `kb` | `write`, `get`, `list`, `tombstone`, `contradictions`, `flag_contradiction` |
| `rag` | `query` |
| `research` | `search_web`, `fetch_url`, `ingest_url`, `fetch_arxiv` |
| `playbook` | `list`, `run`, `summarize` |
| `goals` | `set`, `list`, `resolve` |

Full reference: [docs/mcp-tool-reference.md](docs/mcp-tool-reference.md).

### Projector (`src/engram/projector/`)
Tails the log for `ingested` and `merged` events. Renders content rows to markdown via per-kind renderers (`kb`, `episode`, `entity`, `research`, `playbook-summary`). Records the rendered bytes in `vault_state` so the watcher can diff against them. Tombstoned content gets its vault file deleted.

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

### Playbooks (`playbooks/`)
Two lanes:
- **Scratch (Jupyter):** notebooks under `playbooks/scratch/`, executed headlessly via `papermill`. Default for ad-hoc work.
- **Curated (Marimo):** reactive Python files under `playbooks/curated/`. For workflows you want to re-run reproducibly. (Lane exists; no curated playbooks ship in `v0.1.0-alpha.1`.)

`playbook.run` writes outputs to `playbooks/runs/<run_id>/`. `playbook.summarize` pushes a summary string into the KB as `kind=playbook-summary`; the full notebook stays in the run dir.

### Daemons (`systemd/`)
Three long-running processes installed as user systemd units:
- `engram-projector.service` — log → vault markdown
- `engram-watcher.service` — vault edits → log
- `engram-reactor.service` — embed-on-ingest, staleness, near-dup post-check

Plus an `engram-daily-digest.timer` that synthesizes the last 24h of events into an episode entry.

### Confidence model
```
confidence = source_tier_weight × recency_decay × stored_confidence
recency_decay = 0.5 ** (age_days / half_life_days)
```
Tier weights and half-life are configured in `config.yml`. Per-entry `ttl_days` overrides half-life for volatile topics. The retrieval ranker uses this so ranking stays correct without manual tuning.

## Quick start

```bash
# 1. Install (editable; brings in all deps).
uv pip install -e .

# 2. Initialize. Creates ~/.engram/{config.yml,.env,vault,db.sqlite,.venv}.
./bin/eos-init

# 3. Wire the MCP server into Claude Code.
claude mcp add -s user engram ~/.engram/.venv/bin/engram-mcp

# 4. Start the daemons (systemd user units).
systemctl --user enable --now \
  engram-projector engram-watcher engram-reactor engram-daily-digest.timer
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
├── schema/001_initial.sql        # event log, content, fts5, embeddings hookup
├── src/engram/
│   ├── common/                   # config, db connection, paths
│   ├── log.py                    # event log read/write
│   ├── dedup.py                  # the gate: SHA-256 + cosine
│   ├── rag/                      # chunk, embed, hybrid query
│   ├── research/                 # SearXNG, cross-encoder, arXiv
│   ├── mcp_server/               # one MCP server, namespaced tools
│   │   └── tools/                # kb / rag / research / playbook / goals
│   ├── projector/                # log → vault markdown daemon
│   ├── watcher/                  # vault edits → log events
│   └── reactor/                  # event-triggered handlers
├── playbooks/{scratch,curated}/  # Jupyter (default) / Marimo (curated)
├── research/searxng/             # SearXNG docker-compose + config
├── systemd/                      # user unit files for the three daemons + digest timer
├── vault-template/               # initial Obsidian vault layout
├── bin/                          # eos, eos-init, eos-mcp, eos-status, ...
├── docs/                         # architecture, schema, MCP tools, setup
├── design/                       # original design artifacts (frozen)
└── scripts/                      # reconcile_vault, seed_starter_playbooks
```

## Versioning

This is `v0.1.0-alpha.1` — the **base version**, a dev build. Pre-1.0 means:

- The event log schema (`schema/001_initial.sql`) and the on-disk layout under `~/.engram/` may change without migration tooling.
- MCP tool signatures may add or remove parameters.
- No backward-compatibility guarantees until `v0.1.0`.

After `v0.1.0`, this project follows [SemVer 2.0.0](https://semver.org/): breaking schema or MCP changes bump the major; new tools or new event types bump the minor; bug fixes bump the patch.

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version of Engram as a network service, you must offer your users the modified source under the same license.
