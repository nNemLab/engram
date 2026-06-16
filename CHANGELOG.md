# Changelog

All notable changes to this project are documented in this file.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
From v0.2.1 onward, releases are automated by
[release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/); entries below
v0.2.1 were authored by hand.

## [0.2.5](https://github.com/nNemLab/engram/compare/v0.2.4...v0.2.5) (2026-06-16)


### Features

* **projector:** dead-letter repeated projection failures ([#200](https://github.com/nNemLab/engram/issues/200)) ([e210020](https://github.com/nNemLab/engram/commit/e210020452f16d844aa866f93667e631913c884f)), closes [#158](https://github.com/nNemLab/engram/issues/158)
* **rag:** cwd-scoped priming for session.prime / /prime ([#195](https://github.com/nNemLab/engram/issues/195)) ([308e5be](https://github.com/nNemLab/engram/commit/308e5beeeda98355d34f5eb1f1c18a582c483dfb))
* **sources:** emit audit events for source mutations ([#175](https://github.com/nNemLab/engram/issues/175)) ([05d76bb](https://github.com/nNemLab/engram/commit/05d76bb3577d930f2574fd8fe206209396c8e7cf)), closes [#166](https://github.com/nNemLab/engram/issues/166)


### Bug Fixes

* **db:** data-integrity cluster — atomic, cross-process-exclusive schema migrations ([#191](https://github.com/nNemLab/engram/issues/191)) ([f502c9d](https://github.com/nNemLab/engram/commit/f502c9d4882ce88c0ce7940fbec0a55f28cda4a2))
* harden batch retrieval, temporal filtering, and dedup correctness ([#193](https://github.com/nNemLab/engram/issues/193)) ([b7dcd33](https://github.com/nNemLab/engram/commit/b7dcd33242bec004afe9fb195cb965386480b01e))
* **infra:** harden config loading, eos-source CLI, and poller retry/rate-limit ([#194](https://github.com/nNemLab/engram/issues/194)) ([dda59ec](https://github.com/nNemLab/engram/commit/dda59ec626dcd34d2d31923bf5b9a13f231a6524))
* **poller:** segment-aware glob scoping, skip empty bodies, config-driven confidence/kind ([#178](https://github.com/nNemLab/engram/issues/178)) ([42c91df](https://github.com/nNemLab/engram/commit/42c91df38d89b08e186a08dfdd99c8b040f3de9f)), closes [#171](https://github.com/nNemLab/engram/issues/171)
* **rag:** offload grounding and prime CPU work from event loop ([#198](https://github.com/nNemLab/engram/issues/198)) ([da8768d](https://github.com/nNemLab/engram/commit/da8768da9cf44f6c6439272e74e1ff07bc757bbe)), closes [#157](https://github.com/nNemLab/engram/issues/157)
* **rag:** skip and log corrupt event-log payloads instead of aborting ([#177](https://github.com/nNemLab/engram/issues/177)) ([95b7363](https://github.com/nNemLab/engram/commit/95b7363beeb76a1218ec13b790a68920469a423d)), closes [#155](https://github.com/nNemLab/engram/issues/155)
* **reactor:** close robustness gaps in staleness, tick backoff, and shutdown ([#201](https://github.com/nNemLab/engram/issues/201)) ([9a505b3](https://github.com/nNemLab/engram/commit/9a505b3d660fc4686e7da09de2ea350a375e886a)), closes [#172](https://github.com/nNemLab/engram/issues/172)
* **research:** IP failover, pre-fetch dedup, media-type allowlist, arxiv field queries, tokenizer truncation ([#176](https://github.com/nNemLab/engram/issues/176)) ([6f96555](https://github.com/nNemLab/engram/commit/6f965555301da227008d577b3c09bf1006abad56)), closes [#173](https://github.com/nNemLab/engram/issues/173)
* **retrieval:** exclude non-current revisions from KNN/near-dup retrieval ([#151](https://github.com/nNemLab/engram/issues/151)) ([0855de6](https://github.com/nNemLab/engram/commit/0855de60bdfe32d9fa707de6a914a0570ba86196)), closes [#139](https://github.com/nNemLab/engram/issues/139)
* **vault:** use explicit UTF-8 for vault file reads and writes ([#174](https://github.com/nNemLab/engram/issues/174)) ([a559310](https://github.com/nNemLab/engram/commit/a5593109e4dd21c4b2d50436166006073a0fd780)), closes [#159](https://github.com/nNemLab/engram/issues/159)
* **watcher:** add delete/move handling and startup reconciliation ([#199](https://github.com/nNemLab/engram/issues/199)) ([7b87e28](https://github.com/nNemLab/engram/commit/7b87e284130c7345cc403d39aca120d034ad6cb1)), closes [#165](https://github.com/nNemLab/engram/issues/165)


### Documentation

* adopt low-friction private security reporting policy ([#146](https://github.com/nNemLab/engram/issues/146)) ([c478488](https://github.com/nNemLab/engram/commit/c478488f9325f407c51dad9b768429199c5a40a2))
* align CONTRIBUTING test suites and commit types with CI ([#147](https://github.com/nNemLab/engram/issues/147)) ([8d22d7a](https://github.com/nNemLab/engram/commit/8d22d7a81c7209306df3415482caa0e2227859aa))
* correct README claims for unimplemented capabilities ([#142](https://github.com/nNemLab/engram/issues/142)) ([f28361a](https://github.com/nNemLab/engram/commit/f28361af396a4d5c5a381c13f42251ed73813fbd))
* fix remaining "human and agent share the dedup gate" claims ([#144](https://github.com/nNemLab/engram/issues/144)) ([ef26581](https://github.com/nNemLab/engram/commit/ef26581787aff7a2c1cfecd830c6416ab39311b1))
* **rag:** list /cite endpoint in serve help text ([#190](https://github.com/nNemLab/engram/issues/190)) ([285b297](https://github.com/nNemLab/engram/commit/285b29756f9e58599f1a6e54e706cc8dd47c7264)), closes [#167](https://github.com/nNemLab/engram/issues/167)
* tighten overstated README claims to match actual behavior ([#145](https://github.com/nNemLab/engram/issues/145)) ([cf590fc](https://github.com/nNemLab/engram/commit/cf590fca6f4b0c60106cf5c31eae6fafd0805448))

## [0.2.4](https://github.com/nNemLab/engram/compare/v0.2.3...v0.2.4) (2026-06-15)


### Features

* **rag:** add tunable recency scoring to hybrid ranker ([#131](https://github.com/nNemLab/engram/issues/131)) ([db83a90](https://github.com/nNemLab/engram/commit/db83a90d5f31ce98f55b8aaa183a5ff5374402f8)), closes [#73](https://github.com/nNemLab/engram/issues/73)
* **reactor:** retry budget + dead-letter for deterministically-failing handlers ([#132](https://github.com/nNemLab/engram/issues/132)) ([296b8e0](https://github.com/nNemLab/engram/commit/296b8e0623d80cb5c233f45a55934c9562023c4e))


### Bug Fixes

* close DB connections, httpx clients, and debouncer timers on shutdown ([#141](https://github.com/nNemLab/engram/issues/141)) ([f0cb6e9](https://github.com/nNemLab/engram/commit/f0cb6e9cfebb5c35ab0e57fe017e448aa109d296)), closes [#92](https://github.com/nNemLab/engram/issues/92) [#138](https://github.com/nNemLab/engram/issues/138)
* **db:** atomic multi-statement writes + serialized shared SQLite connection ([#112](https://github.com/nNemLab/engram/issues/112)) ([15bbb06](https://github.com/nNemLab/engram/commit/15bbb06396c4f8d6db9d879f46aba07533678ff5))
* **dedup:** filter tombstoned embeddings and delete on tombstone ([#136](https://github.com/nNemLab/engram/issues/136)) ([e7d163a](https://github.com/nNemLab/engram/commit/e7d163a9e1eae02df1703ff56422f3ec819f2d38))
* harden maintenance restore safety ([#124](https://github.com/nNemLab/engram/issues/124)) ([3eacef5](https://github.com/nNemLab/engram/commit/3eacef5e29b03b4b6225910c972a6d02e6a35915)), closes [#94](https://github.com/nNemLab/engram/issues/94)
* **kb:** clamp list limit to MAX_LIMIT (100) and floor at 1 ([#121](https://github.com/nNemLab/engram/issues/121)) ([77179f6](https://github.com/nNemLab/engram/commit/77179f658bed283cb36b1b7c5bc7b011c2d589e9)), closes [#118](https://github.com/nNemLab/engram/issues/118)
* **mcp:** verify write rowcount and fetchone in sources/set and goals/resolve ([#120](https://github.com/nNemLab/engram/issues/120)) ([ab3d609](https://github.com/nNemLab/engram/commit/ab3d609d8b818a2fee9288e46268da6fb21bf075)), closes [#90](https://github.com/nNemLab/engram/issues/90)
* **playbook:** process-group cleanup on timeout + consistent early-out shape ([#129](https://github.com/nNemLab/engram/issues/129)) ([2c1e654](https://github.com/nNemLab/engram/commit/2c1e65487650738bd71c58aad5fdf644a9d0276d)), closes [#102](https://github.com/nNemLab/engram/issues/102)
* **poller:** cap Retry-After backoff and poll due sources concurrently ([#126](https://github.com/nNemLab/engram/issues/126)) ([123c1cd](https://github.com/nNemLab/engram/commit/123c1cd4688d581d8a871fd4769d64a9a1cd61d2)), closes [#87](https://github.com/nNemLab/engram/issues/87)
* **poller:** feed gate-path failures into circuit breaker escalation ([#135](https://github.com/nNemLab/engram/issues/135)) ([b1e0e08](https://github.com/nNemLab/engram/commit/b1e0e086c706ed7289a8f079f387fd68df6e6840)), closes [#97](https://github.com/nNemLab/engram/issues/97)
* **projector:** commit rendered_body before writing vault file ([#128](https://github.com/nNemLab/engram/issues/128)) ([bb747fd](https://github.com/nNemLab/engram/commit/bb747fdcfc0d62fcf0e960c68b98d0ddfb3b4a79)), closes [#96](https://github.com/nNemLab/engram/issues/96)
* **reactor:** stop cursor on handler failure to prevent silent data loss ([#111](https://github.com/nNemLab/engram/issues/111)) ([0af1a11](https://github.com/nNemLab/engram/commit/0af1a116c384568821c704ccb26c396e89d0f31c))
* **research:** harden web and arxiv fetch robustness ([#123](https://github.com/nNemLab/engram/issues/123)) ([cb23a9e](https://github.com/nNemLab/engram/commit/cb23a9ecdcc94992059783d537e505dcdf874db9)), closes [#93](https://github.com/nNemLab/engram/issues/93)
* **research:** pin validated IP to connection to close DNS-rebinding TOCTOU ([#127](https://github.com/nNemLab/engram/issues/127)) ([600d46b](https://github.com/nNemLab/engram/commit/600d46b27d255983e38dfeb30d01add9d1084227)), closes [#95](https://github.com/nNemLab/engram/issues/95)
* **research:** reject non-HTML content-types in ingest_url ([#122](https://github.com/nNemLab/engram/issues/122)) ([474dd36](https://github.com/nNemLab/engram/commit/474dd360a4162ea543e36b6204be80fbc9d58cb1)), closes [#77](https://github.com/nNemLab/engram/issues/77)


### Performance Improvements

* **db:** narrow DB lock to DB-touching regions so non-DB tool work runs concurrently ([#130](https://github.com/nNemLab/engram/issues/130)) ([0531216](https://github.com/nNemLab/engram/commit/05312163cbce24f2ff59b565e8ec10e8171db1bb)), closes [#113](https://github.com/nNemLab/engram/issues/113)

## [0.2.3](https://github.com/nNemLab/engram/compare/v0.2.2...v0.2.3) (2026-06-15)


### Features

* **dedup:** resolve tool for blocked-supersede contradictions ([#71](https://github.com/nNemLab/engram/issues/71)) ([26a9cad](https://github.com/nNemLab/engram/commit/26a9cadf957cc714259c5203e6396cc948a40846)), closes [#54](https://github.com/nNemLab/engram/issues/54)
* **log:** tamper-evident hash-chained event log ([#72](https://github.com/nNemLab/engram/issues/72)) ([17a4861](https://github.com/nNemLab/engram/commit/17a486138f658215dfe2b082fdacac249f558ad3)), closes [#45](https://github.com/nNemLab/engram/issues/45)
* **maintenance:** add corpus re-embed migration for embedding-model changes ([#74](https://github.com/nNemLab/engram/issues/74)) ([29fbdb0](https://github.com/nNemLab/engram/commit/29fbdb063b26f5e6e17640b4f229de5db49db7f2)), closes [#43](https://github.com/nNemLab/engram/issues/43)
* **rag:** temporal retrieval — time-bounded query + episodic timeline ([#75](https://github.com/nNemLab/engram/issues/75)) ([5baaa17](https://github.com/nNemLab/engram/commit/5baaa17db650902f49adcb058847653ffb2669f8)), closes [#40](https://github.com/nNemLab/engram/issues/40)


### Bug Fixes

* **common:** validate embed_dim before vec0 DDL interpolation ([#98](https://github.com/nNemLab/engram/issues/98)) ([ae22ae2](https://github.com/nNemLab/engram/commit/ae22ae24f31b34524468cc75f60db179f3a4942c)), closes [#89](https://github.com/nNemLab/engram/issues/89)
* **init:** seed starter playbooks in eos-init ([#68](https://github.com/nNemLab/engram/issues/68)) ([b068a42](https://github.com/nNemLab/engram/commit/b068a4277f44e6bd6c4688d17f66bcef9f6ce14d)), closes [#67](https://github.com/nNemLab/engram/issues/67)
* **mcp:** bound playbook.run subprocess with a timeout ([#99](https://github.com/nNemLab/engram/issues/99)) ([ae02c41](https://github.com/nNemLab/engram/commit/ae02c41d6d6575284a5899c062fedc2cb555144c)), closes [#88](https://github.com/nNemLab/engram/issues/88)
* **poller:** handle GitHub tree/compare truncation to avoid skipping files ([#107](https://github.com/nNemLab/engram/issues/107)) ([3ddf9bf](https://github.com/nNemLab/engram/commit/3ddf9bf556039e5b62ba3ab66e15515d8305287d)), closes [#86](https://github.com/nNemLab/engram/issues/86)
* **projector:** skip and dead-letter poison event payloads ([#100](https://github.com/nNemLab/engram/issues/100)) ([058bb81](https://github.com/nNemLab/engram/commit/058bb817b6bc5f7347a81122890a1407df6e6056)), closes [#84](https://github.com/nNemLab/engram/issues/84)
* **rag:** convert sqlite-vec L2 distance to true cosine similarity ([#106](https://github.com/nNemLab/engram/issues/106)) ([5537bc1](https://github.com/nNemLab/engram/commit/5537bc1ba260047257110b51c584c2c61c42756b)), closes [#82](https://github.com/nNemLab/engram/issues/82)
* **rag:** default source-tier weights so tier ranking isn't a no-op ([#78](https://github.com/nNemLab/engram/issues/78)) ([c0d6191](https://github.com/nNemLab/engram/commit/c0d61913fc0bd175ae2228380b9dc213413a3f2c))
* **reactor:** skip and dead-letter poison events instead of freezing ([#105](https://github.com/nNemLab/engram/issues/105)) ([4b28c05](https://github.com/nNemLab/engram/commit/4b28c0504a9e888c57cf975a0b03629cd01fcf44)), closes [#101](https://github.com/nNemLab/engram/issues/101)
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
