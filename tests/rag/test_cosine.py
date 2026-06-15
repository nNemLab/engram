"""Tests for l2_distance-to-cosine conversion (fix #82).

sqlite-vec returns L2 distance; these tests verify the 1 - d²/2 conversion
produces the correct cosine for normalised vectors, and that downstream
components (grounding, near-dup) work on the true cosine scale.
"""
from __future__ import annotations

import math
from unittest.mock import Mock

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

def test_vector_hits_returns_true_cosine():
    """Drive the REAL _vector_hits code (not a stub) by mocking the DB row.

    conn.execute() returns rows with an L2 'distance' column; the production
    code applies l2_to_cosine to it.  We verify the conversion happens *inside*
    the call site, not in the test.
    """
    from engram.rag import query as q

    mock_conn = Mock()
    mock_cursor = Mock()
    d_identical = 0.0
    d_orthogonal = math.sqrt(2.0)
    mock_cursor.fetchall.return_value = [
        {"content_hash": "h1", "distance": d_identical},
        {"content_hash": "h2", "distance": d_orthogonal},
    ]
    mock_conn.execute.return_value = mock_cursor

    results = q._vector_hits(mock_conn, b"dummy", 10)

    # Production code: _vector_hits calls l2_to_cosine(float(row["distance"]))
    assert results == [
        ("h1", l2_to_cosine(d_identical)),
        ("h2", l2_to_cosine(d_orthogonal)),
    ]
    assert l2_to_cosine(d_identical) == pytest.approx(1.0)
    assert l2_to_cosine(d_orthogonal) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Integration: grounding verdict on true cosine scale
# ---------------------------------------------------------------------------

def test_grounding_strong_now_reachable_at_true_cosine():
    """Before the fix, vec0 distance=0.41 → 1-d ≈ 0.59 which was below
    tau_high=0.62 even though true cos(24°)≈0.916 > 0.62.

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
    """Call the REAL dedup.find_near with a mocked DB row.

    Two normalised vectors at ~20° separation have L2 distance d≈0.347,
    true cosine≈0.940 (> 0.92 threshold) but 1-d≈0.653 (< 0.92).  The old
    code would NOT fire near-dup; the fixed code must.
    """
    from engram.dedup import find_near

    # L2 distance between unit vectors at 20°:
    cos_20 = math.cos(math.radians(20))       # ≈ 0.9397
    d_20 = math.sqrt(2.0 * (1.0 - cos_20))    # ≈ 0.347

    mock_conn = Mock()
    mock_cursor = Mock()
    mock_cursor.fetchone.return_value = {
        "content_hash": "existing_hash",
        "distance": d_20,
    }
    mock_conn.execute.return_value = mock_cursor

    result = find_near(mock_conn, b"dummy", 0.92)

    # Old code would compute 1 - d_20 ≈ 0.653 < 0.92 → no match
    old_value = 1.0 - d_20
    assert old_value < 0.92, "pre-fix 1-L2 value correctly fails threshold"

    # New code: find_near returns (hash, cosine) because it calls l2_to_cosine
    assert result is not None
    hash_kept, similarity = result
    assert hash_kept == "existing_hash"
    assert similarity == pytest.approx(cos_20, rel=1e-9)
    assert similarity >= 0.92
