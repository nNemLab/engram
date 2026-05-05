# Engram

Claude Code as kernel. Event log canonical. Obsidian vault projected. One MCP server.

This is the implementation tree for the design described in
`../engram-build-structure.md` (revised). It is staged to disk; nothing has run.

## Quick start

```bash
# 1. Install (editable; brings in all deps).
pip install -e .

# 2. Initialize. Creates ~/.engram/{config.yml,.env,vault,db.sqlite}.
./bin/aos-init

# 3. Wire the MCP server into Claude Code (one-time).
claude mcp add -s user engram ~/.engram/.venv/bin/engram-mcp

# 4. Launch the daemons (separate shells, or use the systemd units in systemd/).
engram-projector   # log -> vault markdown
engram-watcher     # vault edits -> log
engram-reactor     # embed-on-ingest, staleness, near-dup post-check
```

## What's here

```
engram/
├── schema/001_initial.sql        # event log, content, fts5, embeddings hookup
├── src/engram/
│   ├── common/                   # config, db connection, paths
│   ├── log.py                    # event log read/write
│   ├── dedup.py                  # the gate: SHA-256 + cosine
│   ├── rag/                      # chunk, embed, hybrid query
│   ├── mcp_server/               # one MCP server, namespaced tools
│   │   └── tools/                # kb / rag / research / playbook / goals
│   ├── projector/                # log -> vault markdown daemon
│   ├── watcher/                  # vault edits -> log events
│   └── reactor/                  # event-triggered work
├── vault-template/               # initial Obsidian vault layout
├── playbooks/{scratch,curated}/  # Jupyter (default) / Marimo (curated)
├── bin/                          # aos, aos-init, aos-mcp, ...
└── docs/                         # architecture, schema, MCP tool reference, setup
```

## Architecture in one paragraph

Every content write flows through `dedup.gate()` and produces an `ingested`
event in a SQLite append-only log. The reactor embeds new content and runs a
post-hoc near-dup check that may emit `merged`. The projector tails the log
and renders markdown into an Obsidian vault, recording exactly what bytes it
wrote. The watcher tails the vault filesystem; when a human edits a note in
Obsidian, the diff against the last-rendered version becomes a `vault_edit`
event and the human's body becomes authoritative. RAG indexes the content
table directly (sqlite-vec + FTS5, fused via RRF, ranked by source-tier and
recency-decayed confidence).

See `docs/architecture.md` for the long form.

## Dependencies

Single Python venv. Heaviest dep is `sentence-transformers` (pulls torch).
If you want a smaller install, swap the embed model in `config.yml` for an
ONNX/`fastembed` runtime — the contract in `rag/embed.py` is just
`encode(texts, normalize_embeddings=True) -> np.ndarray[float32]`.
