# Setup

## Prerequisites

- Python 3.11+
- Obsidian (for the human surface)
- Optional: `papermill` for Jupyter playbooks; `marimo` for curated playbooks
  (both already in `pyproject.toml` deps)

## Steps

### 1. Install

```bash
cd /data/projects/agenticOS/engram
pip install -e .              # or: uv pip install -e .
```

This pulls in `mcp`, `sqlite-vec`, `sentence-transformers`, `watchdog`,
`papermill`, `marimo`, and creates the `engram-*` console scripts.

### 2. Initialize

```bash
./bin/aos-init
```

Creates:
- `~/.engram/config.yml` — copy of `config.example.yml`, edit paths if needed
- `~/.engram/.env` — copy of `.env.example`, fill in API keys
- `~/.engram/vault/` — Obsidian vault scaffolded from `vault-template/`
- `~/.engram/db.sqlite` — schema applied, `vec0` table created

### 3. Open the vault in Obsidian

Point Obsidian at `~/.engram/vault`. Install plugins per
`~/.engram/vault/.obsidian/README.md`.

### 4. Wire the MCP server into Claude Code

Use the `claude mcp add` CLI (settings.json does not accept `mcpServers` —
config lives in `~/.claude.json`):

```bash
claude mcp add -s user engram ~/.engram/.venv/bin/engram-mcp
claude mcp list   # should show: engram: ... ✓ Connected
```

Restart Claude Code. The `kb.*`, `rag.*`, `research.*`, `playbook.*`, and
`goals.*` tools should now appear (verify with `/mcp` inside a session).

### 5. Run the daemons

Four long-running processes. Pick one:

**Quick start (four shells):**
```bash
engram-projector   # log -> vault markdown
engram-watcher     # vault edits -> log
engram-reactor     # embed, staleness, near-dup post-check
engram-poller      # poll registered sources on schedule
```

**Production (systemd user units):**
Copy `systemd/engram-{projector,watcher,reactor,poller}.service` into
`~/.config/systemd/user/`, then:
```bash
systemctl --user daemon-reload
systemctl --user enable --now engram-projector engram-watcher engram-reactor engram-poller
```

### 6. Verify

```bash
./bin/eos-status
```

Should report 0 events, 0 content, daemon cursors at 0.

### 7. Optional: register a source

Source curation pulls "official documentation"-shaped feeds into the KB on
schedule. Register one via the `eos-source` CLI or the `sources.add` MCP tool:

```bash
./bin/eos-source add docker-docs-linux \
  --name "Docker Docs (Linux)" \
  --adapter sitemap \
  --url https://docs.docker.com/sitemap.xml \
  --include '*/engine/*' --include '*/desktop/install/linux*' \
  --schedule 7d
```

Four adapter types in v0.3:

- `sitemap` — site with a public `sitemap.xml`.
- `github-repo` — public docs-as-markdown repo (uses GitHub compare API).
- `mediawiki-api` — any MediaWiki wiki via `/api.php` (Fandom, PCGamingWiki,
  Wikipedia). Walks via `list=allpages`; tracks updates via
  `list=recentchanges`. Sends `maxlag=5` automatically.
- `urls` — manually curated list of URLs for sites with no sitemap and no API.

Identify the operator to providers: set `poller.http.contact` in
`~/.engram/config.yml` (an email or repo URL). Used in the `User-Agent`
header so well-behaved sources can route abuse reports to you instead of
blocking blindly.

See `docs/superpowers/specs/2026-05-06-adapter-expansion-design.md` for the
adapter contracts and per-adapter politeness defaults; `mcp-tool-reference.md`
for the `sources.*` namespace.

Try the round trip:
```bash
# In a Claude Code session, ask the agent to call kb.write with some content.
# Then:
./bin/aos-status         # event count should be 1+
ls ~/.engram/vault/050-kb/    # the projector should have rendered a file
./bin/aos-query "your query"   # RAG should return the new content
```

## Troubleshooting

- **`engram-mcp` not found** — `pip install -e .` from the project root.
- **`sqlite-vec` extension load failure** — older SQLite. The `sqlite-vec`
  package ships its own loadable extension; if it still fails, check
  `python -c "import sqlite3; print(sqlite3.sqlite_version)"` ≥ 3.41.
- **Embeddings download is slow** — first run downloads `all-MiniLM-L6-v2`
  (~80MB) to `~/.cache/huggingface/`. Pre-warm with
  `python -c "from engram.rag.embed import embed_one; embed_one('warm')"`.
- **Vault projector not writing** — check `~/.engram/db.sqlite` exists,
  check `daemon_cursors` is being updated, check the projector's stderr.
