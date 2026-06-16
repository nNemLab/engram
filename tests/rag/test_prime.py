from tests.rag import fresh_conn
from tests.rag.test_query_calibrated import _add, _stub_cfg


def _add_src(conn, h, title, source_url, *, conf=0.8):
    """Insert a content row with an explicit source_url (the cwd-link signal)."""
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
        (h, title, f"body {h}", source_url, "manual", "2026-06-10T00:00:00Z", conf, "kb"),
    )


def test_prime_includes_goals_and_recent(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    conn.execute("INSERT INTO goals (id,text,status,priority,metadata,created_at,updated_at) "
                 "VALUES ('g1','ship docker',  'active',5,'{}','2026-06-09T00:00:00Z','2026-06-09T00:00:00Z')")
    _add(conn, "h1", "Recent note", "recent body", conf=0.9)
    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram")
    assert "ship docker" in out["block"]
    assert "Recent note" in out["block"]


def test_prime_empty_is_quiet(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    from engram.rag.prime import prime
    out = prime(conn, cwd="/tmp")
    assert out["block"] == "" or "no active" in out["block"].lower()


def test_prime_cwd_surfaces_local_over_higher_confidence_global(tmp_path, monkeypatch):
    """A project-local entry is primed ahead of unrelated, higher-confidence
    global entries when slots are scarce."""
    conn = fresh_conn(tmp_path)
    # Three high-confidence global entries, no project link.
    _add_src(conn, "g1", "Global one", "https://example.com/a", conf=0.99)
    _add_src(conn, "g2", "Global two", "https://example.com/b", conf=0.98)
    # One lower-confidence entry that lives under the working directory.
    _add_src(conn, "loc", "Project note", "/data/projects/engram/notes/x.md", conf=0.40)

    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram", max_entries=1)
    # With a single slot, the cwd-local entry wins despite its lower confidence.
    assert "Project note" in out["block"]
    assert "Global one" not in out["block"]


def test_prime_cwd_none_is_pure_global_confidence(tmp_path, monkeypatch):
    """cwd=None preserves the prior behaviour: top entries by confidence only,
    ignoring any working-directory association."""
    conn = fresh_conn(tmp_path)
    _add_src(conn, "g1", "Global one", "https://example.com/a", conf=0.99)
    _add_src(conn, "loc", "Project note", "/data/projects/engram/notes/x.md", conf=0.40)

    from engram.rag.prime import prime
    out = prime(conn, cwd=None, max_entries=1)
    # Highest-confidence entry is selected; the local path is irrelevant.
    assert "Global one" in out["block"]
    assert "Project note" not in out["block"]


def test_prime_cwd_backfills_with_global(tmp_path, monkeypatch):
    """When local entries don't fill the budget, remaining slots fall back to
    the globally highest-confidence entries."""
    conn = fresh_conn(tmp_path)
    _add_src(conn, "g1", "Global one", "https://example.com/a", conf=0.99)
    _add_src(conn, "loc", "Project note", "/data/projects/engram/x.md", conf=0.40)

    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram", max_entries=5)
    # Local first, then global backfill -- both present, no duplicates.
    assert "Project note" in out["block"]
    assert "Global one" in out["block"]
    assert out["block"].count("Project note") == 1  # no duplicate from both tiers


def test_prime_cwd_matches_file_scheme_url(tmp_path, monkeypatch):
    """source_url written with a file:// scheme (e.g. playbook runs) is matched."""
    conn = fresh_conn(tmp_path)
    _add_src(conn, "g1", "Global one", "https://example.com/a", conf=0.99)
    _add_src(conn, "run", "Run summary", "file:///data/projects/engram/runs/42", conf=0.40)

    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram", max_entries=1)
    assert "Run summary" in out["block"]
    assert "Global one" not in out["block"]


def test_prime_cwd_respects_path_boundary(tmp_path, monkeypatch):
    """A sibling dir sharing a name prefix must NOT be treated as local."""
    conn = fresh_conn(tmp_path)
    _add_src(conn, "sib", "Sibling note", "/data/projects/engram-other/x.md", conf=0.99)
    _add_src(conn, "loc", "Project note", "/data/projects/engram/x.md", conf=0.40)

    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram", max_entries=1)
    # engram-other is not under engram, so the genuine local entry wins.
    assert "Project note" in out["block"]
    assert "Sibling note" not in out["block"]


def test_prime_cwd_trailing_slash_normalized(tmp_path, monkeypatch):
    """A trailing slash on cwd matches the same entries as without it."""
    conn = fresh_conn(tmp_path)
    _add_src(conn, "g1", "Global one", "https://example.com/a", conf=0.99)
    _add_src(conn, "loc", "Project note", "/data/projects/engram/x.md", conf=0.40)

    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram/", max_entries=1)
    assert "Project note" in out["block"]
    assert "Global one" not in out["block"]


def test_prime_cwd_exact_dir_match(tmp_path, monkeypatch):
    """An entry whose source_url is exactly the cwd (the directory itself) is local."""
    conn = fresh_conn(tmp_path)
    _add_src(conn, "g1", "Global one", "https://example.com/a", conf=0.99)
    _add_src(conn, "dir", "Dir note", "/data/projects/engram", conf=0.40)

    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram", max_entries=1)
    assert "Dir note" in out["block"]
    assert "Global one" not in out["block"]


def test_prime_cwd_path_with_like_wildcards(tmp_path, monkeypatch):
    """A cwd containing LIKE metacharacters (%/_) is matched literally, not as
    a wildcard."""
    conn = fresh_conn(tmp_path)
    _add_src(conn, "real", "Real note", "/data/proj_x/a.md", conf=0.40)
    # Would be a false match if `_` were treated as a single-char wildcard.
    _add_src(conn, "decoy", "Decoy note", "/data/projXx/a.md", conf=0.99)

    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/proj_x", max_entries=1)
    assert "Real note" in out["block"]
    assert "Decoy note" not in out["block"]
