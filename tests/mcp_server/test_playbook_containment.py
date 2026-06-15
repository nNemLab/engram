"""playbook.run must contain agent-supplied names to the template dirs (issue #32)."""
import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfg_root(tmp_path, monkeypatch):
    root = tmp_path / "engram"
    for sub in ("playbooks/scratch", "playbooks/curated", "playbooks/runs", "vault"):
        (root / sub).mkdir(parents=True)
    cfg = tmp_path / "config.yml"
    cfg.write_text(json.dumps({
        "paths": {
            "root": str(root),
            "vault": str(root / "vault"),
            "playbooks_scratch": str(root / "playbooks/scratch"),
            "playbooks_curated": str(root / "playbooks/curated"),
            "playbooks_runs": str(root / "playbooks/runs"),
            "db": str(root / "db.sqlite"),
        },
    }))
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg))
    from engram.common.config import load_config
    load_config.cache_clear()
    yield root
    load_config.cache_clear()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript((REPO / "schema" / "001_initial.sql").read_text())
    return c


@pytest.fixture
def run(cfg_root, conn):
    from engram.mcp_server.tools.playbook import register
    return register(conn)["playbook.run"]["handler"]


def test_rejects_parent_traversal_even_when_target_exists(cfg_root, run):
    evil = cfg_root / "evil.ipynb"
    evil.write_text("{}")
    out = run({"name": "../../evil.ipynb", "runtime": "jupyter"})
    assert "escapes" in out.get("error", "")


def test_rejects_absolute_path(cfg_root, run, tmp_path):
    evil = tmp_path / "outside.ipynb"
    evil.write_text("{}")
    out = run({"name": str(evil), "runtime": "jupyter"})
    assert "escapes" in out.get("error", "")


def test_rejects_traversal_for_marimo_runtime(cfg_root, run):
    evil = cfg_root / "evil.py"
    evil.write_text("")
    out = run({"name": "../../evil.py", "runtime": "marimo"})
    assert "escapes" in out.get("error", "")


def test_rejects_traversal_before_creating_run_dir(cfg_root, run):
    run({"name": "../../evil.ipynb", "runtime": "jupyter"})
    assert list((cfg_root / "playbooks/runs").iterdir()) == []


def test_normal_missing_name_reports_not_found(cfg_root, run):
    out = run({"name": "does-not-exist", "runtime": "jupyter"})
    assert "not found" in out.get("error", "")


def test_contained_template_still_runs(cfg_root, run, monkeypatch):
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    mock_proc = mock.Mock()
    mock_proc.pid = 99995
    mock_proc.communicate.return_value = (b"done", b"")
    mock_proc.returncode = 0
    session_flag = []

    def fake_popen(cmd, **kwargs):
        session_flag.append(kwargs.get("start_new_session", False))
        return mock_proc

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.Popen", fake_popen)
    out = run({"name": "demo", "runtime": "jupyter"})
    assert out["exit_code"] == 0
    assert session_flag[0] is True
