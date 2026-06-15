"""Validation of rag.embed_dim before vec0 DDL interpolation (issue #89)."""
import pytest

from engram.common.config import RagConfig, load_config


def _load_via_yaml(tmp_path, monkeypatch, extra: str = "") -> object:
    """Write a YAML config file and load it through the real loader."""
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        "paths:\n"
        f"  root: {tmp_path}\n  vault: {tmp_path}/v\n"
        f"  playbooks_scratch: {tmp_path}/s\n  playbooks_curated: {tmp_path}/c\n"
        f"  playbooks_runs: {tmp_path}/r\n  db: {tmp_path}/db.sqlite\n"
        + extra
    )
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg_path))
    load_config.cache_clear()
    return load_config()


class TestRagConfigEmbedDimValidation:
    """RagConfig.__post_init__ rejects malformed embed_dim early."""

    def test_valid_dim_384_passes(self):
        cfg = RagConfig(embed_dim=384)
        assert cfg.embed_dim == 384

    def test_valid_dim_edge_min_passes(self):
        cfg = RagConfig(embed_dim=1)
        assert cfg.embed_dim == 1

    def test_valid_dim_edge_max_passes(self):
        cfg = RagConfig(embed_dim=8192)
        assert cfg.embed_dim == 8192

    def test_string_embed_dim_raises(self):
        with pytest.raises(ValueError, match="embed_dim"):
            RagConfig(embed_dim="384")

    def test_float_embed_dim_raises(self):
        with pytest.raises(ValueError, match="embed_dim"):
            RagConfig(embed_dim=384.0)

    def test_negative_embed_dim_raises(self):
        with pytest.raises(ValueError, match="embed_dim"):
            RagConfig(embed_dim=-1)

    def test_zero_embed_dim_raises(self):
        with pytest.raises(ValueError, match="embed_dim"):
            RagConfig(embed_dim=0)

    def test_too_large_embed_dim_raises(self):
        with pytest.raises(ValueError, match="embed_dim"):
            RagConfig(embed_dim=8193)

    def test_bool_embed_dim_raises(self):
        # bool is a subclass of int in Python — must be rejected explicitly.
        with pytest.raises(ValueError, match="embed_dim"):
            RagConfig(embed_dim=True)

    def test_none_embed_dim_raises(self):
        with pytest.raises(ValueError, match="embed_dim"):
            RagConfig(embed_dim=None)


class TestDbInitSchemaGuard:
    """init_schema also guards embed_dim (defense in depth)."""

    @pytest.fixture
    def conn(self, tmp_path):
        from engram.common.db import _connect
        return _connect(tmp_path / "test.db")

    def test_valid_embed_dim_succeeds(self, conn):
        from engram.common.db import init_schema
        init_schema(conn, embed_dim=384)

    def test_string_embed_dim_raises_raw(self, conn):
        from engram.common.db import init_schema
        with pytest.raises(ValueError, match="embed_dim"):
            init_schema(conn, embed_dim="abc")

    def test_out_of_range_embed_dim_raises(self, conn):
        from engram.common.db import init_schema
        with pytest.raises(ValueError, match="embed_dim"):
            init_schema(conn, embed_dim=16384)

    def test_zero_embed_dim_raises(self, conn):
        from engram.common.db import init_schema
        with pytest.raises(ValueError, match="embed_dim"):
            init_schema(conn, embed_dim=0)

    def test_no_vec0_table_created_on_invalid_dim(self, conn):
        """Invalid embed_dim must not silently create the vec0 table."""
        from engram.common.db import init_schema
        with pytest.raises(ValueError, match="embed_dim"):
            init_schema(conn, embed_dim="not_an_int")
        # The vec0 table should not have been created.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'embeddings'"
        ).fetchone()
        assert row is None


class TestLoadConfigEmbedDimValidation:
    """Validation fires on the real YAML config-load path (not just direct RagConfig)."""

    def test_valid_config_loads_successfully(self, tmp_path, monkeypatch):
        """A well-formed config with embed_dim=384 loads without error."""
        cfg = _load_via_yaml(
            tmp_path,
            monkeypatch,
            "rag:\n  embed_dim: 384\n",
        )
        assert cfg.rag.embed_dim == 384

    def test_string_embed_dim_in_yaml_raises(self, tmp_path, monkeypatch):
        """YAML embed_dim as a string is rejected with ValueError by __post_init__."""
        with pytest.raises(ValueError, match="embed_dim"):
            _load_via_yaml(
                tmp_path,
                monkeypatch,
                "rag:\n  embed_dim: \"384\"\n",
            )

    def test_out_of_range_embed_dim_in_yaml_raises(self, tmp_path, monkeypatch):
        """YAML embed_dim > 8192 is rejected with ValueError by __post_init__."""
        with pytest.raises(ValueError, match="embed_dim"):
            _load_via_yaml(
                tmp_path,
                monkeypatch,
                "rag:\n  embed_dim: 16384\n",
            )

    def test_zero_embed_dim_in_yaml_raises(self, tmp_path, monkeypatch):
        """YAML embed_dim == 0 is rejected with ValueError by __post_init__."""
        with pytest.raises(ValueError, match="embed_dim"):
            _load_via_yaml(
                tmp_path,
                monkeypatch,
                "rag:\n  embed_dim: 0\n",
            )
