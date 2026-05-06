"""Dedup gate. Every content write goes through here.

Outcomes:
  - 'new'        : content is novel, inserted, ingested event emitted
  - 'exact_dup'  : SHA-256 collision, no-op (existing hash returned)
  - 'near_dup'   : embedding cosine > threshold, merged into existing entry
  - 'contradicts': flagged for human resolution (high overlap + high disagreement signal — stub for now)
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from .common.config import load_config
from . import log as event_log

Outcome = Literal["new", "exact_dup", "near_dup", "contradicts", "superseded"]


@dataclass
class GateResult:
    outcome: Outcome
    hash: str
    merged_into: str | None = None


_WS = re.compile(r"\s+")


def normalize(body: str) -> str:
    return _WS.sub(" ", body).strip().lower()


def content_hash(body: str) -> str:
    return hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()


def find_exact(conn: sqlite3.Connection, h: str) -> str | None:
    row = conn.execute(
        "SELECT hash FROM content WHERE hash = ? AND tombstoned = 0",
        (h,),
    ).fetchone()
    return row["hash"] if row else None


def find_near(conn: sqlite3.Connection, embedding: bytes, threshold: float) -> tuple[str, float] | None:
    """Return (hash, distance) of nearest neighbor if cosine similarity > threshold.
    sqlite-vec returns L2 distance by default — convert to cosine via normalized vectors.
    For simplicity we treat vec0 distance as cosine_distance assuming caller passed normalized embeddings.
    """
    cur = conn.execute(
        "SELECT content_hash, distance FROM embeddings "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (embedding,),
    )
    row = cur.fetchone()
    if not row:
        return None
    similarity = 1.0 - float(row["distance"])
    if similarity >= threshold:
        return row["content_hash"], similarity
    return None


def insert_content(
    conn: sqlite3.Connection,
    *,
    body: str,
    title: str | None = None,
    source_url: str | None = None,
    source_tier: str = "agent-derived",
    fetched_at: str | None = None,
    confidence: float = 0.5,
    ttl_days: int | None = None,
    kind: str = "kb",
) -> str:
    h = content_hash(body)
    conn.execute(
        """INSERT OR IGNORE INTO content
           (hash, body, title, source_url, source_tier, fetched_at, confidence, ttl_days, kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (h, body, title, source_url, source_tier, fetched_at, confidence, ttl_days, kind),
    )
    return h


def gate(
    conn: sqlite3.Connection,
    *,
    body: str,
    title: str | None = None,
    source_url: str | None = None,
    source_tier: str = "agent-derived",
    confidence: float = 0.5,
    ttl_days: int | None = None,
    kind: str = "kb",
    actor: str = "agent",
    correlation_id: str | None = None,
    embedding: bytes | None = None,
) -> GateResult:
    """Single entry point for any content-write into the system.

    Embedding is optional at write-time; if absent, near-dup check is deferred until
    the reactor's embed handler runs, which may then emit a 'merged' event.
    """
    cfg = load_config()
    h = content_hash(body)

    if find_exact(conn, h):
        return GateResult(outcome="exact_dup", hash=h)

    # Source-URL supersede: if a live entry exists at the same source_url with
    # different bytes, treat this write as a new revision rather than a fresh ingest.
    if source_url:
        live = conn.execute(
            "SELECT hash, revision FROM content "
            "WHERE source_url = ? AND is_current = 1 AND tombstoned = 0 "
            "ORDER BY revision DESC LIMIT 1",
            (source_url,),
        ).fetchone()
        if live:
            new_revision = int(live["revision"]) + 1
            conn.execute(
                """INSERT INTO content
                   (hash, body, title, source_url, source_tier, fetched_at,
                    confidence, ttl_days, kind, revision, is_current, source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (h, body, title, source_url, source_tier, None,
                 confidence, ttl_days, kind, new_revision, None),
            )
            conn.execute(
                "UPDATE content SET is_current = 0, superseded_by = ? WHERE hash = ?",
                (h, live["hash"]),
            )
            event_log.append(
                conn, "superseded",
                {
                    "hash_old": live["hash"],
                    "hash_new": h,
                    "source_url": source_url,
                    "revision": new_revision,
                },
                actor=actor, correlation_id=correlation_id,
            )
            return GateResult(outcome="superseded", hash=h)

    if embedding is not None:
        near = find_near(conn, embedding, cfg.rag.near_dup_threshold)
        if near:
            kept_hash, _sim = near
            event_log.append(
                conn, "merged",
                {"hash_kept": kept_hash, "hash_tombstoned": h, "reason": "near_dup_at_write"},
                actor=actor, correlation_id=correlation_id,
            )
            return GateResult(outcome="near_dup", hash=h, merged_into=kept_hash)

    insert_content(
        conn, body=body, title=title, source_url=source_url, source_tier=source_tier,
        confidence=confidence, ttl_days=ttl_days, kind=kind,
    )
    event_log.append(
        conn, "ingested",
        {"hash": h, "title": title, "source_url": source_url, "kind": kind, "source_tier": source_tier},
        actor=actor, correlation_id=correlation_id,
    )
    return GateResult(outcome="new", hash=h)
