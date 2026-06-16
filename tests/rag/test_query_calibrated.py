from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.rag import fresh_conn


def _stub_cfg(monkeypatch, **over):
    base = SimpleNamespace(
        rag=SimpleNamespace(top_k=12, rrf_k=60, embed_dim=4),
        confidence=SimpleNamespace(
            source_tier_weights={},
            recency_half_life_days=365,
            recency_score_enabled=True,
            recency_score_weight=0.2,
            recency_score_half_life_days=30,
        ),
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


def test_vector_hits_query_filters_tombstoned_rows():
    import engram.rag.query as q

    conn = Mock()
    cur = Mock()
    cur.fetchall.return_value = [{"content_hash": "live", "distance": 0.0}]
    conn.execute.return_value = cur

    hits = q._vector_hits(conn, b"qemb", 2)

    assert [h for h, _ in hits] == ["live"]
    sql = conn.execute.call_args.args[0]
    assert "tombstoned = 0" in sql


def test_vector_hits_query_filters_non_current_rows():
    import engram.rag.query as q

    conn = Mock()
    cur = Mock()
    cur.fetchall.return_value = [{"content_hash": "live", "distance": 0.0}]
    conn.execute.return_value = cur

    q._vector_hits(conn, b"qemb", 2)

    sql = conn.execute.call_args.args[0]
    assert "is_current = 1" in sql


def test_hybrid_search_excludes_non_current_rows(tmp_path, monkeypatch):
    """A superseded (is_current=0, not tombstoned) row must not appear in
    hybrid_search results even when the fuser surfaces it (#139)."""
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    # `old` superseded but not tombstoned; `new` is the current revision.
    conn.execute(
        "INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
        "confidence,kind,tombstoned,is_current) VALUES "
        "('old','Old','alpha term',NULL,'manual','2026-06-10T00:00:00Z',0.8,'kb',0,0)"
    )
    _add(conn, "new", "New", "alpha term")  # is_current defaults to 1
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.80), ("new", 0.80)])
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    assert [h.hash for h in hits] == ["new"]


def test_since_uses_datetime_not_lexicographic(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    # Lexicographically, "...00Z" > "...00.100Z" though chronologically it is older.
    conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                 "confidence,kind,tombstoned) VALUES "
                 "('old','Old','alpha term',NULL,'manual','2026-03-01T00:00:00Z',0.8,'kb',0)")
    conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                 "confidence,kind,tombstoned) VALUES "
                 "('new','New','alpha term',NULL,'manual','2026-03-01T00:00:00.200Z',0.8,'kb',0)")
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.80), ("new", 0.80)])
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False, since="2026-03-01T00:00:00.100Z")
    assert [h.hash for h in hits] == ["new"]


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


def test_since_drops_rows_with_invalid_timestamps_when_bounded(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                 "confidence,kind,tombstoned) VALUES "
                 "('bad','Bad','alpha term',NULL,'manual','not-iso',0.8,'kb',0)")
    _add(conn, "good", "Good", "alpha term")
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("bad", 0.80), ("good", 0.80)])
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False, since="2026-01-01T00:00:00Z")
    assert [h.hash for h in hits] == ["good"]


def test_level_controls_body_length(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Big", "word " * 1000)
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("h1", 0.80)])
    snip = q.hybrid_search(conn, "word", log_retrieval=False, level="snippet")[0]
    section = q.hybrid_search(conn, "word", log_retrieval=False, level="section")[0]
    full = q.hybrid_search(conn, "word", log_retrieval=False, level="full")[0]
    assert len(snip.body) < len(full.body)
    assert len(snip.body) <= 320
    assert len(section.body) <= 1200
    assert len(section.body) <= len(full.body)


def test_hybrid_search_refills_after_filtering(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    for i in range(6):
        tier = "forum" if i < 4 else "manual"
        _add(conn, f"h{i}", f"T{i}", "alpha", tier=tier)
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [(f"h{i}", 0.8) for i in range(6)])

    hits = q.hybrid_search(
        conn,
        "alpha",
        top_k=2,
        log_retrieval=False,
        exclude_source_tiers=["forum"],
    )
    assert len(hits) == 2
    assert {h.hash for h in hits} == {"h4", "h5"}


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


def test_relevant_outranks_higher_confidence_irrelevant(tmp_path, monkeypatch):
    """A clearly more dense-relevant hit must rank above a clearly less-relevant
    one even when the latter has higher confidence. RRF flattens relevance to a
    near-constant; multiplying a wide confidence prior onto it let an irrelevant
    high-confidence note win. Relevance magnitude must dominate clear gaps."""
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "rel", "Relevant", "zzz", conf=0.5)   # body shares no query term
    _add(conn, "irr", "Irrelevant", "zzz", conf=0.9)  # higher confidence prior
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    # rel is clearly more relevant (0.60 vs 0.30); query term absent from bodies
    # so BM25 contributes nothing and dense magnitude is the only relevance signal.
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("rel", 0.60), ("irr", 0.30)])
    hits = q.hybrid_search(conn, "qqqq", log_retrieval=False)
    order = [h.hash for h in hits]
    assert order.index("rel") < order.index("irr"), \
        "dense-more-relevant must outrank higher-confidence-but-less-relevant"


def test_confidence_decay_disabled_when_recency_score_enabled(tmp_path, monkeypatch):
    conn = fresh_conn(tmp_path)
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        ("old", "Old", "alpha", None, "manual", "2026-01-01T00:00:00Z", 0.8, "kb"),
    )
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        ("new", "New", "alpha", None, "manual", "2026-06-01T00:00:00Z", 0.8, "kb"),
    )
    import engram.rag.query as q

    monkeypatch.setattr(q, "datetime", SimpleNamespace(
        now=lambda tz=None: datetime(2026, 6, 10, tzinfo=UTC),
        fromisoformat=datetime.fromisoformat,
    ))
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.90), ("new", 0.89)])

    _stub_cfg(
        monkeypatch,
        confidence=SimpleNamespace(
            source_tier_weights={},
            recency_half_life_days=1,
            recency_score_enabled=True,
            recency_score_weight=0.0,
            recency_score_half_life_days=30,
        ),
    )
    hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    assert [h.hash for h in hits][:2] == ["old", "new"]


def test_recency_score_prefers_fresher_docs(tmp_path, monkeypatch):
    conn = fresh_conn(tmp_path)
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        ("old", "Old", "alpha shared term", None, "manual", "2026-01-01T00:00:00Z", 0.8, "kb"),
    )
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        ("new", "New", "alpha shared term", None, "manual", "2026-06-01T00:00:00Z", 0.8, "kb"),
    )
    import engram.rag.query as q

    monkeypatch.setattr(q, "datetime", SimpleNamespace(
        now=lambda tz=None: datetime(2026, 6, 10, tzinfo=UTC),
        fromisoformat=datetime.fromisoformat,
    ))
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    # Older doc has stronger dense relevance; recency ON should flip this order.
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.95), ("new", 0.80)])

    _stub_cfg(
        monkeypatch,
        confidence=SimpleNamespace(
            source_tier_weights={},
            recency_half_life_days=100000,
            recency_score_enabled=False,
            recency_score_weight=1.0,
            recency_score_half_life_days=30,
        ),
    )
    off_hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    assert [h.hash for h in off_hits][:2] == ["old", "new"]

    _stub_cfg(
        monkeypatch,
        confidence=SimpleNamespace(
            source_tier_weights={},
            recency_half_life_days=100000,
            recency_score_enabled=True,
            recency_score_weight=1.0,
            recency_score_half_life_days=30,
        ),
    )
    on_hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    assert [h.hash for h in on_hits][:2] == ["new", "old"]


def test_recency_score_toggle_and_weight_controls_impact(tmp_path, monkeypatch):
    # No recency weighting: preserve raw relevance order.
    _stub_cfg(
        monkeypatch,
        confidence=SimpleNamespace(
            source_tier_weights={},
            recency_half_life_days=100000,
            recency_score_enabled=False,
            recency_score_weight=1.0,
            recency_score_half_life_days=30,
        ),
    )
    conn = fresh_conn(tmp_path)
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        ("old", "Old", "alpha shared term", None, "manual", "2026-01-01T00:00:00Z", 0.8, "kb"),
    )
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        ("new", "New", "alpha shared term", None, "manual", "2026-06-01T00:00:00Z", 0.8, "kb"),
    )
    import engram.rag.query as q

    monkeypatch.setattr(q, "datetime", SimpleNamespace(
        now=lambda tz=None: datetime(2026, 6, 10, tzinfo=UTC),
        fromisoformat=datetime.fromisoformat,
    ))
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.95), ("new", 0.80)])

    disabled_hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    assert [h.hash for h in disabled_hits][:2] == ["old", "new"]

    # Weight 0 should be equivalent to disabled even if enabled is true.
    _stub_cfg(
        monkeypatch,
        confidence=SimpleNamespace(
            source_tier_weights={},
            recency_half_life_days=100000,
            recency_score_enabled=True,
            recency_score_weight=0.0,
            recency_score_half_life_days=30,
        ),
    )
    zero_weight_hits = q.hybrid_search(conn, "alpha", log_retrieval=False)
    assert [h.hash for h in zero_weight_hits][:2] == ["old", "new"]


def test_bm25_matches_on_any_term_not_conjunction(tmp_path, monkeypatch):
    """A multi-word natural-language query must still match docs containing SOME
    of its terms. Quoting tokens space-separated AND-s them in FTS5, so no doc
    matched a full sentence and BM25 silently returned nothing."""
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Quant", "quantization fp4 nvfp4 checkpoints")
    import engram.rag.query as q
    hits = q._bm25_hits(conn, "How does vLLM quantization support FP4 checkpoints?", 10)
    assert any(h == "h1" for h, _ in hits), \
        "BM25 must match on OR of query terms, not require all of them"
