"""Hybrid retrieval: vec0 (dense) + FTS5 (sparse), fused via Reciprocal Rank Fusion."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC

from .. import log as event_log
from ..common.config import load_config
from .embed import embed_one
from .usage import usage_factor

# Word tokens for building a safe FTS5 MATCH expression. `\w+` (Unicode by
# default for str patterns) drops every operator-significant character.
_FTS_TOKEN = re.compile(r"\w+")


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


def _fts_match_expr(query: str) -> str | None:
    """Turn a raw user query into a safe FTS5 MATCH expression.

    FTS5 treats the MATCH string as a *query expression*, so raw punctuation
    (`?`, `'`, `.`, `-`, quotes, parens, bare AND/OR/NOT, `*`, …) raises syntax
    errors — which breaks retrieval (and the grounding daemon) on the kinds of
    natural-language prompts users actually type. We extract word tokens and
    quote each as an FTS5 string literal, joined with explicit `OR`: a doc
    matches if it contains ANY query term, and FTS5's bm25 ranking then favours
    docs matching more (and rarer) terms. Space-joining instead ANDs every
    token — so a full sentence (including stopwords like "for"/"and") matched
    nothing and BM25 silently returned zero hits. Returns None when there are
    no usable tokens (empty / punctuation-only).
    """
    tokens = _FTS_TOKEN.findall(query)
    if not tokens:
        return None
    # Tokens are word characters only, so they never contain a `"` to escape.
    return " OR ".join(f'"{t}"' for t in tokens)


def _bm25_hits(conn: sqlite3.Connection, query: str, k: int) -> list[tuple[str, float]]:
    # FTS5 returns negative ranks; lower is better. We invert for use as a score.
    match = _fts_match_expr(query)
    if match is None:
        return []
    rows = conn.execute(
        "SELECT hash, rank FROM content_fts WHERE content_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (match, k),
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


# Built-in per-tier authority weights, applied when a tier is absent from
# config.confidence.source_tier_weights. Mirrors config.example.yml so a config
# that omits the block (or a tier) still ranks by source authority instead of
# flattening every tier to a single value. Config values override per-tier.
DEFAULT_TIER_WEIGHTS = {
    "peer-reviewed": 0.85,
    "vendor-doc": 0.80,
    "manual": 0.70,
    "agent-derived": 0.60,
    "blog": 0.55,
    "forum": 0.30,
}


def _tier_weight(weights: dict[str, float], tier: str | None) -> float:
    """Resolve a source tier's ranking weight: explicit config value, else the
    built-in default for that tier, else a neutral 0.5 for unknown tiers."""
    t = tier or ""
    return weights.get(t, DEFAULT_TIER_WEIGHTS.get(t, 0.5))


def hybrid_search(conn: sqlite3.Connection, query: str, *, top_k: int | None = None,
                  log_retrieval: bool = True,
                  exclude_source_tiers: list[str] | None = None,
                  exclude_kinds: list[str] | None = None,
                  since: str | None = None, until: str | None = None,
                  level: str = "snippet") -> list[Hit]:
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
    try:
        rankings.append(_bm25_hits(conn, query, k * fetch_mult))
    except Exception:
        # Defence in depth: a malformed FTS expression must never kill retrieval;
        # fall back to dense-only rather than raising.
        pass

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
        # Time-bounded queries: rows without `fetched_at` (NULL/empty) cannot be
        # temporally placed, so they are excluded whenever either bound is active.
        if (since or until) and not r["fetched_at"]:
            continue
        if since and r["fetched_at"] < since:
            continue
        if until and r["fetched_at"] >= until:
            continue
        tier_w = _tier_weight(weights, r["source_tier"])
        decay = _confidence_decay(r["fetched_at"], half_life)
        uf = usage_factor(conn, h, weight=cfg.grounding.usage_weight)
        # Relevance combines the fused rank (recall, incl. BM25-only hits) with
        # the dense cosine MAGNITUDE. RRF alone collapses relevance to a near-
        # constant band, letting the confidence/tier/usage priors swamp it — an
        # irrelevant but high-confidence note could outrank the dense-best hit.
        # Adding dense_sim restores the real spread, so priors only reorder hits
        # of comparable relevance (tie-break) rather than override clear gaps.
        relevance = rrf_score + (dense_map.get(h) or 0.0)
        ranked_score = relevance * (r["confidence"] or 0.5) * tier_w * decay * uf
        hits.append(Hit(
            hash=h, title=r["title"], body=r["body"], score=ranked_score,
            source_url=r["source_url"], confidence=r["confidence"], fetched_at=r["fetched_at"],
            dense_sim=dense_map.get(h),
        ))

    hits.sort(key=lambda x: x.score, reverse=True)
    hits = hits[:k]
    if level == "snippet":
        for h in hits:
            h.body = (h.body[:319] + "…") if len(h.body) > 320 else h.body
    # level == "section"/"full": leave body as stored (section==full for now;
    # true section extraction is a follow-up).

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
