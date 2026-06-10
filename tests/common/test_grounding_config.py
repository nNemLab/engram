def _load(tmp_path, monkeypatch, extra: str = "") -> object:
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "paths:\n"
        f"  root: {tmp_path}\n  vault: {tmp_path}/v\n"
        f"  playbooks_scratch: {tmp_path}/s\n  playbooks_curated: {tmp_path}/c\n"
        f"  playbooks_runs: {tmp_path}/r\n  db: {tmp_path}/db.sqlite\n"
        + extra
    )
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg))
    from engram.common.config import load_config
    load_config.cache_clear()
    return load_config()


def test_grounding_defaults(tmp_path, monkeypatch):
    g = _load(tmp_path, monkeypatch).grounding
    assert g.tau_high == 0.62 and g.tau_low == 0.45 and g.delta == 0.08
    assert g.token_budget == 1500 and g.port == 8770 and g.usage_weight == 0.5


def test_grounding_override(tmp_path, monkeypatch):
    g = _load(tmp_path, monkeypatch, "grounding:\n  tau_high: 0.7\n  port: 9001\n").grounding
    assert g.tau_high == 0.7 and g.port == 9001 and g.tau_low == 0.45  # others default
