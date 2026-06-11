"""The doc-level embedding truncation must be a single shared slice.

The live ingest path (reactor.handlers.on_ingested) and the corpus reembed
migration (maintenance.reembed) both embed a leading prefix of the body, capped
at the same width. If they diverge, a reembed produces vectors that re-ingesting
would not reproduce, so the index stops being reconstructible from the log. These
tests pin the shared helper that both paths route through.
"""
from __future__ import annotations

from engram.rag import chunk as chunker


def test_embed_char_cap_is_eight_chunks_of_chars():
    # 8 chunks * size_tokens tokens * 4 chars/token.
    assert chunker.embed_char_cap(512) == 8 * 512 * 4
    assert chunker.embed_char_cap(512) == 16384
    assert chunker.embed_char_cap(100) == 8 * 100 * 4


def test_embed_prefix_truncates_long_body_to_cap():
    cap = chunker.embed_char_cap(512)
    body = "x" * (cap + 5000)
    prefix = chunker.embed_prefix(body, 512)
    assert len(prefix) == cap
    assert prefix == body[:cap]


def test_embed_prefix_passes_short_body_through_unchanged():
    body = "short body well under the cap"
    assert chunker.embed_prefix(body, 512) == body
