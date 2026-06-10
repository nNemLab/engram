"""Deterministic retrieval grounding: a sufficiency verdict + token-budget packing.

No model calls — operates on the raw dense cosine similarities already computed by
hybrid_search. Verdict drives whether/what the ambient hook injects (Phase 3).
"""
from __future__ import annotations

from typing import Any

Verdict = str  # "STRONG" | "WEAK" | "NONE"


def classify(hits: list[Any], cfg: Any) -> Verdict:
    """Verdict from the top hit's dense similarity and its margin to #2.

    cfg needs: tau_high, tau_low, delta. Hits need a `dense_sim: float | None`.
    A BM25-only top hit (dense_sim is None) can never be STRONG.
    """
    if not hits:
        return "NONE"
    top = hits[0].dense_sim
    if top is None:
        # No dense evidence for the best hit; treat as WEAK if any dense signal
        # exists below, else NONE.
        return "WEAK" if any(h.dense_sim is not None for h in hits) else "NONE"
    if top < cfg.tau_low:
        return "NONE"
    second = next((h.dense_sim for h in hits[1:] if h.dense_sim is not None), 0.0)
    margin = top - second
    if top >= cfg.tau_high and margin >= cfg.delta:
        return "STRONG"
    return "WEAK"
