# Event log schema

> Part of the [engram documentation](README.md).

Authoritative reference. Schema lives in `schema/` (`001_initial.sql` plus the
gated migrations `002`–`005`); this document explains the *intent* of each
event type.

## Event types

| type | emitted by | payload |
|---|---|---|
| `ingested` | dedup gate | `{hash, title, source_url, kind, source_tier}` |
| `merged` | dedup gate, reactor | `{hash_kept, hash_tombstoned, reason, similarity?}` |
| `superseded` | dedup gate, `kb.resolve_supersede` (accept_upstream) | `{hash_old, hash_new, source_url, revision, reason?}` |
| `contradicted` | poller (dedup gate), `kb.flag_contradiction` | `{hash_a, hash_b, detected_by?, source_url?, id?}` |
| `contradiction_resolved` | `kb.resolve_supersede` (keep_mine) | `{hash_a, hash_b, resolution: 'kept_a', tombstoned_upstream}` |
| `retrieved` | `rag.query` | `{query, hashes, count}` (one per query) |
| `cited` | `rag.cite` | `{hashes, query, turn_id}` |
| `stale_marked` | reactor | `{hash, score}` |
| `refresh_requested` | reactor | `{hash, source_url}` |
| `vault_edit` | watcher | `{path, hash, hash_old, hash_new, diff}` (diff truncated to 8KB) |
| `playbook_run` | playbook.run | `{run_id, playbook, runtime, params, exit_code, run_dir}` |
| `goal_set` | goals tool | `{goal_id, text}` |
| `goal_resolved` | goals tool | `{goal_id}` |
| `source_polled` | poller | `{source_id, candidates_seen, …}` |
| `source_error` | poller | `{source_id, error, retryable}` |
| `source_circuit_broken` | poller | `{source_id, error_count}` |

## Tables (high-level)

- `events` — the log
- `content` — deduplicated content store, addressed by SHA-256
- `content_fts` — FTS5 virtual table over content (auto-maintained via triggers)
- `embeddings` — vec0 virtual table, content_hash → float32[N], where N is
  `rag.embed_dim` (default 384; migrate with `eos reembed`)
- `entities`, `entity_mentions` — semantic memory
- `goals` — active investigations
- `contradictions` — surfaces conflicting content for human resolution
- `retrieval_log` — drives demand-based refresh
- `vault_state` — last-rendered body for each vault path; the watcher's diff base
- `daemon_cursors` — last event ID consumed by each daemon

## Invariants

- The log is tamper-evident: each event (since migration `005`) carries an
  `event_hash` over its canonical fields plus the previous event's hash, forming
  a chain. `eos-verify` walks and validates it; rows predating the chain are
  skipped, not flagged.
- A row in `content` with `tombstoned = 0` is the canonical, current version.
- A row in `vault_state` exists iff the projector has rendered a markdown file
  for the linked content_hash. If the file is deleted on disk, the row may
  briefly be stale until the next render tick recreates the file.
- Every `merged` event is paired with `tombstoned = 1` on the merged-away row.
- Every `content` row's body hashes to its stored `hash` (content is addressed
  by SHA-256 of the normalized body) — including human-edited rows.
- A `vault_edit` records the human edit as a *new content revision*, not an
  in-place mutation: a fresh `is_current = 1, protected = 1` row addressed by the
  edited body is inserted, the prior revision is marked `is_current = 0` with
  `superseded_by` set, and `vault_state` is repointed to the new hash. The
  projector ignores `vault_edit` events, so it never clobbers the human's file.
