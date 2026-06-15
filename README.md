<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="design/brand/engram-lockup-horizontal-white.svg">
    <source media="(prefers-color-scheme: light)" srcset="design/brand/engram-lockup-horizontal-ink.svg">
    <img alt="Engram" src="design/brand/engram-lockup-horizontal-ink.svg" width="360">
  </picture>
</p>

<p align="center">
  <strong>A personal knowledge platform built around an append-only event log, projected into an Obsidian vault, accessed by agents over MCP.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: AGPL-3.0-or-later" src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
  <a href="https://docs.astral.sh/uv/"><img alt="built with uv" src="https://img.shields.io/badge/built%20with-uv-261230.svg"></a>
  <a href="https://github.com/nNemLab/engram/releases"><img alt="latest release" src="https://img.shields.io/github/v/release/nNemLab/engram?sort=semver&amp;display_name=tag&amp;cacheSeconds=3600"></a>
</p>

> **Status:** early development (`0.x`). While on the `0.x` series, APIs, schema, and on-disk layout may change between minor versions without migration paths. (The badge above shows the latest release.)

---

## Table of contents

- [Highlights](#highlights)
- [How it works](#how-it-works)
- [Roadmap](#roadmap)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Docker](#docker)
- [Ambient memory](#ambient-memory)
- [What you can curate](#what-you-can-curate)
- [Documentation](#documentation)
- [Configuration](#configuration)
- [Privacy & offline](#privacy--offline)
- [Uninstall](#uninstall)
- [Contributing](#contributing)
- [FAQ](#faq)
- [License](#license)

## Highlights

- **Append-only event log.** Every state change is an immutable event in an
  append-only SQLite log; the FTS index, vector index, and Obsidian vault are
  projections rebuilt from the database.
- **Hybrid retrieval.** `sqlite-vec` vector search fused with SQLite FTS5
  full-text search via reciprocal-rank fusion, ranked by source-tier × recency ×
  confidence.
- **Ambient memory.** An optional Claude Code plugin auto-injects calibrated
  retrieval on every turn, primes each session, and records the entries it cites — so
  memory shows up without being asked for. See [Ambient memory](#ambient-memory).
- **Self-hosted & offline-capable.** Runs entirely on your machine. Everything
  curated is local; query, read, and edit with no network. Web search goes
  through your own SearXNG.
- **Human-auditable.** The vault is plain markdown you can read, grep, diff, and
  correct by hand — and every change is an immutable, timestamped event.
- **No hidden memory.** Agent and source writes flow through one dedup gate, and
  your own edits in the vault are taken as authoritative — there is no hidden
  agent-only memory, and you can always see and override what was stored.

## How it works

Engram treats:

- **the event log as canonical history** — every state change is an immutable event in SQLite,
- **the Obsidian vault as a projection** — markdown files are a materialized view, not source of truth,
- **the human and the agent as peers** — the agent writes through the dedup gate, the human edits the vault directly, and both act on the same content.

```mermaid
flowchart TD
    K["Claude Code · kernel"]
    L[("Event Log — SQLite, append-only<br/>ingested · merged · superseded · retrieved · vault_edit · source_polled")]
    P["Projector<br/>log → vault"]
    R["RAG view<br/>vec0 + FTS5"]
    Rx["Reactor<br/>embed · staleness"]
    O["Obsidian<br/>human edits"]
    W["Watcher<br/>edits → log"]
    Po["Poller<br/>due sources"]
    A["Adapters<br/>sitemap · github-repo · mediawiki · urls"]

    K <-->|MCP stdio| L
    L --> P --> O
    O -->|edits| W -->|edits → log| L
    L --> R
    L --> Rx -->|embed / merge| L
    L --> Po --> A -->|candidates → gate| L

    classDef kernel fill:#eef2ff,stroke:#6366f1,color:#0f172a;
    classDef log fill:#e0f2fe,stroke:#0ea5e9,color:#0f172a;
    classDef view fill:#ccfbf1,stroke:#14b8a6,color:#0f172a;
    classDef human fill:#fef3c7,stroke:#f59e0b,color:#0f172a;
    classDef source fill:#f3e8ff,stroke:#a855f7,color:#0f172a;

    class K kernel
    class L log
    class P,R,Rx view
    class O,W human
    class Po,A source
```

Agent and source writes go through `dedup.gate()`, which classifies each
candidate as `exact_dup`, `superseded`, `near_dup`, or `new`. The reactor embeds
and post-checks for near-dups. The projector renders content rows to the vault.
The watcher tails the vault so manual edits in Obsidian are applied directly and
become authoritative.

| Subsystem | Role |
|---|---|
| **Event log** | Append-only SQLite; the immutable, timestamped record of every state change. |
| **Dedup gate** | The write path for agent and source content: `exact_dup` / `superseded` / `near_dup` / `new`. |
| **RAG** | Hybrid `vec0` + FTS5 retrieval, RRF-fused, ranked by confidence × source-tier × recency. |
| **MCP server** | One server (stdio or HTTP), seven tool namespaces (`kb`, `rag`, `research`, `playbook`, `goals`, `sources`, `session`). |
| **Projector / Watcher** | Log → vault markdown, and vault edits → log. |
| **Reactor** | Embed-on-ingest, staleness checks, near-dup post-check. |
| **Poller + adapters** | `sitemap`, `github-repo`, `mediawiki-api`, `urls`. |
| **Research** | Self-hosted SearXNG, cross-encoder rerank, arXiv fetcher. |
| **Playbooks** | Jupyter (scratch) / Marimo (curated), run via `playbook.run`. |
| **Daemons** | Four systemd user units + a daily-digest timer (plus an optional grounding daemon for ambient memory). |

Full internals — component-by-component, the confidence model, and
failure/recovery modes — are in [docs/architecture.md](docs/architecture.md).

## Roadmap

Two capabilities are **under development** and not available yet:

- **Full log replay.** Today the unit you back up is the SQLite database. The
  event log is a complete audit trail of every state change, but rebuilding all
  content from the log alone (replay from event 0) is not yet implemented — the
  goal is to make the log self-sufficient so it becomes the only thing you need
  to back up.
- **arXiv PDF ingestion.** `research.fetch_arxiv` currently returns abstracts
  and PDF links; ingesting full PDF text is planned.

## Requirements

- **[uv](https://docs.astral.sh/uv/)** — builds the runtime venv and installs engram (no separate `pip install`).
- **Python 3.11+**
- **[Obsidian](https://obsidian.md/)** — the human surface for the vault (optional but recommended).
- **CPU by default; GPU optional.** Embeddings run on CPU out of the box; the GPU lane is an install-time choice — see [docs/configuration.md](docs/configuration.md).

Developed and run on Linux; macOS should work (SQLite + a uv venv). The native
install uses systemd user units; the [Docker](#docker) path needs only Docker.

## Quick start

**1. Get the code.**

```bash
git clone https://github.com/nNemLab/engram.git && cd engram
```

**2. Initialize** (builds `~/.engram/.venv` and installs engram with the `[rag]`
extra, then creates `~/.engram/{config.yml,.env,vault,db.sqlite}`).

```bash
./bin/eos-init
```

**3. Wire the MCP server into Claude Code.**

```bash
claude mcp add -s user engram ~/.engram/.venv/bin/engram-mcp
```

**4. Start the daemons** (systemd user units; copy them in first — see [docs/setup.md](docs/setup.md)).

```bash
systemctl --user enable --now \
  engram-projector engram-watcher engram-reactor engram-poller engram-daily-digest.timer
```

**5. Verify.**

```bash
./bin/eos-status
```

Full setup, troubleshooting, and round-trip verification:
[docs/setup.md](docs/setup.md). To start feeding it content, see
[What you can curate](#what-you-can-curate).

## Docker

Prefer containers? One command brings up the full stack (MCP server + daemons +
a private SearXNG):

```bash
docker compose -f docker/compose.yml up -d --build
```

Connect any MCP client over the HTTP transport:

```bash
claude mcp add --transport http engram http://localhost:8765/mcp
```

See **[docker/README.md](docker/README.md)** for setup, the loopback-only
security note, and the provider-agnostic `ENGRAM_LLM_*` config.

## Ambient memory

For auto-injected retrieval on every turn — calibrated so it stays quiet when
nothing relevant exists — run the grounding daemon (`engram-rag serve`) and
enable the Claude Code plugin in `engram-plugin/`. The plugin injects relevant
memory (`UserPromptSubmit`), primes each session (`SessionStart`), and records
the entries the agent cites (`Stop`) so they rank higher next time.

See **[engram-plugin/README.md](engram-plugin/README.md)** for install options
and configuration.

## What you can curate

Anything text-shaped that you want an agent to remember and reason over. Content
arrives three ways — you write it, the agent writes it, or a polled source feeds
it — and all three land in the same gate.

| You want to keep... | How |
|---|---|
| Your own notes, decisions, episodes | `kb.write`, or drop a markdown file into the vault inbox |
| Docs sites that change over time | `sitemap` source (e.g. Docker, a framework's docs) |
| Wikis | `mediawiki-api` source (Fandom game wikis, Wikipedia, PCGamingWiki, …) |
| A GitHub repo's docs/code tree | `github-repo` source (tracks branch HEAD, incremental via compare API) |
| Research papers | `research.fetch_arxiv` (abstracts + PDF links; full-PDF ingestion is [on the roadmap](#roadmap)) |
| One-off pages with no feed | `urls` source, or `research.ingest_url` |
| Web-search findings | `research.search_web` via your local SearXNG, then ingest the keepers |

A polled source is one declarative line. For example, track the Linux-relevant
slice of Docker's docs, re-checked weekly:

```bash
./bin/eos-source add docker-docs-linux \
  --name "Docker Docs (Linux)" --adapter sitemap \
  --url https://docs.docker.com/sitemap.xml \
  --include '*/engine/*' --include '*/desktop/install/linux*' \
  --schedule 7d
```

The poller fetches on its tick, runs every candidate through the dedup gate, and
chains revisions when a page changes (old version stays queryable, vault file
updates in place). Per-source schedules, conditional GETs, and a circuit breaker
keep it polite.

## Documentation

| Document | What's inside |
|---|---|
| [Setup](docs/setup.md) | Full install, running the daemons, verification, and troubleshooting. |
| [Configuration](docs/configuration.md) | `config.yml` reference, the LLM provider, embedding/reranker models, and the CPU-vs-GPU lane. |
| [Architecture](docs/architecture.md) | Component-by-component internals, the confidence model, and failure/recovery modes. |
| [MCP tool reference](docs/mcp-tool-reference.md) | Every tool across the seven namespaces, with arguments. |
| [Event log schema](docs/event-log-schema.md) | Event types, tables, and the invariants the log guarantees. |
| [Docker](docker/README.md) | Container install, what's exposed, and the loopback-only security posture. |
| [Ambient memory plugin](engram-plugin/README.md) | Plugin install options and grounding-daemon configuration. |
| [Contributing](CONTRIBUTING.md) | Dev setup, running the tests, and the PR flow. |

New to the project? Read [Setup](docs/setup.md) → [Configuration](docs/configuration.md)
→ [Architecture](docs/architecture.md), then keep the
[MCP tool reference](docs/mcp-tool-reference.md) handy.

## Configuration

All settings live in `~/.engram/config.yml` (secrets in `~/.engram/.env`),
written by `bin/eos-init`. See **[docs/configuration.md](docs/configuration.md)**
for the full reference — including the embedding/reranker model options and the
CPU-vs-GPU lane choice.

## Privacy & offline

Engram is **online to gather, offline to use** — self-hosted, single-user, and
free of telemetry.

- **Online to gather.** The poller refreshes live sources, `research.search_web`
  queries your SearXNG, and the arXiv/URL fetchers pull new material. Search goes
  through your own SearXNG instance, so queries aren't handed to a third-party API.
- **Offline to use.** Everything already curated is local — a SQLite database
  plus a markdown vault. Query, read, and edit it with no network; embeddings run
  on CPU locally, so retrieval works on a plane or an air-gapped box. The poller
  pauses and resumes when connectivity returns; nothing else depends on the network.
- **Auditable by construction.** The append-only log is a full audit trail —
  every ingest, merge, supersede, retrieval, and human edit is an immutable,
  timestamped row. Nothing mutates silently; superseded revisions stay queryable
  rather than being deleted.
- **Yours to correct.** The vault is plain markdown in Obsidian: human-readable,
  greppable, diffable, git-able. The watcher makes your hand edits authoritative,
  and there is no hidden agent-only memory — everything the agent stores is
  visible and overridable.

## Uninstall

```bash
./bin/eos-uninstall
```

Removes the runtime (`~/.engram` — database, vault, venv — plus the systemd
units and the Claude Code MCP registration). It first reports the database size
and offers to export it to an `engram-export-<timestamp>.tar.gz`, then requires
you to type `DELETE` to confirm. **Removal is permanent; unexported curated
knowledge is lost for good.** The source checkout is left in place. It
auto-detects whether you have a native or Docker install and handles both.

## Contributing

Contributions are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for dev
setup, running the test suite, and the PR flow, and
**[SECURITY.md](SECURITY.md)** for reporting vulnerabilities. Changes are tracked
in [CHANGELOG.md](CHANGELOG.md).

## FAQ

**Is it multi-user?**
No. Single-user by design — no auth and no concurrency model beyond SQLite's WAL.

**Does my data leave my machine?**
No. Engram is self-hosted with no cloud backend, no account, and no telemetry.
Web search runs through your own SearXNG, and server-side fetches of
agent-supplied URLs are SSRF-guarded.

**Do I need a GPU?**
No. Embeddings run on CPU by default. The GPU lane is an opt-in install choice,
not a config flag — see [docs/configuration.md](docs/configuration.md).

**Is it a vector database?**
No. Retrieval is hybrid `sqlite-vec` + FTS5, RRF-fused and scored by confidence,
source tier, and recency. If you need a dedicated vector DB at scale, this is the
wrong project.

**Is it a Claude API wrapper?**
No. Engram exposes tools to a kernel (Claude Code, or any MCP client). The kernel
does the reasoning; engram is storage and retrieval.

**Which clients work?**
Any MCP-capable client. Claude Code is the primary target, over stdio (native) or
the HTTP transport (Docker).

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version of Engram as a
network service, you must offer your users the modified source under the same
license.
