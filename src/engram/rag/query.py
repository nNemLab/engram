"""Hybrid retrieval: vec0 (dense) + FTS5 (sparse), fused via Reciprocal Rank Fusion."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC

from .. import log as event_log
from ..common.config import load_config
from .embed import embed_one
from .usage import usage_factor


@dataclass
class Hit:
    hash: str
    title: str | None
    body: str
    score: float
    source_url: str | None
    confidence: float
    fetched_at: str | None
    dense_sim: float | None = None


def _vector_hits(conn: sqlite3.Connection, query_emb: bytes, k: int) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT content_hash, distance FROM embeddings "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (query_emb, k),
    ).fetchall()
    return [(r["content_hash"], 1.0 - float(r["distance"])) for r in rows]


def _bm25_hits(conn: sqlite3.Connection, query: str, k: int) -> list[tuple[str, float]]:
    # FTS5 returns negative ranks; lower is better. We invert for use as a score.
    rows = conn.execute(
        "SELECT hash, rank FROM content_fts WHERE content_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (query, k),
    ).fetchall()
    return [(r["hash"], -float(r["rank"])) for r in rows]


def _rrf_fuse(rankings: list[list[tuple[str, float]]], k: int, rrf_k: int) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (h, _s) in enumerate(ranking):
            scores[h] = scores.get(h, 0.0) + 1.0 / (rrf_k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]


def _confidence_decay(fetched_at: str | None, half_life_days: int) -> float:
    if not fetched_at:
        return 1.0
    from datetime import datetime
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    age_days = max(0.0, (datetime.now(UTC) - ts).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(1, half_life_days))


def hybrid_search(conn: sqlite3.Connection, query: str, *, top_k: int | None = None,
                  log_retrieval: bool = True,
                  exclude_source_tiers: list[str] | None = None,
                  exclude_kinds: list[str] | None = None) -> list[Hit]:
    cfg = load_config()
    k = top_k or cfg.rag.top_k
    # Over-fetch when filtering so we can still return ~k hits after the cut.
    fetch_mult = 4 if (exclude_source_tiers or exclude_kinds) else 2

    rankings: list[list[tuple[str, float]]] = []
    dense_map: dict[str, float] = {}
    try:
        q_emb = embed_one(query)
        dv = _vector_hits(conn, q_emb, k * fetch_mult)
        dense_map = dict(dv)
        rankings.append(dv)
    except Exception:
        # Embedding failure should not kill retrieval; fall back to BM25 only.
        pass
    rankings.append(_bm25_hits(conn, query, k * fetch_mult))

    fused = _rrf_fuse(rankings, k=k * fetch_mult, rrf_k=cfg.rag.rrf_k)
    if not fused:
        return []

    hashes = [h for h, _ in fused]
    placeholders = ",".join("?" * len(hashes))
    rows = conn.execute(
        f"SELECT hash, title, body, source_url, source_tier, fetched_at, confidence, kind "
        f"FROM content WHERE hash IN ({placeholders}) AND tombstoned = 0",
        hashes,
    ).fetchall()
    by_hash = {r["hash"]: r for r in rows}
    excl_tiers = set(exclude_source_tiers or [])
    excl_kinds = set(exclude_kinds or [])

    weights = cfg.confidence.source_tier_weights
    half_life = cfg.confidence.recency_half_life_days
    hits: list[Hit] = []
    for h, rrf_score in fused:
        r = by_hash.get(h)
        if not r:
            continue
        if r["source_tier"] in excl_tiers:
            continue
        if r["kind"] in excl_kinds:
            continue
        tier_w = weights.get(r["source_tier"] or "", 0.5)
        decay = _confidence_decay(r["fetched_at"], half_life)
        uf = usage_factor(conn, h, weight=cfg.grounding.usage_weight)
        ranked_score = rrf_score * (r["confidence"] or 0.5) * tier_w * decay * uf
        hits.append(Hit(
            hash=h, title=r["title"], body=r["body"], score=ranked_score,
            source_url=r["source_url"], confidence=r["confidence"], fetched_at=r["fetched_at"],
            dense_sim=dense_map.get(h),
        ))

    hits.sort(key=lambda x: x.score, reverse=True)
    hits = hits[:k]

    if log_retrieval and hits:
        # One event per query, with the list of returned hashes.
        # Reactor's on_retrieved iterates the list for demand-driven staleness.
        # The retrieval_log table stays as a fast-access access counter.
        event_log.append(
            conn, "retrieved",
            {"query": query, "hashes": [h.hash for h in hits], "count": len(hits)},
            actor="agent",
        )
        conn.executemany(
            "INSERT INTO retrieval_log (content_hash, query) VALUES (?, ?)",
            [(h.hash, query) for h in hits],
        )

    return hits
