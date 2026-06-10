# Engram documentation

Reference docs for [engram](../README.md). New to the project? Read them in
order — each builds on the last.

| # | Document | What's inside |
|---|---|---|
| 1 | [Setup](setup.md) | Install, run the daemons, verify the round trip, and troubleshoot. |
| 2 | [Configuration](configuration.md) | `config.yml` reference, the LLM provider, embedding/reranker models, and the CPU-vs-GPU lane. |
| 3 | [Architecture](architecture.md) | Component-by-component internals, the confidence model, and failure/recovery modes. |
| 4 | [MCP tool reference](mcp-tool-reference.md) | Every tool across the seven namespaces (`kb`, `rag`, `research`, `playbook`, `goals`, `sources`, `session`), with arguments. |
| 5 | [Event log schema](event-log-schema.md) | Event types, tables, and the invariants the log guarantees. |

### Related

- [Docker install](../docker/README.md) — run the full stack in containers.
- [Ambient memory plugin](../engram-plugin/README.md) — auto-injected retrieval for Claude Code.
- [Contributing](../CONTRIBUTING.md) · [Security policy](../SECURITY.md) · [Changelog](../CHANGELOG.md)
