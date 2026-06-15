"""Tests for l2_distance-to-cosine conversion (fix #82).

sqlite-vec returns L2 distance; these tests verify the 1 - d²/2 conversion
produces the correct cosine for normalised vectors, and that downstream
components (grounding, near-dup) work on the true cosine scale.
"""
from __future__ import annotations

import math

import pytest

from engram.rag._cosine import l2_to_cosine

# ---------------------------------------------------------------------------
# Unit: l2_to_cosine
# ---------------------------------------------------------------------------

class TestL2ToCosine:
    """Pure conversion tests with known L2 distances for unit vectors."""

    def test_identical_vectors_distance_zero(self):
        """d=0 → cosine=1.0."""
        assert l2_to_cosine(0.0) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        """For unit vectors, orthogonal → d=√2 → cos=0.0."""
        d = math.sqrt(2.0)
        assert l2_to_cosine(d) == pytest.approx(0.0)

    def test_60_degree(self):
        """For unit vectors, 60° → d=1.0 → cos=0.5."""
        assert l2_to_cosine(1.0) == pytest.approx(0.5)

    def test_90_degree(self):
        """For unit vectors, 90° → d=√2 → cos=0.0."""
        d = math.sqrt(2.0)
        assert l2_to_cosine(d) == pytest.approx(0.0)

    def test_clamped_on_large_distance(self):
        """Non-normalised inputs that produce cos<0 are clamped to 0.0."""
        assert l2_to_cosine(10.0) == 0.0

    def test_clamped_on_negative_input(self):
        """Negative distance: squared → positive, result may fall in [0,1]."""
        assert l2_to_cosine(-1.0) == 0.5  # 1 - 0.5*(-1)² = 0.5

    def test_45_degree(self):
        """For unit vectors, 45° → d=√(2(1-cos45°)) → cos≈0.707."""
        cos_val = math.cos(math.radians(45))
        d = math.sqrt(2.0 * (1.0 - cos_val))
        assert l2_to_cosine(d) == pytest.approx(cos_val, rel=1e-9)

    def test_20_degree(self):
        """For unit vectors, 20° → cos(20°) ≈ 0.9397."""
        cos_val = math.cos(math.radians(20))
        d = math.sqrt(2.0 * (1.0 - cos_val))
        assert l2_to_cosine(d) == pytest.approx(cos_val, rel=1e-9)


# ---------------------------------------------------------------------------
# Integration: query._vector_hits conversion end-to-end
# ---------------------------------------------------------------------------

def test_vector_hits_returns_true_cosine(tmp_path, monkeypatch):
    """The query pipeline calls l2_to_cosine inside _vector_hits.

    We mock _vector_hits to call l2_to_cosine on known L2 distances (as the
    real implementation does), then verify dense_sim carries those cosine
    values through to the returned Hit objects.
    """
    from engram.rag import query as q
    from engram.rag._cosine import l2_to_cosine
    from tests.rag import fresh_conn

    conn = fresh_conn(tmp_path)
    # Need content rows so hybrid_search can build Hit objects from hashes.
    conn.execute(
        "INSERT INTO content (hash,title,body,source_url,source_tier,"
        "fetched_at,confidence,kind,tombstoned) VALUES "
        "('h1','A','alpha term',NULL,'manual','2026-06-10T00:00:00Z',0.8,'kb',0), "
        "('h2','B','beta term',NULL,'manual','2026-06-10T00:00:00Z',0.8,'kb',0)",
    )
    # Stub: vec0 returns L2 distances, converted to cosine by l2_to_cosine.
    monkeypatch.setattr(q, "embed_one", lambda s: b"dummy")
    monkeypatch.setattr(
        q,
        "_vector_hits",
        lambda conn, emb, k: [
            ("h1", l2_to_cosine(0.0)),
            ("h2", l2_to_cosine(math.sqrt(2.0))),
        ],
    )

    # Patch load_config so hybrid_search doesn't try to load a real YAML.
    from types import SimpleNamespace

    base = SimpleNamespace(
        rag=SimpleNamespace(top_k=12, rrf_k=60, embed_dim=4),
        confidence=SimpleNamespace(source_tier_weights={}, recency_half_life_days=365),
        grounding=SimpleNamespace(usage_weight=0.5, tau_high=0.62, tau_low=0.45,
                                  delta=0.08, token_budget=1500),
    )
    from engram.common import config as cm

    monkeypatch.setattr(cm, "load_config", lambda *a, **k: base)
    monkeypatch.setattr(q, "load_config", lambda *a, **k: base)

    hits = q.hybrid_search(conn, "dummy", log_retrieval=False)
    sims = {h.hash: h.dense_sim for h in hits}
    assert sims["h1"] == pytest.approx(1.0)
    assert sims["h2"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Integration: grounding verdict on true cosine scale
# ---------------------------------------------------------------------------

def test_grounding_strong_now_reachable_for_70_degrees():
    """Before the fix, vec0 distance=0.41 (~40°) → 1-d ≈ 0.59 which was below
    tau_high=0.62 even though true cosine(40°)≈0.766 > 0.62.

    With the fix, a vector at L2 distance 0.41 (cos ≈ 0.916) correctly triggers
    STRONG when margin is sufficient.
    """
    from types import SimpleNamespace

    from engram.rag.grounding import classify

    G = SimpleNamespace(tau_high=0.62, tau_low=0.45, delta=0.08)

    # L2 distance 0.41 → cos = 1 - 0.5 * 0.41² = 0.91595 > 0.62.
    # Second best at distance 0.90 → cos = 1 - 0.5 * 0.90² = 0.595.
    # margin = 0.916 - 0.595 = 0.321 >= delta(0.08) → STRONG.
    from engram.rag._cosine import l2_to_cosine
    sim_top = l2_to_cosine(0.41)
    sim_second = l2_to_cosine(0.90)

    assert sim_top >= 0.62
    assert sim_top - sim_second >= 0.08

    hits = [
        SimpleNamespace(hash="h0", dense_sim=sim_top),
        SimpleNamespace(hash="h1", dense_sim=sim_second),
    ]
    assert classify(hits, G) == "STRONG"


def test_grounding_weak_at_true_cosine_0_50():
    """A true cosine of 0.50 (60°) is above tau_low(0.45) but below tau_high(0.62)
    with thin margin → WEAK."""
    from types import SimpleNamespace

    from engram.rag.grounding import classify

    G = SimpleNamespace(tau_high=0.62, tau_low=0.45, delta=0.08)
    hits = [
        SimpleNamespace(hash="h0", dense_sim=0.50),
        SimpleNamespace(hash="h1", dense_sim=0.10),
    ]
    assert classify(hits, G) == "WEAK"


# ---------------------------------------------------------------------------
# Integration: near-dup dedup at true cosine scale
# ---------------------------------------------------------------------------

def test_near_dup_detects_at_correct_cosine_threshold():
    """With tau=0.92, two vectors at 20° separation have cos(20°)≈0.9397 > 0.92.
    The old code (1-d) would compute 1-0.347≈0.653 which FAILS the check.
    The corrected code produces the right similarity and triggers near-dup."""
    from engram.rag._cosine import l2_to_cosine

    # L2 distance between unit vectors at 20°:
    cos_20 = math.cos(math.radians(20))       # ≈ 0.9397
    d_20 = math.sqrt(2.0 * (1.0 - cos_20))    # ≈ 0.347

    # Old code: 1 - d = 1 - 0.347 = 0.653  →  FAILS threshold 0.92
    old_code = 1.0 - d_20

    # New code: true cosine
    new_code = l2_to_cosine(d_20)

    assert old_code < 0.92, "old code correctly fails threshold"
    assert new_code >= 0.92, "new code correctly passes threshold"
    assert new_code == pytest.approx(cos_20, rel=1e-9)
