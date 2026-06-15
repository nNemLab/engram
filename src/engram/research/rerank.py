"""Cross-encoder reranker. Scores (query, passage) pairs for semantic relevance.

The cross-encoder is more accurate than embedding cosine because it sees both
sequences together — but slower (can't precompute corpus embeddings). For a
result set of 10–30 candidates this is cheap (<200ms on CPU, <30ms on GPU).
"""
from __future__ import annotations

import threading
from collections.abc import Sequence

from ..common.config import load_config

_lock = threading.Lock()
_model = None


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import CrossEncoder
                cfg = load_config()
                name = cfg.research.reranker_model
                _model = CrossEncoder(name)
    return _model


def score(query: str, passages: Sequence[str]) -> list[float]:
    """Return a list of relevance scores aligned with `passages`. Higher = better.
    Empty input returns empty output."""
    if not passages:
        return []
    model = _get_model()
    pairs = [(query, p) for p in passages]
    raw = model.predict(pairs, show_progress_bar=False)
    return [float(x) for x in raw]
