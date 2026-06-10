"""The MCP server must answer an `initialize` over streamable HTTP at /mcp."""
import httpx

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def test_initialize_returns_engram_server_info(tmp_path, monkeypatch):
    # Isolate the DB the server opens (build_http_app builds the registry).
    # Import locally (not at module scope): a collection-time import of http_app
    # pulls in engram.dedup/tools, which makes other suites' load_config
    # monkeypatch on the config module ineffective for their already-bound names.
    _isolate_engram(monkeypatch, tmp_path)
    from engram.mcp_server.http_app import build_http_app
    app = build_http_app(json_response=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mcp", follow_redirects=True
        ) as client:
            # Mount("/mcp", ...) serves the manager at the trailing-slash path and
            # 307-redirects "/mcp" -> "/mcp/"; follow_redirects keeps this end-to-end.
            r = await client.post(
                "/mcp/", json=INIT,
                headers={"Accept": "application/json, text/event-stream"},
            )
    assert r.status_code == 200
    assert "engram" in r.text  # serverInfo.name


def _isolate_engram(monkeypatch, tmp_path):
    """Point engram at a throwaway config/DB so build_http_app() can construct.

    Other suites monkeypatch ``load_config`` to a bare ``SimpleNamespace`` fake;
    because the name is re-exported into ``engram.common.db`` (and friends) via
    ``from .config import load_config``, those rebinds can outlive their test and
    leave ``db.load_config`` pointing at a fake. So rather than only clearing the
    lru_cache, install a real loader of *our* config on every module that calls it
    in the ``build_http_app`` path — fully isolating this test from leaked bindings.
    """
    cfg = tmp_path / "config.yml"
    (tmp_path / "vault").mkdir()
    cfg.write_text(
        "paths:\n"
        f"  root: {tmp_path}\n"
        f"  vault: {tmp_path}/vault\n"
        f"  playbooks_scratch: {tmp_path}/ps\n"
        f"  playbooks_curated: {tmp_path}/pc\n"
        f"  playbooks_runs: {tmp_path}/pr\n"
        f"  db: {tmp_path}/db.sqlite\n"
    )
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg))
    from engram.common import config as cfg_mod
    cfg_mod.load_config.cache_clear()
    real = cfg_mod.load_config

    from engram.common import db as db_mod
    monkeypatch.setattr(cfg_mod, "load_config", real)
    monkeypatch.setattr(db_mod, "load_config", real)


def test_cli_parses_http_flags():
    from engram.mcp_server.__main__ import parse_args
    args = parse_args(["--http", "--host", "0.0.0.0", "--port", "9000"])
    assert args.http is True
    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_cli_defaults_to_stdio():
    from engram.mcp_server.__main__ import parse_args
    args = parse_args([])
    assert args.http is False
