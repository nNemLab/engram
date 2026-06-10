# engram-memory plugin

Ambient memory for Claude Code — the plugin auto-injects calibrated retrieval
into every turn so the agent always has relevant context without you having to
ask.

## What it does

Three hooks work together:

- **UserPromptSubmit** — on every turn, queries the engram grounding daemon with
  your prompt and prepends a "Relevant memory" block to the message if anything
  scores above the confidence threshold.
- **SessionStart** — injects a brief priming block at the start of a new session
  so the agent is oriented before the first user turn.
- **Stop** — records which memory fragments were used during the session back
  into the event log (retrieval telemetry).

The `engram-memory` skill (loaded alongside the hooks) governs how the agent
interprets and cites injected memory.

## Prerequisites

- `jq` and `curl` on `PATH`
- The engram **grounding daemon** running and reachable:
  - Native: `engram-rag serve` (or the `engram-rag` systemd user unit)
  - Docker: the engram stack publishes the daemon at `127.0.0.1:8770`
- The MCP server already registered in Claude Code (see the main README)

## Install

**Session-only (try it out):**

```bash
claude --plugin-dir ./engram-plugin
```

**Persistent (loads on every session):**

```bash
cp -r engram-plugin ~/.claude/skills/engram-memory
```

Restart Claude Code after copying. The hooks are active from the next session.

## Config

The hooks read one environment variable:

| Variable | Default | Purpose |
|---|---|---|
| `ENGRAM_GROUNDING_URL` | `http://127.0.0.1:8770` | Override if the daemon runs on a different host or port |

Set it in your shell profile or in `.env` before starting Claude Code.

## Failure behaviour

The hooks **fail open** — if the grounding daemon is unreachable or returns an
error, the hook exits cleanly and no injection is added. Your turn is never
blocked by a daemon outage.
