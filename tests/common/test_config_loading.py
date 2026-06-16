"""Config loading hardening (#162): empty sections, unknown keys, relative-path
resolution, and the ENGRAM_SKIP_VERSION_CHECK guard."""
from pathlib import Path

import pytest

from engram.common import config as cfgmod
from engram.common.config import load_config

# A minimal, valid paths block every fixture reuses. All absolute (~-expanded).
_PATHS = """\
paths:
  root: /tmp/engram-test-root
  vault: /tmp/engram-test-root/vault
  playbooks_scratch: /tmp/engram-test-root/pb/scratch
  playbooks_curated: /tmp/engram-test-root/pb/curated
  playbooks_runs: /tmp/engram-test-root/pb/runs
  db: /tmp/engram-test-root/db.sqlite
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    load_config.cache_clear()
    return p


def test_empty_section_falls_back_to_defaults(tmp_path):
    """A present-but-empty section (YAML None) must use dataclass defaults, not
    crash on RagConfig(**None)."""
    cfg = load_config(_write(tmp_path, _PATHS + "rag:\nresearch:\n"))
    assert cfg.rag.embed_dim == 384  # default
    assert cfg.research.web_default_k == 8  # default


def test_missing_sections_use_defaults(tmp_path):
    """Only `paths` is required; omitted sections take their defaults."""
    cfg = load_config(_write(tmp_path, _PATHS))
    assert cfg.rag.embed_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert cfg.grounding.tau_high == 0.62


def test_unknown_key_raises_naming_file_section_key(tmp_path):
    p = _write(tmp_path, _PATHS + "rag:\n  embed_dimm: 768\n")
    with pytest.raises(ValueError) as ei:
        load_config(p)
    msg = str(ei.value)
    assert "embed_dimm" in msg
    assert "rag" in msg
    assert str(p) in msg


def test_unknown_key_in_paths_raises(tmp_path):
    p = _write(tmp_path, _PATHS + "")  # base
    body = p.read_text().replace("db: /tmp/engram-test-root/db.sqlite",
                                 "db: /tmp/engram-test-root/db.sqlite\n  typo_key: x")
    p.write_text(body)
    load_config.cache_clear()
    with pytest.raises(ValueError, match="typo_key"):
        load_config(p)


def test_relative_db_resolves_under_root_not_cwd(tmp_path, monkeypatch):
    """A relative `db:` resolves against paths.root regardless of CWD."""
    body = """\
paths:
  root: /tmp/engram-rel-root
  vault: /tmp/engram-rel-root/vault
  playbooks_scratch: /tmp/engram-rel-root/pb/scratch
  playbooks_curated: /tmp/engram-rel-root/pb/curated
  playbooks_runs: /tmp/engram-rel-root/pb/runs
  db: db.sqlite
"""
    p = _write(tmp_path, body)
    # CWD must not influence the result.
    monkeypatch.chdir(tmp_path)
    cfg = load_config(p)
    assert cfg.db_path == Path("/tmp/engram-rel-root/db.sqlite")


def test_absolute_db_left_alone(tmp_path):
    cfg = load_config(_write(tmp_path, _PATHS))
    assert cfg.db_path == Path("/tmp/engram-test-root/db.sqlite")


def test_empty_file_reports_missing_paths(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("")  # yaml.safe_load -> None
    load_config.cache_clear()
    with pytest.raises(ValueError, match="paths"):
        load_config(p)


def test_non_mapping_section_rejected(tmp_path):
    p = _write(tmp_path, _PATHS + "rag: not-a-mapping\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(p)


def teardown_function(_fn):
    # Don't leak a cached config into sibling tests.
    cfgmod.load_config.cache_clear()
