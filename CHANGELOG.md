# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-10

Ambient memory, durability tooling, and a Docker install.

### Added

- **Ambient memory (Claude Code plugin + grounding daemon).** A new `engram-rag`
  daemon serves calibrated retrieval over HTTP, and the `engram-memory` Claude
  Code plugin wires it into a session: it auto-injects relevant memory on every
  turn (`UserPromptSubmit`), primes each session (`SessionStart`), and records
  grounded-in citations (`Stop`), backed by a companion behavioral skill.
  Retrieval is calibrated to a `STRONG`/`WEAK`/`NONE` verdict so it stays quiet
  when nothing relevant exists.
- **Session priming and reflection.** `session.prime` seeds a new session with
  active goals plus high-confidence memory; `session.reflect` (and the
  `eos-reflect` CLI) surface what needs attention — unresolved contradictions,
  stale high-value entries, and idle goals. Both deterministic, no model calls.
- **Citation-weighted ranking.** A `rag.cite` tool and a `content_usage` table
  record which entries get cited; cited entries are weighted up in later
  retrieval (`usage_factor`).
- **`rag.query` calibration and scoping.** Returns a calibration `verdict`
  alongside results, and accepts `token_budget`, `level` (snippet/full), and
  `since` filters.
- **Backup, verify, and restore.** `eos-snapshot` (consistent `VACUUM INTO`
  copy), `eos-verify` (content-hash re-derivation + integrity invariants, with a
  CI/cron-usable exit code), and `eos-restore` (verify-first, backup-first,
  typed confirmation) — making the "back up the log" promise real.
- **Source health observability.** A `sources.health` tool and `eos-source
  health` CLI report per-source liveness, content counts, duplicate ratio, and a
  derived status.
- **Docker install method.** `docker compose` brings up the full stack (MCP HTTP
  streamable server + daemons + a private SearXNG); the LLM is provider-agnostic
  via `ENGRAM_LLM_*`.
- **Uninstall.** `eos-uninstall` fully removes engram (native *or* Docker) with
  an optional database export and a typed `DELETE` confirmation.
- **Schema migrations** `003` (grounding / `content_usage`) and `004`
  (`content.protected`).

### Changed

- **`sentence-transformers` moved to an optional `[rag]` extra** (with `numpy`
  promoted to a core dependency). Real installs (`eos-init`, Docker) pull
  `[rag]`; a bare core install is now torch-free, which keeps CI and
  embedding-stubbing tooling lean.
- **`rag.query` response shape** now wraps results with a calibration verdict:
  `{verdict, results}`.

### Fixed

- **Human edits are no longer silently clobbered (#37).** When the poller would
  supersede a human-edited (`protected`) sourced row, it now preserves the
  upstream change as a non-current revision and raises a contradiction instead
  of overwriting the human's vault file — restoring the "human and agent are
  peers" invariant.

## [0.1.0] - 2026-06-09

Initial public release.

### Added

- **Event-log-canonical knowledge base.** Append-only SQLite event log is the
  single source of truth; the FTS index, vector index, and Obsidian vault are
  all rebuildable projections. Every write passes through one dedup gate
  (`exact_dup` / `superseded` / `near_dup` / `new`).
- **Hybrid RAG retrieval.** `sqlite-vec` vector search fused with SQLite FTS5
  full-text search via reciprocal-rank fusion, ranked by source-tier × recency
  × confidence.
- **Six MCP tool namespaces** over a single stdio server: `kb`, `rag`,
  `research`, `playbook`, `goals`, and `sources`.
- **Self-hosted web research.** `research.search_web` queries a local SearXNG
  instance (no third-party search API), with cross-encoder reranking and
  SSRF-guarded outbound fetching.
- **arXiv search and ingestion** for abstracts and PDFs.
- **Obsidian vault projection.** The projector renders content rows to markdown;
  the watcher tails the vault so manual edits become authoritative back in the
  log.
- **Source poller with adapters** — `sitemap`, `github-repo`, `mediawiki-api`,
  and `urls` — with per-source schedules, conditional GETs, and a circuit
  breaker. Page revisions chain so superseded versions stay queryable.
- **Playbooks.** Jupyter (scratch) and Marimo (curated) notebooks runnable via
  `playbook.run`.

[Unreleased]: https://github.com/nNemLab/engram/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/nNemLab/engram/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nNemLab/engram/releases/tag/v0.1.0
