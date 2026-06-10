from types import SimpleNamespace

from engram.rag.grounding import classify

G = SimpleNamespace(tau_high=0.62, tau_low=0.45, delta=0.08)


def _hits(*sims):
    return [SimpleNamespace(hash=f"h{i}", dense_sim=s) for i, s in enumerate(sims)]


def test_strong_when_high_and_clear_margin():
    assert classify(_hits(0.91, 0.55), G) == "STRONG"


def test_weak_when_above_low_but_thin_margin():
    assert classify(_hits(0.64, 0.63), G) == "WEAK"   # high enough but margin < delta


def test_weak_when_between_low_and_high():
    assert classify(_hits(0.50, 0.10), G) == "WEAK"


def test_none_when_below_low():
    assert classify(_hits(0.30, 0.10), G) == "NONE"


def test_none_on_empty():
    assert classify([], G) == "NONE"


def test_bm25_only_hit_without_dense_sim_is_weak_at_best():
    # top hit has no dense_sim (BM25-only) -> cannot be STRONG
    assert classify(_hits(None, None), G) in ("WEAK", "NONE")
