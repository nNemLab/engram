"""Event handlers. Each handler is pure: (conn, event) -> None.
Side effects: mutate DB, append further events, never write to vault directly
(that's the projector's job)."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from .. import log as event_log
from ..common.config import load_config
from ..common.db import transaction
from ..rag import chunk as chunker
from ..rag import embed as embedder
from ..rag._cosine import l2_to_cosine

logger = logging.getLogger("engram.reactor.handlers")


def on_ingested(conn: sqlite3.Connection, evt: event_log.Event) -> None:
    """Embed the new content; near-dup check post-hoc; emit merged if found."""
    cfg = load_config()
    h = evt.payload["hash"]
    row = conn.execute("SELECT body FROM content WHERE hash = ? AND tombstoned = 0", (h,)).fetchone()
    if not row:
        return
    chunks = chunker.chunk_markdown(row["body"], cfg.rag.chunk_size_tokens, cfg.rag.chunk_overlap_tokens)
    if not chunks:
        return
    # Single doc-level embedding for now (chunk-level retrieval is a follow-up).
    # Compute it BEFORE opening the transaction so the (potentially slow) embed
    # never runs while holding the DB lock / an open write transaction.
    emb = embedder.embed_one(chunker.embed_prefix(row["body"], cfg.rag.chunk_size_tokens))
    # #152: the embed write AND any near-dup tombstone/delete/merged-event are one
    # atomic unit -- a crash/error mid-sequence rolls back ALL of it (never an
    # embedded row with no merged event, never a tombstone with the embedding
    # still present). When the reactor loop calls this inside its per-event
    # transaction, this `with transaction` joins it, so the effects also commit
    # atomically with the cursor advance (#153).
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (content_hash, embedding) VALUES (?, ?)",
            (h, emb),
        )
        # Post-hoc near-dup check against the nearest OTHER entry. sqlite-vec KNN
        # requires an explicit `k = ?` constraint and rejects extra non-MATCH
        # predicates in the same query (so `... AND content_hash != ?` raises). We
        # fetch the 2 nearest — k=2 covers self plus the closest neighbour — and
        # drop self in Python.
        rows = conn.execute(
            "SELECT content_hash, distance FROM embeddings "
            "WHERE embedding MATCH ? "
            "AND content_hash IN (SELECT hash FROM content WHERE tombstoned = 0 AND is_current = 1) "
            "AND k = 2 ORDER BY distance",
            (emb,),
        ).fetchall()
        near = next((r for r in rows if r["content_hash"] != h), None)
        if near:
            sim = l2_to_cosine(float(near["distance"]))
            if sim >= cfg.rag.near_dup_threshold:
                kept = near["content_hash"]
                conn.execute("UPDATE content SET tombstoned = 1 WHERE hash = ?", (h,))
                conn.execute("DELETE FROM embeddings WHERE content_hash = ?", (h,))
                event_log.append(
                    conn, "merged",
                    {"hash_kept": kept, "hash_tombstoned": h, "reason": "near_dup_post_embed", "similarity": sim},
                    actor="reactor",
                )
                logger.info("post-embed near-dup: tombstoned %s into %s (sim=%.3f)", h, kept, sim)


def on_retrieved(conn: sqlite3.Connection, evt: event_log.Event) -> None:
    """Demand-driven staleness: for each retrieved content hash, if it's past
    its TTL fraction, bump staleness_score and emit refresh_requested.

    Payload shape: {query, hashes: [...], count}. Older single-hash payloads
    are tolerated for backward-compat with any pre-fix events in the log.
    """
    cfg = load_config()
    payload = evt.payload
    hashes: list[str] = payload.get("hashes") or (
        [payload["hash"]] if "hash" in payload else []
    )
    if not hashes:
        return
    now_utc = datetime.now(UTC)
    threshold = cfg.reactor.retrieval_staleness_threshold

    # #152: the staleness bumps and the stale_marked / refresh_requested events
    # are one atomic unit per invocation -- a failure partway rolls back ALL of
    # them (never a bumped score with no event, or vice versa). Joins the reactor
    # loop's per-event transaction when called from the loop (#153).
    with transaction(conn):
        rows = conn.execute(
            f"SELECT hash, fetched_at, ttl_days, source_url FROM content "
            f"WHERE hash IN ({','.join('?' * len(hashes))}) AND tombstoned = 0",
            hashes,
        ).fetchall()
        for row in rows:
            if not row["fetched_at"] or not row["ttl_days"]:
                continue
            try:
                ts = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            age_days = (now_utc - ts).total_seconds() / 86400.0
            fraction = age_days / max(1, row["ttl_days"])
            if fraction < threshold:
                continue
            new_score = min(1.0, fraction)
            conn.execute("UPDATE content SET staleness_score = ? WHERE hash = ?",
                         (new_score, row["hash"]))
            event_log.append(conn, "stale_marked",
                             {"hash": row["hash"], "score": new_score}, actor="reactor")
            if row["source_url"]:
                event_log.append(conn, "refresh_requested",
                                 {"hash": row["hash"], "source_url": row["source_url"]},
                                 actor="reactor")


HANDLERS = {
    "ingested":  on_ingested,
    "retrieved": on_retrieved,
}
