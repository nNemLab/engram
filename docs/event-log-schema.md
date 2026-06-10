# Event log schema

> Part of the [engram documentation](README.md).

Authoritative reference. Schema lives at `schema/001_initial.sql`; this document
explains the *intent* of each event type.

## Event types

| type | emitted by | payload |
|---|---|---|
| `ingested` | dedup gate | `{hash, title, source_url, kind, source_tier}` |
| `merged` | dedup gate, reactor | `{hash_kept, hash_tombstoned, reason, similarity?}` |
| `contradicted` | agent, reactor | `{hash_a, hash_b, id}` |
| `retrieved` | rag.query | `{hash, query}` (one per hit) |
| `stale_marked` | reactor | `{hash, score}` |
| `refresh_requested` | reactor | `{hash, source_url}` |
| `goal_set` | goals tool | `{goal_id, text}` |
| `goal_resolved` | goals tool | `{goal_id}` |
| `vault_edit` | watcher | `{path, hash, diff}` (diff truncated to 8KB) |
| `playbook_run` | playbook.run | `{run_id, playbook, runtime, params, exit_code, run_dir}` |
| `system` | any daemon | `{component, message, level}` |

## Tables (high-level)

- `events` — the log
- `content` — deduplicated content store, addressed by SHA-256
- `content_fts` — FTS5 virtual table over content (auto-maintained via triggers)
- `embeddings` — vec0 virtual table, content_hash → float32[384]
- `entities`, `entity_mentions` — semantic memory
- `goals` — active investigations
- `contradictions` — surfaces conflicting content for human resolution
- `retrieval_log` — drives demand-based refresh
- `vault_state` — last-rendered body for each vault path; the watcher's diff base
- `daemon_cursors` — last event ID consumed by each daemon

## Invariants

- A row in `content` with `tombstoned = 0` is the canonical, current version.
- A row in `vault_state` exists iff the projector has rendered a markdown file
  for the linked content_hash. If the file is deleted on disk, the row may
  briefly be stale until the next render tick recreates the file.
- Every `merged` event is paired with `tombstoned = 1` on the merged-away row.
- `vault_edit` events update both `content.body` AND `vault_state.rendered_body`
  in the same transaction, so the next projector pass won't clobber the edit.
