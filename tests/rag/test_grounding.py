from types import SimpleNamespace

from engram.rag.grounding import classify, pack
from tests.rag import fresh_conn
from tests.rag.test_query_calibrated import _add, _stub_cfg

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


def _hit(h, title, body, sim=0.8, src=None):
    return SimpleNamespace(hash=h, title=title, body=body, dense_sim=sim, source_url=src)


def test_pack_fits_budget_and_lists_hashes():
    hits = [_hit(f"h{i}", f"Title {i}", "word " * 400) for i in range(5)]
    out = pack(hits, token_budget=120)
    assert out["hashes"], "should include at least the top hit"
    # crude token estimate = chars/4; block must fit budget
    assert len(out["block"]) / 4 <= 120 * 1.1
    assert "Title 0" in out["block"]            # top hit included
    assert out["hashes"][0] == "h0"


def test_pack_empty():
    out = pack([], token_budget=100)
    assert out == {"block": "", "hashes": []}


def test_ground_strong_returns_block(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Docker OOM", "flashinfer sm120 first start OOM guardrails MAX_JOBS")
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("h1", 0.91)])
    from engram.rag.grounding import ground
    out = ground(conn, "flashinfer oom")
    assert out["verdict"] == "STRONG"
    assert "h1"[:12] in out["block"] and out["hashes"] == ["h1"]


def test_ground_none_on_empty_corpus(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [])
    from engram.rag.grounding import ground
    out = ground(conn, "anything")
    assert out["verdict"] == "NONE" and out["block"] == "" and out["hashes"] == []
