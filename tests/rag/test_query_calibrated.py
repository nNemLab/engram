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


def test_since_filters_old_entries(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                 "confidence,kind,tombstoned) VALUES "
                 "('old','Old','alpha term',NULL,'manual','2026-01-01T00:00:00Z',0.8,'kb',0)")
    _add(conn, "new", "New", "alpha term")  # fetched_at 2026-06-10
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.80), ("new", 0.80)])
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False, since="2026-03-01T00:00:00Z")
    assert [h.hash for h in hits] == ["new"]


def test_until_filters_newer_entries(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                 "confidence,kind,tombstoned) VALUES "
                 "('old','Old','alpha term',NULL,'manual','2026-01-01T00:00:00Z',0.8,'kb',0)")
    _add(conn, "new", "New", "alpha term")  # fetched_at 2026-06-10
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.80), ("new", 0.80)])
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False, until="2026-03-01T00:00:00Z")
    assert [h.hash for h in hits] == ["old"]


def test_since_and_until_bound_a_window(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    for h, ts in (("old", "2026-01-01T00:00:00Z"), ("mid", "2026-04-01T00:00:00Z"),
                  ("new", "2026-09-01T00:00:00Z")):
        conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                     "confidence,kind,tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
                     (h, h, "alpha term", None, "manual", ts, 0.8, "kb"))
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits",
                        lambda conn, emb, k: [("old", 0.8), ("mid", 0.8), ("new", 0.8)])
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False,
                           since="2026-03-01T00:00:00Z", until="2026-08-01T00:00:00Z")
    assert [h.hash for h in hits] == ["mid"]


def test_level_controls_body_length(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Big", "word " * 1000)
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("h1", 0.80)])
    snip = q.hybrid_search(conn, "word", log_retrieval=False, level="snippet")[0]
    full = q.hybrid_search(conn, "word", log_retrieval=False, level="full")[0]
    assert len(snip.body) < len(full.body)
    assert len(snip.body) <= 320


def test_tier_weight_falls_back_to_defaults_then_half():
    """A tier absent from config must fall back to the built-in default weight,
    not flatten to 0.5; an explicit config value overrides; unknown -> 0.5."""
    import engram.rag.query as q
    # Empty config -> built-in defaults (peer-reviewed > agent-derived).
    assert q._tier_weight({}, "peer-reviewed") == q.DEFAULT_TIER_WEIGHTS["peer-reviewed"]
    assert q._tier_weight({}, "agent-derived") == q.DEFAULT_TIER_WEIGHTS["agent-derived"]
    assert q._tier_weight({}, "peer-reviewed") > q._tier_weight({}, "agent-derived")
    # Config value overrides the default for that tier.
    assert q._tier_weight({"peer-reviewed": 0.2}, "peer-reviewed") == 0.2
    # Unknown tier / NULL -> neutral 0.5.
    assert q._tier_weight({}, "made-up-tier") == 0.5
    assert q._tier_weight({}, None) == 0.5


def test_peer_reviewed_outranks_agent_derived_by_default(tmp_path, monkeypatch):
    """With source_tier_weights unset, an authoritative tier must still outrank a
    lower one at equal relevance/confidence (regression: tier was a 0.5 no-op)."""
    _stub_cfg(monkeypatch)  # source_tier_weights = {}
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "A", "alpha shared term", tier="agent-derived", conf=0.8)
    _add(conn, "h2", "B", "alpha shared term", tier="peer-reviewed", conf=0.8)
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    # Equal dense sim; h1 even gets the better dense rank — tier must still flip it.
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("h1", 0.80), ("h2", 0.80)])
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    order = [h.hash for h in hits]
    assert order.index("h2") < order.index("h1"), "peer-reviewed should outrank agent-derived"
