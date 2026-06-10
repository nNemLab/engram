import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "engram-plugin"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def stub_daemon():
    """A stub grounding daemon. Set routes[path]=dict to control responses;
    captured (path, body) requests land in received."""
    routes: dict = {}
    received: list = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else ""
            received.append((self.path, body))
            data = json.dumps(routes.get(self.path, {})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"url": f"http://127.0.0.1:{port}", "routes": routes, "received": received}
    srv.shutdown()


def _run_hook(script: str, stdin: str, url: str, extra_env: dict | None = None):
    env = {**os.environ, "ENGRAM_GROUNDING_URL": url, **(extra_env or {})}
    return subprocess.run(
        ["bash", str(PLUGIN / "hooks" / script)],
        input=stdin, capture_output=True, text=True, env=env,
    )


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


def test_ups_injects_block_on_strong(stub_daemon):
    stub_daemon["routes"]["/grounding"] = {"verdict": "STRONG", "block": "## Relevant memory\n- thing"}
    r = _run_hook("user_prompt_submit.sh", json.dumps({"prompt": "flashinfer oom"}), stub_daemon["url"])
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Relevant memory" in out["hookSpecificOutput"]["additionalContext"]
    path, body = stub_daemon["received"][0]
    assert path == "/grounding" and json.loads(body)["query"] == "flashinfer oom"


def test_ups_quiet_on_none(stub_daemon):
    stub_daemon["routes"]["/grounding"] = {"verdict": "NONE", "block": ""}
    r = _run_hook("user_prompt_submit.sh", json.dumps({"prompt": "x"}), stub_daemon["url"])
    assert r.returncode == 0 and json.loads(r.stdout) == {}


def test_ups_fail_open_when_daemon_down():
    r = _run_hook("user_prompt_submit.sh", json.dumps({"prompt": "x"}), "http://127.0.0.1:1")
    assert r.returncode == 0 and json.loads(r.stdout) == {}


def test_ups_injects_block_on_weak(stub_daemon):
    stub_daemon["routes"]["/grounding"] = {"verdict": "WEAK", "block": "## Relevant memory\n- maybe"}
    r = _run_hook("user_prompt_submit.sh", json.dumps({"prompt": "q"}), stub_daemon["url"])
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "Relevant memory" in out["hookSpecificOutput"]["additionalContext"]


def test_session_start_injects_prime(stub_daemon):
    stub_daemon["routes"]["/prime"] = {"block": "## Engram session priming\n- goal"}
    r = _run_hook("session_start.sh", json.dumps({"cwd": "/x", "source": "startup"}), stub_daemon["url"])
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "session priming" in out["hookSpecificOutput"]["additionalContext"]


def test_session_start_quiet_on_empty(stub_daemon):
    stub_daemon["routes"]["/prime"] = {"block": ""}
    r = _run_hook("session_start.sh", json.dumps({"cwd": "/x"}), stub_daemon["url"])
    assert r.returncode == 0 and json.loads(r.stdout) == {}


def test_session_start_fail_open():
    r = _run_hook("session_start.sh", json.dumps({"cwd": "/x"}), "http://127.0.0.1:1")
    assert r.returncode == 0 and json.loads(r.stdout) == {}


def test_stop_records_grounded_hashes(stub_daemon):
    stub_daemon["routes"]["/cite"] = {"cited": 1}
    msg = "Set MAX_JOBS=4.\n\n_grounded in: [[flashinfer note]] `[a1b2c3d4e5f6]`_"
    r = _run_hook("stop.sh", json.dumps({"assistant_message": msg}), stub_daemon["url"])
    assert r.returncode == 0 and json.loads(r.stdout) == {}
    path, body = stub_daemon["received"][0]
    assert path == "/cite"
    sent = json.loads(body)
    assert sent["hashes"] == ["a1b2c3d4e5f6"] and "turn_id" in sent


def test_stop_noop_when_no_grounding(stub_daemon):
    r = _run_hook("stop.sh", json.dumps({"assistant_message": "just chatting, no citations"}),
                  stub_daemon["url"])
    assert r.returncode == 0 and json.loads(r.stdout) == {}
    assert stub_daemon["received"] == []


def test_stop_fail_open():
    msg = "x _grounded in: `[a1b2c3d4e5f6]`_"
    r = _run_hook("stop.sh", json.dumps({"assistant_message": msg}), "http://127.0.0.1:1")
    assert r.returncode == 0 and json.loads(r.stdout) == {}
