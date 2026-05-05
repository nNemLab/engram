# Engram — Build Structure

> Claude Code as an operating system kernel, orchestrating RAG, memory, playbooks, research, knowledge management, and tooling through MCP.

---

## Directory Layout

```
~/.engram/
├── claude-code.config.json          # Claude Code MCP + hook configuration
├── .env                             # API keys (Tavily, Anthropic, etc.)
│
├── vault/                           # Obsidian vault — canonical knowledge store
│   ├── .obsidian/
│   │   ├── plugins/
│   │   │   ├── dataview/
│   │   │   └── templater/
│   │   └── app.json
│   ├── 000-inbox/                   # Drop zone — FS watcher triggers ingest
│   ├── 010-episodes/                # Episodic memory — daily conversation logs
│   │   └── 2026-05-05.md
│   ├── 020-entities/                # Semantic memory — entity/concept notes
│   │   ├── tools/
│   │   ├── people/
│   │   └── concepts/
│   ├── 030-research/                # Research outputs — provenance-tagged
│   │   ├── web/
│   │   ├── academic/
│   │   └── docs/
│   ├── 040-playbooks/               # Runbook templates (Obsidian-readable)
│   │   ├── pcap-analysis.md
│   │   ├── malware-triage.md
│   │   ├── osint-collection.md
│   │   └── incident-response.md
│   ├── 050-kb/                      # Knowledge base — deduplicated entries
│   │   ├── _index.yml               # SHA-256 + SimHash registry
│   │   └── ...entries.md
│   └── _templates/                  # Templater templates
│       ├── episode.md
│       ├── entity.md
│       ├── research-note.md
│       └── kb-entry.md
│
├── rag/                             # RAG engine
│   ├── chromadb/                    # Vector store data directory
│   ├── tantivy-idx/                 # BM25 sparse index
│   ├── ingest.sh                    # Chunking + embedding pipeline
│   ├── query.sh                     # Hybrid retrieval (vector + BM25 → RRF)
│   └── config.yml                   # Chunk sizes, overlap, model, collection map
│
├── playbooks/                       # Jupyter notebook store
│   ├── templates/
│   │   ├── pcap-analysis.ipynb
│   │   ├── malware-triage.ipynb
│   │   ├── osint-collection.ipynb
│   │   └── ir-playbook.ipynb
│   ├── runs/                        # Papermill output directory
│   │   └── 2026-05-05_pcap_xyz/
│   ├── runner.sh                    # Papermill headless executor
│   └── grounding.sh                 # RAG context injection pre-execution
│
├── research/                        # Research system
│   ├── firecrawl/
│   │   └── docker-compose.yml       # Self-hosted Firecrawl
│   ├── searxng/
│   │   └── settings.yml             # SearXNG instance config (optional)
│   ├── fetch.sh                     # Unified fetch: web, arxiv, docs
│   └── provenance.sh                # Source tagging + dedup pre-check
│
├── kb/                              # Knowledge base tooling
│   ├── dedup.sh                     # SHA-256 exact + SimHash near-dup
│   ├── merge.sh                     # Overlapping entry merger
│   ├── refresh.sh                   # Staleness scorer + re-fetch trigger
│   └── simhash.sh                   # SimHash fingerprint generator
│
├── mcp-servers/                     # Custom MCP server implementations
│   ├── mcp-obsidian/
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── src/
│   │       └── index.ts             # Vault CRUD, search, frontmatter queries
│   ├── mcp-rag/
│   │   └── src/
│   │       └── index.ts             # query, ingest, reindex tools
│   ├── mcp-research/
│   │   └── src/
│   │       └── index.ts             # fetch, search, provenance tools
│   ├── mcp-playbook/
│   │   └── src/
│   │       └── index.ts             # run, list, parameterize tools
│   ├── mcp-kb/
│   │   └── src/
│   │       └── index.ts             # dedup, query, refresh tools
│   └── mcp-tools/
│       └── src/
│           └── index.ts             # CLI wrappers: r2, tshark, binwalk, foremost
│
├── automation/                      # Automation layer
│   ├── crontab.conf                 # Cron definitions
│   ├── watcher.sh                   # inotifywait filesystem watcher
│   ├── git-hooks/
│   │   ├── pre-commit               # Lint playbooks, validate frontmatter
│   │   └── post-commit              # Re-index KB, update SimHash registry
│   ├── pipelines/
│   │   ├── research-ingest.yml      # Research → Dedup → KB → RAG reindex
│   │   ├── memory-consolidate.yml   # Episodic → summarize → Semantic merge
│   │   └── staleness-refresh.yml    # Score KB → re-fetch stale → Dedup → KB
│   ├── pipeline-runner.sh           # YAML pipeline executor
│   └── healthcheck.sh              # Watchdog: MCP, ChromaDB, Jupyter
│
└── bin/                             # Convenience scripts on PATH
    ├── aos                          # Main CLI entry point
    ├── aos-ingest                   # Manual ingest trigger
    ├── aos-query                    # RAG query from terminal
    ├── aos-research                 # Kick off research pipeline
    ├── aos-playbook                 # Run a playbook by name
    └── aos-status                   # Health dashboard
```

---

## Layer Architecture

### Layer 0 — Kernel: Claude Code

Claude Code is the runtime. It reads `claude-code.config.json` to discover MCP servers, then orchestrates everything through tool calls. The config wires up all six MCP servers:

```jsonc
// claude-code.config.json
{
  "mcpServers": {
    "obsidian":  { "command": "node", "args": ["mcp-servers/mcp-obsidian/dist/index.js"],  "env": { "VAULT_PATH": "./vault" } },
    "rag":       { "command": "node", "args": ["mcp-servers/mcp-rag/dist/index.js"],       "env": { "CHROMA_PATH": "./rag/chromadb" } },
    "research":  { "command": "node", "args": ["mcp-servers/mcp-research/dist/index.js"],  "env": { "FIRECRAWL_URL": "http://localhost:3002" } },
    "playbook":  { "command": "node", "args": ["mcp-servers/mcp-playbook/dist/index.js"],  "env": { "NOTEBOOKS": "./playbooks/templates" } },
    "kb":        { "command": "node", "args": ["mcp-servers/mcp-kb/dist/index.js"],        "env": { "KB_PATH": "./vault/050-kb" } },
    "tools":     { "command": "node", "args": ["mcp-servers/mcp-tools/dist/index.js"] }
  }
}
```

Claude Code doesn't need a wrapper — it **is** the shell. Every `aos-*` bin script is just a convenience shortcut that invokes `claude-code` with a pre-formed prompt + relevant MCP context.

---

### Layer 1 — Subsystems

#### RAG Engine (`rag/`)

The retrieval pipeline. Two parallel indexes over the same Obsidian vault content:

- **Dense path**: `ingest.sh` chunks vault markdown (semantic boundaries, 512-token sliding window fallback), embeds via `sentence-transformers/all-MiniLM-L6-v2`, stores in ChromaDB.
- **Sparse path**: Same chunks indexed into tantivy for BM25 keyword retrieval.
- **Query path**: `query.sh` runs both retrievers, fuses results via Reciprocal Rank Fusion, returns top-k within a configurable token budget.

The MCP RAG server exposes three tools: `rag_query`, `rag_ingest`, `rag_reindex`.

#### Memory System (`vault/010-episodes/`, `vault/020-entities/`)

Three tiers, all backed by the Obsidian vault:

- **Working memory**: The Claude Code context window itself. No persistence needed — it *is* the active state.
- **Episodic memory**: After each session, Claude Code writes a summary to `010-episodes/YYYY-MM-DD.md` via the MCP Obsidian server. YAML frontmatter carries tags, participants, and topic clusters.
- **Semantic memory**: The `memory-consolidate.yml` automation pipeline runs nightly. It scans recent episodes, extracts entities and concepts, and upserts notes in `020-entities/` with backlinks. Obsidian's graph view becomes a navigable knowledge graph. Dataview queries surface connections.

Consolidation uses a summarize-then-merge strategy: older episodes get progressively compressed while entity notes accumulate detail over time.

#### Playbook System (`playbooks/`)

Parameterized Jupyter notebooks executed headlessly:

- **Templates** in `playbooks/templates/` are `.ipynb` files with Papermill parameter cells. Example: `pcap-analysis.ipynb` accepts `pcap_path`, `filter_expr`, `output_dir`.
- **Grounding**: Before execution, `grounding.sh` queries the RAG engine for context relevant to the playbook parameters and injects it as a grounding cell. This is the NotebookLM-style behavior — outputs are grounded in your existing knowledge base.
- **Execution**: `runner.sh` calls Papermill, writes output notebooks to `playbooks/runs/`, then triggers the `research-ingest.yml` pipeline to dedup and store results in the KB.
- **Obsidian mirror**: Each playbook template has a parallel `.md` in `vault/040-playbooks/` for browsing and annotation in Obsidian.

#### Research System (`research/`)

Multi-source intelligence gathering with provenance:

- **Web**: Self-hosted Firecrawl (already running in Docker) for structured extraction. Tavily API for search. Optional SearXNG for privacy-respecting meta-search.
- **Academic**: `arxiv` CLI for paper fetch, Semantic Scholar API for citation graph traversal.
- **Provenance**: Every artifact gets a YAML header: `source_url`, `fetched_at`, `confidence`, `content_hash`. Before writing to the KB, `provenance.sh` runs a dedup pre-check against the SimHash registry.

The MCP Research server exposes: `research_web`, `research_academic`, `research_fetch_url`.

#### Knowledge Base (`vault/050-kb/`, `kb/`)

Content-addressed store with deduplication — the single source of truth:

- **Exact dedup**: SHA-256 of normalized content. Collisions rejected immediately.
- **Near dedup**: SimHash fingerprinting. Entries with hamming distance < 3 flagged for auto-merge. `merge.sh` takes the newer metadata, unions the content, and tombstones the older entry.
- **Staleness**: Every entry has a `staleness_score` in frontmatter, incremented by `refresh.sh` on a cron schedule. When score exceeds threshold, the entry's `source_url` is dispatched back to Research for re-fetch, completing the cycle.
- **Index**: `_index.yml` is the flat registry mapping content hashes to vault paths, SimHash fingerprints, and staleness scores.

#### MCP Layer (`mcp-servers/`)

Each subsystem has its own MCP server. All use the TypeScript MCP SDK with stdio transport. The pattern is consistent:

1. Server exposes 2–4 tools scoped to its subsystem.
2. Tools call the bash scripts in their respective directories.
3. Results returned as structured JSON for Claude Code to reason over.

`mcp-tools` is the catch-all for native CLI wrappers: radare2 disassembly, tshark packet parsing, binwalk entropy analysis, foremost carving — all wrapped as MCP-callable tools.

---

### Layer 2 — Components

Each subsystem breaks into three focused components (visible in the tree visualization). These map directly to the scripts and modules in the directory structure above. The key design principle: every component either **produces** data (Research, Episodic Memory, Playbook Runner) or **refines** data (Dedup Engine, SimHash, RRF Reranker), and the Knowledge Base sits at the convergence point.

---

### Layer 3 — Automation

The bottom layer spans all branches. Nothing here is subsystem-specific — each automation component operates across the full tree:

**Cron Scheduler** (`crontab.conf`)
```
0 3 * * *   ~/.engram/automation/pipeline-runner.sh memory-consolidate.yml
0 4 * * *   ~/.engram/automation/pipeline-runner.sh staleness-refresh.yml
*/30 * * * * ~/.engram/rag/ingest.sh --incremental
*/5 * * * *  ~/.engram/automation/healthcheck.sh
```

**Filesystem Watcher** (`watcher.sh`): `inotifywait -mr` on `vault/000-inbox/`. Any new file triggers: identify type → route to appropriate ingest (research note, raw data, playbook output) → dedup → KB → RAG reindex.

**Git Hooks** (`git-hooks/`): The vault is a git repo. Pre-commit validates YAML frontmatter and lints playbook markdown. Post-commit triggers KB re-index and updates the SimHash registry.

**Task Queue**: `ts` (task spooler) for long-running jobs — bulk embedding runs, multi-page crawls, batch notebook execution. Prevents resource contention.

**Pipeline Composer** (`pipelines/*.yml`): Declarative YAML that chains subsystem operations:

```yaml
# research-ingest.yml
name: research-ingest
steps:
  - tool: research.fetch
    params: { query: "$INPUT" }
    output: raw_content

  - tool: kb.dedup_check
    params: { content: "$raw_content" }
    output: dedup_result
    on_duplicate: skip

  - tool: obsidian.create_note
    params:
      path: "050-kb/$HASH.md"
      content: "$raw_content"
      frontmatter: { source: "$SOURCE", fetched_at: "$NOW" }

  - tool: rag.ingest
    params: { path: "050-kb/$HASH.md" }

  - notify: "Ingested: $TITLE"
```

**Health Monitor** (`healthcheck.sh`): Pings each MCP server, checks ChromaDB responsiveness, verifies Jupyter kernel availability, confirms Firecrawl container is up. Logs to `vault/010-episodes/` as system events. Auto-restarts failed services via systemd or Docker.

---

## Data Flow Summary

```
                    ┌─────────────────────────────┐
                    │        Claude Code           │
                    │     (kernel / runtime)       │
                    └──────┬──────────────┬────────┘
                           │              │
                    ┌──────▼──────┐ ┌─────▼──────┐
                    │  Research   │ │  Playbooks  │
                    │  System     │ │  System     │
                    └──────┬──────┘ └─────┬──────┘
                           │              │
                           ▼              ▼
                    ┌─────────────────────────────┐
                    │     Dedup Engine             │
                    │  SHA-256 exact + SimHash     │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │      Knowledge Base          │
                    │   (Obsidian vault/050-kb)    │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │       RAG Engine             │
                    │  ChromaDB + tantivy → RRF    │
                    └──────────────┬──────────────┘
                                   │
                           ┌───────▼───────┐
                           │  Claude Code  │
                           │  (retrieval)  │
                           └───────┬───────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │      Memory System           │
                    │  episode → semantic → KB     │
                    └─────────────────────────────┘

    ═══════════════════════════════════════════════
    ║          AUTOMATION LAYER                   ║
    ║  cron · watcher · hooks · queue · pipeline  ║
    ═══════════════════════════════════════════════
```

The critical dedup point: **nothing enters the KB without passing through the Dedup Engine**. Research, Playbook outputs, and Memory consolidation all converge at the same gate. This is how duplicate data collection is eliminated — every subsystem that produces knowledge writes to one store through one filter.

---

## Build Order

For bootstrapping the system from scratch, build in dependency order:

1. **Obsidian vault** — Create the directory structure, install Dataview + Templater plugins, set up templates. This is the foundation everything writes to.
2. **MCP Obsidian server** — First MCP server. Once Claude Code can read/write the vault, it can assist with building everything else.
3. **Knowledge Base tooling** — `dedup.sh`, `simhash.sh`, `merge.sh`, `_index.yml`. The gate must exist before anything writes through it.
4. **RAG Engine** — ChromaDB + tantivy setup, `ingest.sh` over the vault, `query.sh` for retrieval. Wire up MCP RAG server.
5. **Memory System** — Episode logging (simple: write a daily note post-session), then the consolidation pipeline later.
6. **Research System** — Firecrawl is already running. Add `fetch.sh`, `provenance.sh`, wire up MCP Research server.
7. **Playbook System** — Jupyter + Papermill, template notebooks, grounding injection, MCP Playbook server.
8. **MCP Tools server** — CLI wrappers for existing native tools.
9. **Automation layer** — Cron, watcher, git hooks, pipelines, healthcheck. This comes last because it orchestrates everything above.
10. **`bin/` convenience scripts** — Thin wrappers once everything is wired.
