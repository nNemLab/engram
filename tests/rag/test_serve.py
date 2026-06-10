import httpx

from tests.rag import fresh_conn
from tests.rag.test_query_calibrated import _add, _stub_cfg


def _stub_retrieval(monkeypatch, hits):
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: hits)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d")


async def test_healthz_ok(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    from engram.rag.serve import build_serve_app
    app = build_serve_app(fresh_conn(tmp_path))
    async with await _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_grounding_endpoint_returns_verdict_and_block(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Docker OOM", "flashinfer sm120 first start OOM guardrails MAX_JOBS")
    _stub_retrieval(monkeypatch, [("h1", 0.91)])
    from engram.rag.serve import build_serve_app
    app = build_serve_app(conn)
    async with await _client(app) as c:
        r = await c.post("/grounding", json={"query": "flashinfer", "token_budget": 500})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "STRONG"
    assert "h1"[:12] in body["block"] and body["hashes"] == ["h1"]


async def test_grounding_none_on_empty(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _stub_retrieval(monkeypatch, [])
    from engram.rag.serve import build_serve_app
    app = build_serve_app(conn)
    async with await _client(app) as c:
        r = await c.post("/grounding", json={"query": "nothing"})
    assert r.json()["verdict"] == "NONE" and r.json()["block"] == ""


async def test_prime_endpoint_returns_block(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    conn.execute("INSERT INTO goals (id,text,status,priority,metadata,created_at,updated_at) "
                 "VALUES ('g1','ship phase 2','active',5,'{}','2026-06-10T00:00:00Z','2026-06-10T00:00:00Z')")
    from engram.rag.serve import build_serve_app
    app = build_serve_app(conn)
    async with await _client(app) as c:
        r = await c.post("/prime", json={"cwd": "/x"})
    assert r.status_code == 200 and "ship phase 2" in r.json()["block"]


async def test_grounding_survives_special_chars(tmp_path, monkeypatch):
    """A question-shaped prompt (trailing `?`) must return 200, not 500 (#63)."""
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Dev instance", "engram dev instance runs on the CPU lane")
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [])
    from engram.rag.serve import build_serve_app
    app = build_serve_app(conn)
    async with await _client(app) as c:
        r = await c.post("/grounding", json={"query": "where does the engram dev instance run?"})
    assert r.status_code == 200


async def test_grounding_missing_query_is_400(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    from engram.rag.serve import build_serve_app
    app = build_serve_app(fresh_conn(tmp_path))
    async with await _client(app) as c:
        r = await c.post("/grounding", json={"token_budget": 100})
    assert r.status_code == 400


async def test_prime_bad_token_budget_is_400(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    from engram.rag.serve import build_serve_app
    app = build_serve_app(fresh_conn(tmp_path))
    async with await _client(app) as c:
        r = await c.post("/prime", json={"token_budget": "fast"})
    assert r.status_code == 400


def test_serve_uses_configured_port(monkeypatch):
    """serve() resolves the port from grounding.port when not given, and runs uvicorn."""
    import engram.rag.serve as serve_mod
    captured = {}
    monkeypatch.setattr(serve_mod, "build_serve_app", lambda: "APP")

    def fake_run(app, host, port):
        captured.update(app=app, host=host, port=port)

    from types import SimpleNamespace

    import uvicorn

    from engram.common import config as cfgmod
    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(cfgmod, "load_config",
                        lambda *a, **k: SimpleNamespace(grounding=SimpleNamespace(port=8770)))
    serve_mod.serve()
    assert captured == {"app": "APP", "host": "127.0.0.1", "port": 8770}


def test_cli_has_serve_command():
    from engram.rag.__main__ import cli
    assert "serve" in cli.commands


def test_systemd_unit_exists_and_runs_serve():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    unit = (repo / "systemd" / "engram-rag.service").read_text()
    assert "engram-rag serve" in unit
    assert "ExecStart=" in unit and "%h/.engram/.venv/bin/engram-rag" in unit


def test_docker_entrypoint_starts_grounding_daemon():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    ep = (repo / "docker" / "entrypoint.sh").read_text()
    assert "engram-rag serve" in ep
    assert "8770" in ep  # binds the grounding port


def test_compose_publishes_grounding_port():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    compose = (repo / "docker" / "compose.yml").read_text()
    assert "127.0.0.1:8770:8770" in compose


async def test_prime_malformed_body_is_400(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    from engram.rag.serve import build_serve_app
    app = build_serve_app(fresh_conn(tmp_path))
    async with await _client(app) as c:
        r1 = await c.post("/prime", content=b"not json")      # malformed JSON
        r2 = await c.post("/prime", json=["not", "an", "object"])  # JSON array, not object
    assert r1.status_code == 400 and r2.status_code == 400


async def test_cite_endpoint_resolves_prefix_to_full_hash(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    full1 = "a1b2c3d4e5f6" + "0" * 52   # 64-char content hash
    full2 = "d4e5f6a1b2c3" + "1" * 52
    for h in (full1, full2):
        conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                     "confidence,kind,tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
                     (h, "t", "b", None, "manual", "2026-06-10T00:00:00Z", 0.8, "kb"))
    from engram.rag.serve import build_serve_app
    app = build_serve_app(conn)
    async with await _client(app) as c:
        r = await c.post("/cite", json={"hashes": ["a1b2c3d4e5f6", "d4e5f6a1b2c3"], "turn_id": "t1"})
    assert r.status_code == 200 and r.json()["cited"] == 2
    recorded = {row["content_hash"] for row in conn.execute("SELECT content_hash FROM content_usage")}
    assert recorded == {full1, full2}   # FULL hashes recorded, not the 12-char prefixes


async def test_cite_endpoint_bad_body_is_400(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    from engram.rag.serve import build_serve_app
    app = build_serve_app(fresh_conn(tmp_path))
    async with await _client(app) as c:
        r = await c.post("/cite", json={"hashes": "notalist"})
    assert r.status_code == 400
