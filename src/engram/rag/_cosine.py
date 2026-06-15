"""L2-distance-to-cosine conversion for sqlite-vec (vec0) results.

sqlite-vec's KNN returns L2 (Euclidean) distance by default.  When vectors are
L2-normalised the exact identity holds::

    d_L2(x, y) = sqrt(2 * (1 - cos(x, y)))   ⇒   cos(x, y) = 1 - d_L2² / 2

This module provides the forward conversion so every call site that interprets
vec0 distance as a similarity score uses the true cosine scale instead of the
broken ``1 - d`` shortcut (which can go negative past ~60° and does not match
the cosine range [0, 1] for orthogonal-to-identical vectors).

.. note:: The identity is exact for unit-norm vectors.  Callers must ensure
   embeddings are L2-normalised before insertion (see :func:`~engram.rag.embed`
   for the default normalisation).
"""
from __future__ import annotations


def l2_to_cosine(distance: float) -> float:
    """Convert an L2 (Euclidean) distance to cosine similarity.

    Valid for L2-normalised vectors:  cos = 1 - d²/2.
    Clamps to [0, 1] for non-normalised inputs that may produce negatives.
    """
    return max(0.0, min(1.0, 1.0 - 0.5 * distance * distance))
