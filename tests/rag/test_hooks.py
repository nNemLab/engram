import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "engram-plugin"


def test_plugin_manifest_valid():
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "engram-memory"
    assert "version" in m and "hooks" in m and "skills" in m


def test_hooks_declared():
    h = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    events = h["hooks"]
    assert {"UserPromptSubmit", "SessionStart", "Stop"} <= set(events)
    flat = json.dumps(h)
    assert "${CLAUDE_PLUGIN_ROOT}" in flat
    assert "user_prompt_submit.sh" in flat and "session_start.sh" in flat and "stop.sh" in flat
