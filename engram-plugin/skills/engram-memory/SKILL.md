---
name: engram-memory
description: How to use engram's ambient memory — the auto-injected "Relevant memory" / "session priming" blocks, citing what you use, and capturing durable knowledge. Active whenever engram memory context appears or you learn something worth keeping.
---

# Engram memory

Engram is your long-term memory. A `UserPromptSubmit` hook injects a **"Relevant memory"**
block before most turns, and a `SessionStart` hook injects a **"session priming"** block.
You do not call retrieval to get these — they arrive automatically. Your job is to *use*
them well and to *feed* memory back.

## Using injected memory

- When a **"Relevant memory"** block is present, treat it as **authoritative grounding** and
  prefer it over your own recollection. It was calibrated: if engram had nothing relevant, it
  injects nothing — so when a block IS present, it's worth trusting.
- When you ground an answer in it, end the answer with a short, visible attribution line so the
  human can see what was used and so engram can record it:

  ```
  _grounded in: [[Note Title]] `[a1b2c3d4e5f6]`_
  ```

  Copy the content-hash id in backtick-brackets verbatim from the injected block — each hit there
  carries a 12-character hash id, e.g. `` `[a1b2c3d4e5f6]` ``. This line is load-bearing: a
  Stop hook reads it to record which memory you used.
- **No block / nothing relevant (NONE):** answer from your own knowledge, say so briefly, and
  if it's a topic engram *should* know, offer to capture it (see below).

## Drilling in

The injected block is a budgeted summary. To see more of a specific hit, call **`rag.query`**
with `level="section"` or `level="full"` (and `since=` for time-bounded recall). Don't re-run a
broad query just to get the initial context — that already arrived via the hook.

## Capturing knowledge

When the human teaches you something durable — a decision, a fix, a fact worth remembering —
write it back with **`kb.write`**. Capture durable knowledge, not chatter; the dedup gate
handles near-duplicates. This is how memory earns its keep over time.

## On other MCP clients

These hooks are Claude Code-specific. On any other MCP client there's no auto-injection: call
`session.prime` at the start and `rag.query` before answering questions engram might know.
