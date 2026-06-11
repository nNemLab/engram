"""Deterministic retrieval grounding: a sufficiency verdict + token-budget packing.

No model calls — operates on the raw dense cosine similarities already computed by
hybrid_search. Verdict drives whether/what the ambient hook injects (Phase 3).
"""
from __future__ import annotations

from typing import Any

Verdict = str  # "STRONG" | "WEAK" | "NONE"


def classify(hits: list[Any], cfg: Any) -> Verdict:
    """Verdict from the strongest dense match and its margin to the next.

    cfg needs: tau_high, tau_low, delta. Hits need a `dense_sim: float | None`.
    Read from the dense-BEST hit, not hits[0]: presentation order is set by the
    confidence/tier/usage re-ranking, which can place a dense-weak hit first —
    that must not drag the verdict to NONE when a strong dense match is present.
    With no dense evidence at all (only BM25-only hits) the verdict is NONE.
    """
    if not hits:
        return "NONE"
    dense = sorted((h.dense_sim for h in hits if h.dense_sim is not None), reverse=True)
    if not dense:
        return "NONE"
    top = dense[0]
    if top < cfg.tau_low:
        return "NONE"
    second = dense[1] if len(dense) > 1 else 0.0
    margin = top - second
    if top >= cfg.tau_high and margin >= cfg.delta:
        return "STRONG"
    return "WEAK"


def _est_tokens(s: str) -> int:
    return (len(s) + 3) // 4


def pack(hits: list[Any], token_budget: int) -> dict[str, Any]:
    """Pack the highest-value hits (title + snippet) into a markdown block that
    fits token_budget. Returns {block, hashes}. Deterministic; always includes the
    top hit if any budget at all."""
    if not hits:
        return {"block": "", "hashes": []}
    lines = ["## Relevant memory"]
    used = _est_tokens(lines[0])
    chosen: list[str] = []
    for h in hits:
        title = (h.title or "(untitled)")
        snippet = " ".join((h.body or "").split())[:280]
        src = f" — {h.source_url}" if getattr(h, "source_url", None) else ""
        entry = f"- **{title}**{src} `[{h.hash[:12]}]`\n  {snippet}"
        cost = _est_tokens(entry)
        if chosen and used + cost > token_budget:
            break
        lines.append(entry)
        used += cost
        chosen.append(h.hash)
    return {"block": "\n".join(lines), "hashes": chosen}


def ground(conn, query: str, *, token_budget: int | None = None) -> dict[str, Any]:
    """One call for the hook/daemon: retrieve -> classify -> pack. Never logs a
    `retrieved` event on NONE (keeps the log clean on irrelevant turns)."""
    from ..common.config import load_config
    from .query import hybrid_search
    cfg = load_config()
    budget = token_budget or cfg.grounding.token_budget
    hits = hybrid_search(conn, query, log_retrieval=False)
    verdict = classify(hits, cfg.grounding)
    if verdict == "NONE":
        return {"verdict": "NONE", "block": "", "hashes": [], "hits": []}
    packed = pack(hits, budget)
    return {"verdict": verdict, "block": packed["block"], "hashes": packed["hashes"],
            "hits": [{"hash": h.hash, "title": h.title, "score": round(h.score, 4)} for h in hits]}
