"""Hybrid retrieval: vec0 (dense) + FTS5 (sparse), fused via Reciprocal Rank Fusion."""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .. import log as event_log
from ..common.config import load_config
from ._cosine import l2_to_cosine
from .embed import embed_one
from .usage import usage_factor

logger = logging.getLogger("engram.rag.query")

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
        "WHERE embedding MATCH ? "
        "AND content_hash IN (SELECT hash FROM content WHERE tombstoned = 0 AND is_current = 1) "
        "ORDER BY distance LIMIT ?",
        (query_emb, k),
    ).fetchall()
    return [(r["content_hash"], l2_to_cosine(float(r["distance"]))) for r in rows]


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


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _confidence_decay(fetched_at: str | None, half_life_days: int) -> float:
    ts = _parse_timestamp(fetched_at)
    if ts is None:
        return 1.0
    age_days = max(0.0, (datetime.now(UTC) - ts).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(1, half_life_days))


def _recency_score(
    *,
    fetched_at: str | None,
    enabled: bool,
    weight: float,
    half_life_days: int,
    now: datetime | None = None,
) -> float:
    if not enabled or weight <= 0.0:
        return 1.0
    ts = _parse_timestamp(fetched_at)
    if ts is None:
        return 1.0
    ref = now or datetime.now(UTC)
    age_days = max(0.0, (ref - ts).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / max(1, half_life_days))
    # Blend with neutral 1.0 so the recency term can be tuned from no-op (0)
    # to full exponential decay (1) without disrupting existing ranking.
    return (1.0 - weight) + (weight * decay)


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


def _section_text(body: str, query: str, *, max_chars: int = 1200) -> str:
    """Return a paragraph-sized section biased toward query terms."""
    if not body:
        return ""
    tokens = {t.lower() for t in _FTS_TOKEN.findall(query)}
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paras:
        paras = [body.strip()]
    chosen = paras[0]
    if tokens:
        for p in paras:
            low = p.lower()
            if any(t in low for t in tokens):
                chosen = p
                break
    if len(chosen) <= max_chars:
        return chosen
    return chosen[: max_chars - 1] + "…"


def hybrid_search(conn: sqlite3.Connection, query: str, *, top_k: int | None = None,
                  log_retrieval: bool = True,
                  exclude_source_tiers: list[str] | None = None,
                  exclude_kinds: list[str] | None = None,
                  since: str | None = None, until: str | None = None,
                  level: str = "snippet") -> list[Hit]:
    cfg = load_config()
    k = top_k or cfg.rag.top_k
    if level not in {"snippet", "section", "full"}:
        raise ValueError(f"unsupported level: {level!r}")
    # Over-fetch when filtering so we can still return ~k hits after the cut.
    fetch_mult = 4 if (exclude_source_tiers or exclude_kinds) else 2

    since_dt = _parse_timestamp(since)
    until_dt = _parse_timestamp(until)

    q_emb: bytes | None = None
    dense_available = False
    try:
        q_emb = embed_one(query)
        dense_available = True
    except Exception as exc:
        logger.warning("dense retrieval unavailable; falling back without embeddings",
                       extra={"mode": "dense", "cause": str(exc)}, exc_info=True)

    rankings: list[list[tuple[str, float]]] = []
    dense_map: dict[str, float] = {}
    limit = max(k * fetch_mult, k)
    max_limit = max(limit, k) * 16

    while True:
        iter_rankings: list[list[tuple[str, float]]] = []
        if dense_available and q_emb is not None:
            try:
                dv = _vector_hits(conn, q_emb, limit)
                dense_map.update(dict(dv))
                iter_rankings.append(dv)
            except Exception as exc:
                logger.warning("dense retrieval failed during hybrid search",
                               extra={"mode": "dense", "cause": str(exc)}, exc_info=True)
                dense_available = False
        try:
            iter_rankings.append(_bm25_hits(conn, query, limit))
        except Exception as exc:
            logger.warning("FTS retrieval failed during hybrid search",
                           extra={"mode": "fts", "cause": str(exc)}, exc_info=True)

        rankings = [r for r in iter_rankings if r]
        fused = _rrf_fuse(rankings, k=limit, rrf_k=cfg.rag.rrf_k)
        if not fused:
            return []

        hashes = [h for h, _ in fused]
        placeholders = ",".join("?" * len(hashes))
        rows = conn.execute(
            f"SELECT hash, title, body, source_url, source_tier, fetched_at, confidence, kind "
            f"FROM content WHERE hash IN ({placeholders}) AND tombstoned = 0 AND is_current = 1",
            hashes,
        ).fetchall()
        by_hash = {r["hash"]: r for r in rows}
        excl_tiers = set(exclude_source_tiers or [])
        excl_kinds = set(exclude_kinds or [])

        weights = cfg.confidence.source_tier_weights
        half_life = cfg.confidence.recency_half_life_days
        recency_enabled = cfg.confidence.recency_score_enabled
        recency_weight = cfg.confidence.recency_score_weight
        recency_half_life = cfg.confidence.recency_score_half_life_days
        # Avoid double age-decay on fetched_at: when recency scoring is enabled,
        # confidence decay is disabled regardless of recency weight.
        use_conf_decay = not recency_enabled

        hits: list[Hit] = []
        for h, rrf_score in fused:
            r = by_hash.get(h)
            if not r:
                continue
            if r["source_tier"] in excl_tiers:
                continue
            if r["kind"] in excl_kinds:
                continue
            fetched_at = _parse_timestamp(r["fetched_at"])
            # Time-bounded queries: rows without parseable timestamps cannot be
            # temporally placed, so they are excluded whenever either bound is active.
            if (since_dt or until_dt) and fetched_at is None:
                continue
            if since_dt and fetched_at is not None and fetched_at < since_dt:
                continue
            if until_dt and fetched_at is not None and fetched_at >= until_dt:
                continue
            tier_w = _tier_weight(weights, r["source_tier"])
            decay = _confidence_decay(r["fetched_at"], half_life) if use_conf_decay else 1.0
            uf = usage_factor(conn, h, weight=cfg.grounding.usage_weight)
            recency = _recency_score(
                fetched_at=r["fetched_at"],
                enabled=recency_enabled,
                weight=recency_weight,
                half_life_days=recency_half_life,
            )
            relevance = rrf_score + (dense_map.get(h) or 0.0)
            ranked_score = relevance * (r["confidence"] or 0.5) * tier_w * decay * uf * recency
            hits.append(Hit(
                hash=h, title=r["title"], body=r["body"], score=ranked_score,
                source_url=r["source_url"], confidence=r["confidence"], fetched_at=r["fetched_at"],
                dense_sim=dense_map.get(h),
            ))

        hits.sort(key=lambda x: x.score, reverse=True)
        if len(hits) >= k or limit >= max_limit or len(fused) < limit:
            hits = hits[:k]
            break
        limit = min(limit * 2, max_limit)

    if level == "snippet":
        for h in hits:
            h.body = (h.body[:319] + "…") if len(h.body) > 320 else h.body
    elif level == "section":
        for h in hits:
            h.body = _section_text(h.body, query)

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
