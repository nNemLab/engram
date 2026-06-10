from types import SimpleNamespace

import pytest

from tests.rag import fresh_conn


def _stub_cfg(monkeypatch, **over):
    base = SimpleNamespace(
        rag=SimpleNamespace(top_k=12, rrf_k=60, embed_dim=4),
        confidence=SimpleNamespace(source_tier_weights={}, recency_half_life_days=365),
        grounding=SimpleNamespace(usage_weight=0.5, tau_high=0.62, tau_low=0.45, delta=0.08,
                                  token_budget=1500),
    )
    for k, v in over.items():
        setattr(base, k, v)
    from engram.common import config as m
    monkeypatch.setattr(m, "load_config", lambda *a, **k: base)
    import engram.rag.query as q
    monkeypatch.setattr(q, "load_config", lambda *a, **k: base)
    return base


def _add(conn, h, title, body, *, tier="manual", conf=0.8):
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        (h, title, body, None, tier, "2026-06-10T00:00:00Z", conf, "kb"),
    )
    # Note: content_ai trigger already inserts into content_fts; no manual insert needed.


def test_hit_carries_dense_sim(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Docker OOM", "flashinfer sm120 first start OOM guardrails")
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("h1", 0.91)])
    hits = q.hybrid_search(conn, "flashinfer oom", log_retrieval=False)
    assert hits and hits[0].hash == "h1"
    assert hits[0].dense_sim == pytest.approx(0.91)


def test_citation_boosts_ranking(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "A", "alpha shared term", conf=0.8)
    _add(conn, "h2", "B", "alpha shared term", conf=0.8)
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    # equal dense sim -> tie broken by usage
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("h1", 0.80), ("h2", 0.80)])
    from engram.rag.usage import record_cited
    record_cited(conn, ["h2"], query="alpha")  # h2 has been useful
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    order = [h.hash for h in hits]
    assert order.index("h2") < order.index("h1"), "cited h2 should rank above h1"
