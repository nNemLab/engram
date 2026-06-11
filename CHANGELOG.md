# Changelog

All notable changes to this project are documented in this file.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
From v0.2.1 onward, releases are automated by
[release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/); entries below
v0.2.1 were authored by hand.

## [0.2.3](https://github.com/nNemLab/engram/compare/v0.2.2...v0.2.3) (2026-06-11)


### Features

* **dedup:** resolve tool for blocked-supersede contradictions ([#71](https://github.com/nNemLab/engram/issues/71)) ([26a9cad](https://github.com/nNemLab/engram/commit/26a9cadf957cc714259c5203e6396cc948a40846)), closes [#54](https://github.com/nNemLab/engram/issues/54)
* **log:** tamper-evident hash-chained event log ([#72](https://github.com/nNemLab/engram/issues/72)) ([17a4861](https://github.com/nNemLab/engram/commit/17a486138f658215dfe2b082fdacac249f558ad3)), closes [#45](https://github.com/nNemLab/engram/issues/45)
* **maintenance:** add corpus re-embed migration for embedding-model changes ([#74](https://github.com/nNemLab/engram/issues/74)) ([29fbdb0](https://github.com/nNemLab/engram/commit/29fbdb063b26f5e6e17640b4f229de5db49db7f2)), closes [#43](https://github.com/nNemLab/engram/issues/43)
* **rag:** temporal retrieval — time-bounded query + episodic timeline ([#75](https://github.com/nNemLab/engram/issues/75)) ([5baaa17](https://github.com/nNemLab/engram/commit/5baaa17db650902f49adcb058847653ffb2669f8)), closes [#40](https://github.com/nNemLab/engram/issues/40)


### Bug Fixes

* **init:** seed starter playbooks in eos-init ([#68](https://github.com/nNemLab/engram/issues/68)) ([b068a42](https://github.com/nNemLab/engram/commit/b068a4277f44e6bd6c4688d17f66bcef9f6ce14d)), closes [#67](https://github.com/nNemLab/engram/issues/67)
* **rag:** default source-tier weights so tier ranking isn't a no-op ([#78](https://github.com/nNemLab/engram/issues/78)) ([c0d6191](https://github.com/nNemLab/engram/commit/c0d61913fc0bd175ae2228380b9dc213413a3f2c))
* repair hybrid retrieval ranking, BM25 recall, grounding verdict, and near-dup dedup ([#81](https://github.com/nNemLab/engram/issues/81)) ([239e80e](https://github.com/nNemLab/engram/commit/239e80ec1a0a0ba186f72876c8646c00f90a578a))
* **watcher:** record human vault edits as new content revisions ([#70](https://github.com/nNemLab/engram/issues/70)) ([d52b5cf](https://github.com/nNemLab/engram/commit/d52b5cf67ea4f612d04579761b605509dde5c68c)), closes [#55](https://github.com/nNemLab/engram/issues/55)


### Documentation

* **configuration:** add granite-embedding-r2 as the CPU 768-dim option ([#79](https://github.com/nNemLab/engram/issues/79)) ([6fda21a](https://github.com/nNemLab/engram/commit/6fda21ab846e83a59eca194730d4a00760e85e99))
* fix release badge, recenter header lockup, correct stale references ([#76](https://github.com/nNemLab/engram/issues/76)) ([73ad702](https://github.com/nNemLab/engram/commit/73ad7022a5cdf8d13ee3549fc712c8bb88887bd1))

## [0.2.2](https://github.com/nNemLab/engram/compare/v0.2.1...v0.2.2) (2026-06-10)


### Bug Fixes

* **rag:** sanitize FTS5 MATCH query so special characters don't crash retrieval ([#64](https://github.com/nNemLab/engram/issues/64)) ([efbdea0](https://github.com/nNemLab/engram/commit/efbdea064b8a2e510d38a6b3fc5dcb91a62c7c56)), closes [#63](https://github.com/nNemLab/engram/issues/63)

## [0.2.1](https://github.com/nNemLab/engram/compare/v0.2.0...v0.2.1) (2026-06-10)


### Features

* **db:** schema/embedding-dimension compatibility guard + eos-version ([83079bd](https://github.com/nNemLab/engram/commit/83079bd91f48252ca47ce92dc7a6248b9ae58d2f))


### Documentation

* restructure README + add documentation index ([fb4322e](https://github.com/nNemLab/engram/commit/fb4322e497e5cf060ec7140b710e1be1f1cf3fc5))

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

[0.2.0]: https://github.com/nNemLab/engram/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nNemLab/engram/releases/tag/v0.1.0
