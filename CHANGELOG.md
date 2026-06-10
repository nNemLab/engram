# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/nNemLab/engram/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nNemLab/engram/releases/tag/v0.1.0
