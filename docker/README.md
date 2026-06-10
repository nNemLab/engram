# Engram — Docker install

Run the full engram stack — the MCP server (over HTTP) plus the reactor,
projector, watcher, poller, and digest scheduler, all sharing one SQLite DB —
in a single supervised container, alongside an internal-only SearXNG service for
web research. Any HTTP-capable MCP client connects over `http://localhost:8765/mcp`.

## Prerequisites

- Docker Engine + the Compose plugin (`docker compose version`).
- `openssl` (to generate the SearXNG secret).
- An MCP client (e.g. Claude Code) to connect.

No host Python, `uv`, or model download is needed — the image bakes the
embedding and reranker models so the first run is fully offline.

## Quick start

From this `docker/` directory:

```bash
cp .env.example .env
```

Generate a SearXNG secret (replaces the placeholder in `searxng/settings.yml`):

```bash
sed -i "s|REPLACE_ME_WITH_RANDOM_HEX_64|$(openssl rand -hex 32)|" searxng/settings.yml
```

Build and start the stack:

```bash
docker compose up -d --build
```

Wire the MCP server into your client (Claude Code shown):

```bash
claude mcp add --transport http engram http://localhost:8765/mcp
```

## SECURITY — read before exposing anything

- **Loopback only.** The compose file publishes the MCP port as
  `127.0.0.1:8765:8765` — reachable only from the host, never the LAN.
- **`playbook.run` is arbitrary code execution.** The engram MCP server runs
  Jupyter notebooks in-process. Anyone who can reach the `/mcp` endpoint can run
  arbitrary Python inside the container. The HTTP transport has **no
  authentication**.
- **Do NOT publish to `0.0.0.0` or a LAN** without putting an authenticating
  reverse proxy (mTLS, OAuth, or a token gateway) in front of it. Changing the
  port mapping to expose it beyond loopback removes the only access control.
- SearXNG is internal-only (`expose:` not `ports:`) and is not reachable from
  the host.

## LLM provider (optional, provider-agnostic)

engram's core needs no LLM — the kernel reasons. Only the synthesis playbooks
(research synthesis, daily digest) call one, and only when configured. Point
them at **any OpenAI-compatible endpoint** via `docker/.env`:

- `ENGRAM_LLM_BASE_URL` — e.g. `https://api.openai.com/v1`,
  `http://localhost:1234/v1` (LM Studio), `https://api.anthropic.com/v1`
  (Anthropic's OpenAI-compatible API).
- `ENGRAM_LLM_API_KEY`
- `ENGRAM_LLM_MODEL` — e.g. `gpt-4o-mini`.

With none set, synthesis falls back to structural (non-LLM) output. These are
passed through to the container from `.env`; `env_file: .env` makes compose
require the file to exist (hence `cp .env.example .env` above).

## Data and the vault

- All runtime state (DB, playbooks, logs) lives in the named volume
  `engram-data`, mounted at `/data`.
- The Obsidian **vault** is bind-mounted so you can open it directly. By default
  it maps `./vault` on the host to `/data/vault` in the container. Override the
  host path with `ENGRAM_VAULT`:

  ```bash
  ENGRAM_VAULT=/path/to/my/vault docker compose up -d
  ```

  On first run the container scaffolds the vault from the bundled template if the
  target is empty. Open the host dir in Obsidian and install the Dataview +
  Templater plugins.
- **File ownership:** the container runs as uid/gid `1000`. The entrypoint starts
  as root only to `chown` the data volume and the bind-mounted vault, then drops
  privileges. If your host user isn't uid 1000 and you want to edit the vault
  files directly, set `PUID`/`PGID` to your `id -u`/`id -g`:

  ```bash
  PUID=$(id -u) PGID=$(id -g) docker compose up -d
  ```

## Smoke test

After `docker compose up -d --build`, wait for startup, then:

```bash
# -L follows the /mcp -> /mcp/ redirect (MCP clients do this automatically).
curl -sL -X POST http://127.0.0.1:8765/mcp/ \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

Expect an SSE `data:` line whose JSON-RPC result names `engram` in `serverInfo`. Tear down with:

```bash
docker compose down
```

## Uninstall

Run the dual-mode uninstaller from the repo root — it auto-detects whether you
have a native or Docker install (or both) and tears down accordingly:

```bash
./bin/eos-uninstall
```

For the Docker install it offers to export the DB from the `engram-data` volume,
runs `docker compose down -v` (removing containers, network, and the volume),
removes the MCP registration, and optionally drops the host vault dir and the
`engram:local` image. A typed-`DELETE` confirmation gate guards the destructive
steps.
