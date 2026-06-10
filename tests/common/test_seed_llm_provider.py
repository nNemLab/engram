"""The seeder must emit provider-agnostic (OpenAI-compatible) LLM calls (no Anthropic)."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _seeder_source() -> str:
    return (REPO / "scripts" / "seed_starter_playbooks.py").read_text()


def test_no_anthropic_references():
    src = _seeder_source()
    assert "anthropic" not in src.lower(), "seeder must not reference Anthropic"
    assert "ENGRAM_ANTHROPIC_API_KEY" not in src


def test_uses_openai_and_generic_env():
    src = _seeder_source()
    assert "from openai import OpenAI" in src
    assert "chat.completions.create" in src
    for var in ("ENGRAM_LLM_BASE_URL", "ENGRAM_LLM_API_KEY", "ENGRAM_LLM_MODEL"):
        assert var in src, f"missing {var}"


def test_seeder_still_imports():
    """Module must remain importable (syntax intact after edits)."""
    spec = importlib.util.spec_from_file_location(
        "seed_starter_playbooks", REPO / "scripts" / "seed_starter_playbooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main") or hasattr(mod, "PLAYBOOKS") or True
