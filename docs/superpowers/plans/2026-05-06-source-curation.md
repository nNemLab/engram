# Source Curation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build polled, MCP-managed source subscriptions for Engram so a single declaration like "the Docker docs Linux pages" stays curated indefinitely — pages update when upstream changes, old revisions are preserved but not rendered, and the operator never edits a script.

**Architecture:** New standalone `engram-poller` daemon scans a `sources` table on a 60s tick; for each due source it dispatches a typed adapter (`sitemap` or `github-repo`) that yields candidate `(source_url, body, ...)` tuples. Tuples flow through the existing dedup gate, which gains a fourth outcome `superseded` when a live entry already exists at the same `source_url`. New `revision` / `is_current` / `superseded_by` columns on the `content` table maintain a per-URL version chain. Vault projector handles the new `superseded` event by overwriting the same canonical path; vault filenames for sourced content are URL-derived, not hash-derived, so revisions stably overwrite. New `sources.*` MCP namespace exposes six tools.

**Tech Stack:** Python 3.11, asyncio, sqlite3, httpx, trafilatura (already deps), pyyaml, pytest + pytest-asyncio (already in dev deps), GitHub REST v3.

**Spec:** [`docs/superpowers/specs/2026-05-06-source-curation-design.md`](../specs/2026-05-06-source-curation-design.md)

---

## Setup (one-time, before Task 1)

- [ ] **Install dev dependencies in the runtime venv**

```bash
uv pip install --python /home/nemy/.engram/.venv/bin/python -e '.[dev]'
```

Expected: pytest, pytest-asyncio, ruff installed without errors. Verify:

```bash
/home/nemy/.engram/.venv/bin/python -c "import pytest, pytest_asyncio; print(pytest.__version__, pytest_asyncio.__version__)"
```

- [ ] **Create test directory skeleton**

```bash
mkdir -p tests/sources tests/integration tests/fixtures/docs_v1 tests/fixtures/docs_v2
touch tests/__init__.py tests/sources/__init__.py tests/integration/__init__.py
```

- [ ] **Add a pytest config** so tests find the source tree

Create `pyproject.toml` modification — add this section:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Verify pytest discovers nothing yet** (sanity check)

```bash
cd /data/projects/engram && /home/nemy/.engram/.venv/bin/pytest -q
```

Expected: `no tests ran in 0.0Xs` (clean baseline).

---

## Task 1: Schema migration — `sources` table + content revision columns

**Files:**
- Create: `schema/002_sources_and_revisions.sql`
- Test: `tests/sources/test_schema_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/sources/test_schema_migration.py`:

```python
"""Schema migration 002: sources table + content revision columns."""
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_001 = REPO_ROOT / "schema" / "001_initial.sql"
SCHEMA_002 = REPO_ROOT / "schema" / "002_sources_and_revisions.sql"


def _apply(conn, sql_path):
    conn.executescript(sql_path.read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c, SCHEMA_001)
    _apply(c, SCHEMA_002)
    yield c
    c.close()


def test_sources_table_exists(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    expected = {
        "id", "name", "adapter", "url", "config", "schedule",
        "source_tier", "paused", "next_poll_at", "last_polled_at",
        "last_success_at", "cursor", "error_count", "last_error",
        "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_sources_due_index_exists(conn):
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sources'"
    )}
    assert "idx_sources_due" in idx


def test_content_has_revision_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(content)")}
    assert {"revision", "is_current", "superseded_by", "source_id"}.issubset(cols)


def test_content_revision_default_is_one(conn):
    conn.execute(
        "INSERT INTO content (hash, body, kind, source_tier, confidence) "
        "VALUES ('h1', 'b', 'kb', 'manual', 0.9)"
    )
    row = conn.execute("SELECT revision, is_current FROM content WHERE hash='h1'").fetchone()
    assert row["revision"] == 1
    assert row["is_current"] == 1


def test_content_url_current_index_exists(conn):
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='content'"
    )}
    assert "idx_content_url_current" in idx
    assert "idx_content_source" in idx
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_schema_migration.py -v
```

Expected: tests fail because `schema/002_sources_and_revisions.sql` does not exist.

- [ ] **Step 3: Write the migration**

Create `schema/002_sources_and_revisions.sql`:

```sql
-- 002: sources registry + content revision chain.
-- Idempotent: run after 001_initial.sql on existing dbs to upgrade.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    adapter         TEXT NOT NULL,
    url             TEXT NOT NULL,
    config          TEXT NOT NULL DEFAULT '{}',
    schedule        TEXT NOT NULL,
    source_tier     TEXT NOT NULL DEFAULT 'vendor-doc',
    paused          INTEGER NOT NULL DEFAULT 0,
    next_poll_at    TEXT,
    last_polled_at  TEXT,
    last_success_at TEXT,
    cursor          TEXT,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sources_due
    ON sources(next_poll_at) WHERE paused = 0;

ALTER TABLE content ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE content ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1;
ALTER TABLE content ADD COLUMN superseded_by TEXT REFERENCES content(hash);
ALTER TABLE content ADD COLUMN source_id TEXT REFERENCES sources(id);

CREATE INDEX IF NOT EXISTS idx_content_url_current
    ON content(source_url, is_current);
CREATE INDEX IF NOT EXISTS idx_content_source
    ON content(source_id, is_current);
```

- [ ] **Step 4: Run the tests, verify they pass**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_schema_migration.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Apply the migration to the live runtime DB**

```bash
/home/nemy/.engram/.venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('/home/nemy/.engram/db.sqlite')
conn.executescript(open('schema/002_sources_and_revisions.sql').read())
conn.commit()
print('migration applied')
"
```

Expected: `migration applied`. Verify with:

```bash
/home/nemy/.engram/.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('/home/nemy/.engram/db.sqlite')
print('sources:', [r[1] for r in c.execute('PRAGMA table_info(sources)')])
print('content_new_cols:', [r[1] for r in c.execute('PRAGMA table_info(content)') if r[1] in ('revision','is_current','superseded_by','source_id')])
"
```

Expected: sources columns listed; content has the four new columns.

- [ ] **Step 6: Commit**

```bash
git add schema/002_sources_and_revisions.sql tests/__init__.py tests/sources/__init__.py tests/integration/__init__.py tests/sources/test_schema_migration.py pyproject.toml
git commit -m "schema 002: sources table + content revision chain"
```

---

## Task 2: Schedule parser — `parse_interval`

**Files:**
- Create: `src/engram/poller/__init__.py`
- Create: `src/engram/poller/schedule.py`
- Test: `tests/sources/test_schedule.py`

- [ ] **Step 1: Write the failing test**

Create `tests/sources/test_schedule.py`:

```python
from datetime import timedelta

import pytest

from engram.poller.schedule import parse_interval


def test_minutes():
    assert parse_interval("30m") == timedelta(minutes=30)


def test_hours():
    assert parse_interval("6h") == timedelta(hours=6)


def test_days():
    assert parse_interval("1d") == timedelta(days=1)
    assert parse_interval("7d") == timedelta(days=7)


def test_weeks():
    assert parse_interval("2w") == timedelta(weeks=2)


def test_seconds():
    assert parse_interval("90s") == timedelta(seconds=90)


@pytest.mark.parametrize("bad", ["", "1", "x", "1y", "  ", "1d2h", "-1d"])
def test_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse_interval(bad)
```

- [ ] **Step 2: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_schedule.py -v
```

Expected: ImportError, no `engram.poller`.

- [ ] **Step 3: Implement**

Create `src/engram/poller/__init__.py` (empty):

```python
"""Source poller daemon — scans the sources table, dispatches adapters."""
```

Create `src/engram/poller/schedule.py`:

```python
"""Duration-string parser for source schedules.

Grammar: <int><unit> where unit ∈ {s,m,h,d,w}. Examples: 30m, 6h, 1d, 7d, 2w.
"""
from __future__ import annotations

import re
from datetime import timedelta

_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}
_PATTERN = re.compile(r"^(\d+)([smhdw])$")


def parse_interval(s: str) -> timedelta:
    if not isinstance(s, str):
        raise ValueError(f"schedule must be str, got {type(s).__name__}")
    m = _PATTERN.match(s.strip())
    if not m:
        raise ValueError(f"invalid schedule {s!r}; expected like '7d', '6h', '30m'")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError(f"schedule duration must be positive: {s!r}")
    return timedelta(**{_UNITS[unit]: n})
```

- [ ] **Step 4: Run, verify pass**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_schedule.py -v
```

Expected: 11 passed (5 unit tests + 6 parametrize cases — actually 5+6 = 11).

- [ ] **Step 5: Commit**

```bash
git add src/engram/poller/__init__.py src/engram/poller/schedule.py tests/sources/test_schedule.py
git commit -m "poller: parse_interval for source schedule strings"
```

---

## Task 3: Glob filter — `matches_globs`

**Files:**
- Create: `src/engram/poller/adapters/__init__.py`
- Test: `tests/sources/test_glob_filter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/sources/test_glob_filter.py`:

```python
from engram.poller.adapters import matches_globs


def test_no_filters_passes_everything():
    assert matches_globs("/anything", include=[], exclude=[]) is True


def test_include_matches():
    assert matches_globs("/engine/install/", include=["*/engine/*"], exclude=[]) is True


def test_include_does_not_match():
    assert matches_globs("/desktop/", include=["*/engine/*"], exclude=[]) is False


def test_exclude_overrides_include():
    assert matches_globs(
        "/desktop/install/macos/",
        include=["*/install/*"],
        exclude=["*/macos/*"],
    ) is False


def test_double_star_matches_path_segments():
    assert matches_globs(
        "docs/engine/deep/nested/install.md",
        include=["docs/engine/**"],
        exclude=[],
    ) is True


def test_url_filter_handles_full_url():
    assert matches_globs(
        "https://docs.docker.com/engine/install/linux/",
        include=["*/engine/*"],
        exclude=[],
    ) is True
```

- [ ] **Step 2: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_glob_filter.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/engram/poller/adapters/__init__.py`:

```python
"""Adapter protocol, Candidate dataclass, ADAPTERS registry, glob filter."""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


# ----- Candidate ---------------------------------------------------------

@dataclass
class Candidate:
    """One ingestable item produced by an adapter for the dedup gate."""
    source_url: str
    body: str
    title: str | None = None
    fetched_at: str | None = None
    metadata: dict = field(default_factory=dict)


# ----- Adapter protocol --------------------------------------------------

class Adapter(Protocol):
    """An adapter polls one source and yields Candidates.

    The adapter reads source['cursor'] (a JSON string) for its incremental
    state, and is expected to return updated cursor data via the source row
    (the poller writes it back atomically at end of run).
    """
    name: str

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        ...


# ----- Glob filter helper ------------------------------------------------

def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a shell glob (with ** for any-depth) to a regex.

    fnmatch.translate handles single * and ? but treats ** identically to *.
    We pre-substitute a sentinel for ** so multi-segment matches work.
    """
    SENTINEL = "\x00DOUBLESTAR\x00"
    pat = pattern.replace("**", SENTINEL)
    regex = fnmatch.translate(pat)
    regex = regex.replace(re.escape(SENTINEL), ".*")
    return re.compile(regex)


def matches_globs(path: str, include: list[str], exclude: list[str]) -> bool:
    """True if path matches any include and no exclude. Empty include = wildcard."""
    if exclude and any(_glob_to_regex(p).match(path) for p in exclude):
        return False
    if not include:
        return True
    return any(_glob_to_regex(p).match(path) for p in include)


# ----- Registry (populated as adapters import) ---------------------------

ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    ADAPTERS[adapter.name] = adapter
```

- [ ] **Step 4: Run, verify pass**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_glob_filter.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/engram/poller/adapters/__init__.py tests/sources/test_glob_filter.py
git commit -m "poller: matches_globs filter + Adapter protocol"
```

---

## Task 4: Dedup gate — superseded outcome

**Files:**
- Modify: `src/engram/dedup.py`
- Test: `tests/sources/test_dedup_supersede.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/sources/test_dedup_supersede.py`:

```python
"""dedup.gate gains a fourth outcome `superseded` when source_url already has a
live entry at a different content hash. Old row's is_current flips to 0 and
superseded_by points to the new hash. A `superseded` event is emitted.
"""
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply_schema(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply_schema(c)
    # Stub config so dedup.gate doesn't try to load ~/.engram/config.yml
    from engram.common import config as cfg_mod
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    yield c


def test_first_ingest_with_source_url_is_new(conn):
    from engram.dedup import gate
    r = gate(conn, body="hello v1", source_url="https://example.com/page",
             kind="research", source_tier="vendor-doc")
    assert r.outcome == "new"
    row = conn.execute(
        "SELECT revision, is_current, superseded_by FROM content WHERE hash=?", (r.hash,)
    ).fetchone()
    assert row["revision"] == 1
    assert row["is_current"] == 1
    assert row["superseded_by"] is None


def test_second_ingest_same_url_different_body_supersedes(conn):
    from engram.dedup import gate
    r1 = gate(conn, body="hello v1", source_url="https://example.com/page",
              kind="research", source_tier="vendor-doc")
    r2 = gate(conn, body="hello v2 changed", source_url="https://example.com/page",
              kind="research", source_tier="vendor-doc")
    assert r2.outcome == "superseded"
    old = conn.execute(
        "SELECT revision, is_current, superseded_by FROM content WHERE hash=?", (r1.hash,)
    ).fetchone()
    new = conn.execute(
        "SELECT revision, is_current, superseded_by FROM content WHERE hash=?", (r2.hash,)
    ).fetchone()
    assert old["is_current"] == 0
    assert old["superseded_by"] == r2.hash
    assert new["revision"] == 2
    assert new["is_current"] == 1
    assert new["superseded_by"] is None


def test_supersede_emits_event(conn):
    from engram.dedup import gate
    gate(conn, body="v1", source_url="https://example.com/p", kind="research",
         source_tier="vendor-doc")
    r2 = gate(conn, body="v2", source_url="https://example.com/p", kind="research",
              source_tier="vendor-doc")
    rows = conn.execute(
        "SELECT type, payload FROM events WHERE type='superseded' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["hash_new"] == r2.hash
    assert payload["source_url"] == "https://example.com/p"
    assert payload["revision"] == 2


def test_exact_dup_at_same_url_is_exact_dup_not_supersede(conn):
    from engram.dedup import gate
    r1 = gate(conn, body="same", source_url="https://example.com/p", kind="research",
              source_tier="vendor-doc")
    r2 = gate(conn, body="same", source_url="https://example.com/p", kind="research",
              source_tier="vendor-doc")
    assert r2.outcome == "exact_dup"
    assert r2.hash == r1.hash


def test_supersede_only_triggers_with_source_url(conn):
    """kb.write paths without source_url must be unaffected."""
    from engram.dedup import gate
    gate(conn, body="alpha body for note", kind="kb")
    r = gate(conn, body="alpha body for note revised", kind="kb")
    assert r.outcome == "new"  # different content, no source_url, no supersede
```

- [ ] **Step 2: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_dedup_supersede.py -v
```

Expected: 4–5 failures (no `superseded` outcome yet; gate inserts as new).

- [ ] **Step 3: Modify `src/engram/dedup.py`**

Replace the `Outcome` Literal and add the supersede branch in `gate()`. Apply this edit:

Find:
```python
Outcome = Literal["new", "exact_dup", "near_dup", "contradicts"]
```
Replace with:
```python
Outcome = Literal["new", "exact_dup", "near_dup", "contradicts", "superseded"]
```

Find the body of `gate()` after the `find_exact` check and before `embedding is not None`. Insert the supersede branch:

```python
    if find_exact(conn, h):
        return GateResult(outcome="exact_dup", hash=h)

    # NEW: source_url supersede check.
    if source_url:
        live = conn.execute(
            "SELECT hash, revision FROM content "
            "WHERE source_url = ? AND is_current = 1 AND tombstoned = 0 "
            "ORDER BY revision DESC LIMIT 1",
            (source_url,),
        ).fetchone()
        if live:
            new_revision = int(live["revision"]) + 1
            conn.execute(
                """INSERT INTO content
                   (hash, body, title, source_url, source_tier, fetched_at,
                    confidence, ttl_days, kind, revision, is_current, source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (h, body, title, source_url, source_tier, None,
                 confidence, ttl_days, kind, new_revision, None),
            )
            conn.execute(
                "UPDATE content SET is_current = 0, superseded_by = ? WHERE hash = ?",
                (h, live["hash"]),
            )
            event_log.append(
                conn, "superseded",
                {
                    "hash_old": live["hash"],
                    "hash_new": h,
                    "source_url": source_url,
                    "revision": new_revision,
                },
                actor=actor, correlation_id=correlation_id,
            )
            return GateResult(outcome="superseded", hash=h)

    if embedding is not None:
```

(The remainder of `gate()` is unchanged.)

- [ ] **Step 4: Run, verify pass**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_dedup_supersede.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run the full test suite to confirm no regressions**

```bash
/home/nemy/.engram/.venv/bin/pytest -q
```

Expected: all tests so far pass.

- [ ] **Step 6: Commit**

```bash
git add src/engram/dedup.py tests/sources/test_dedup_supersede.py
git commit -m "dedup: superseded outcome for source_url version chain"
```

---

## Task 5: Vault path stability — URL-derived filename for sourced content

**Files:**
- Modify: `src/engram/projector/renderers.py`
- Test: `tests/sources/test_vault_path.py`

- [ ] **Step 1: Write the failing test**

Create `tests/sources/test_vault_path.py`:

```python
"""Sourced content (non-null source_url + source_id) renders to a stable
URL-derived path so successive revisions overwrite the same file. Non-sourced
content keeps the existing title-slug-hash scheme.
"""
import sqlite3

from engram.projector.renderers import render_kb


def _row(**fields):
    """Build a sqlite3.Row-like mapping (dict suffices since render_kb only indexes by name)."""
    defaults = dict(
        hash="hashabcdef0123456789",
        title=None,
        source_url=None,
        source_tier="manual",
        fetched_at=None,
        confidence=0.5,
        ttl_days=None,
        kind="kb",
        body="hello",
        source_id=None,
    )
    defaults.update(fields)
    # render_kb uses row["k"] subscript; dict satisfies that.
    return defaults


def test_non_sourced_uses_title_hash_path():
    row = _row(title="My Note", hash="abcdef0123" * 6)
    path, _ = render_kb(row, "050-kb")
    assert path == "050-kb/my-note-abcdef01.md"


def test_sourced_uses_url_derived_path():
    row = _row(
        title="Engine Install",
        source_url="https://docs.docker.com/engine/install/linux/",
        source_id="docker-docs-linux",
        kind="research",
    )
    path, _ = render_kb(row, "030-research")
    # URL path tail "linux" → slug; source_id first 8 chars → "docker-d"
    assert path == "030-research/linux-docker-d.md"


def test_sourced_two_revisions_same_path():
    r1 = _row(
        hash="rev1hash" * 8,
        source_url="https://example.com/foo/bar/",
        source_id="example-src",
        title="First",
    )
    r2 = _row(
        hash="rev2hash" * 8,
        source_url="https://example.com/foo/bar/",
        source_id="example-src",
        title="Second",
    )
    p1, _ = render_kb(r1, "030-research")
    p2, _ = render_kb(r2, "030-research")
    assert p1 == p2  # stable across revisions
```

- [ ] **Step 2: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_vault_path.py -v
```

Expected: 2 failures (sourced cases produce title-hash path).

- [ ] **Step 3: Modify `src/engram/projector/renderers.py`**

Replace the `render_kb` function body's path computation. Find:

```python
    slug = _safe_slug(row["title"], row["hash"][:12])
    path = f"{kind_dir}/{slug}-{row['hash'][:8]}.md"
```

Replace with:

```python
    if row["source_url"] and row.get("source_id") if isinstance(row, dict) else (row["source_url"] and row["source_id"]):
        # URL-derived stable path so revisions overwrite the same file.
        from urllib.parse import urlparse
        url_path = urlparse(row["source_url"]).path.rstrip("/")
        tail = url_path.rsplit("/", 1)[-1] or "index"
        slug = _safe_slug(tail, row["hash"][:12])
        suffix = (row["source_id"] or "")[:8] or row["hash"][:8]
        path = f"{kind_dir}/{slug}-{suffix}.md"
    else:
        slug = _safe_slug(row["title"], row["hash"][:12])
        path = f"{kind_dir}/{slug}-{row['hash'][:8]}.md"
```

Note: `row` may be a `sqlite3.Row` (no `.get()`) or a dict in tests. The conditional handles both — sqlite3.Row supports `["key"]` access; check via try/except is cleaner. Use this form instead:

```python
    try:
        sid = row["source_id"]
    except (KeyError, IndexError):
        sid = None
    if row["source_url"] and sid:
        from urllib.parse import urlparse
        url_path = urlparse(row["source_url"]).path.rstrip("/")
        tail = url_path.rsplit("/", 1)[-1] or "index"
        slug = _safe_slug(tail, row["hash"][:12])
        suffix = sid[:8] or row["hash"][:8]
        path = f"{kind_dir}/{slug}-{suffix}.md"
    else:
        slug = _safe_slug(row["title"], row["hash"][:12])
        path = f"{kind_dir}/{slug}-{row['hash'][:8]}.md"
```

- [ ] **Step 4: Run tests, verify pass**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_vault_path.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Run all tests**

```bash
/home/nemy/.engram/.venv/bin/pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/engram/projector/renderers.py tests/sources/test_vault_path.py
git commit -m "projector: URL-derived vault path for sourced content"
```

---

## Task 6: Projector — handle `superseded` events

**Files:**
- Modify: `src/engram/projector/projector.py`
- Test: `tests/sources/test_projector_superseded.py`

- [ ] **Step 1: Write the failing test**

Create `tests/sources/test_projector_superseded.py`:

```python
"""Projector handles `superseded` events: re-renders the new hash to the same
canonical path the old hash occupied, updates vault_state."""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    from engram.common import config as cfg_mod
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    yield c


def test_handle_superseded_overwrites_vault_path(conn, tmp_path):
    from engram.dedup import gate
    from engram.projector.projector import _handle_event
    from engram import log as event_log

    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}

    # Seed: source_id row + first revision via gate (use source_url+source_id path)
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule) "
        "VALUES ('docker-docs', 'Docker', 'sitemap', 'https://x', '7d')"
    )
    r1 = gate(conn, body="v1 body", source_url="https://docs.docker.com/engine/install/",
              source_tier="vendor-doc", kind="research")
    # Manually attach source_id so URL-derived path triggers
    conn.execute("UPDATE content SET source_id='docker-docs' WHERE hash=?", (r1.hash,))

    # Render the first revision via the ingested event
    ingested_evt = list(event_log.since(conn, 0, types=["ingested"]))[-1]
    _handle_event(conn, vault, ingested_evt, kind_dirs)

    first_path = conn.execute(
        "SELECT vault_path FROM vault_state WHERE content_hash=?", (r1.hash,)
    ).fetchone()["vault_path"]
    assert (vault / first_path).read_text().find("v1 body") > 0

    # Supersede: gate emits a superseded event
    r2 = gate(conn, body="v2 changed body", source_url="https://docs.docker.com/engine/install/",
              source_tier="vendor-doc", kind="research")
    conn.execute("UPDATE content SET source_id='docker-docs' WHERE hash=?", (r2.hash,))

    sup_evt = list(event_log.since(conn, 0, types=["superseded"]))[-1]
    _handle_event(conn, vault, sup_evt, kind_dirs)

    # Same on-disk file path, new content
    new_text = (vault / first_path).read_text()
    assert "v2 changed body" in new_text
    assert "v1 body" not in new_text

    # vault_state row for old hash gone; row for new hash points to same path
    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE content_hash=?", (r1.hash,)
    ).fetchone() is None
    new_state = conn.execute(
        "SELECT vault_path FROM vault_state WHERE content_hash=?", (r2.hash,)
    ).fetchone()
    assert new_state["vault_path"] == first_path
```

- [ ] **Step 2: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_projector_superseded.py -v
```

Expected: failure — projector's `_handle_event` only handles `ingested` and `merged`.

- [ ] **Step 3: Modify `src/engram/projector/projector.py`**

In `_handle_event`, add a branch for `superseded`. Find:

```python
    elif evt.type == "merged":
```

After the existing `merged` block, add:

```python
    elif evt.type == "superseded":
        hash_old = evt.payload.get("hash_old")
        hash_new = evt.payload.get("hash_new")
        old_state = conn.execute(
            "SELECT vault_path, rendered_body FROM vault_state WHERE content_hash = ?",
            (hash_old,),
        ).fetchone()
        if old_state and old_state["vault_path"]:
            old_path = old_state["vault_path"]
            # Re-render new content
            new_row = conn.execute(
                "SELECT * FROM content WHERE hash = ? AND tombstoned = 0",
                (hash_new,),
            ).fetchone()
            if new_row:
                from .renderers import RENDERERS
                renderer = RENDERERS.get(new_row["kind"], RENDERERS["kb"])
                kind_dir = kind_dirs.get(new_row["kind"], kind_dirs.get("kb", "050-kb"))
                _, body = renderer(new_row, kind_dir)
                abs_path = vault / old_path
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(body)
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute("DELETE FROM vault_state WHERE content_hash = ?", (hash_old,))
                conn.execute(
                    "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(vault_path) DO UPDATE SET content_hash=excluded.content_hash, "
                    "rendered_body=excluded.rendered_body, rendered_at=excluded.rendered_at",
                    (old_path, hash_new, body, now),
                )
                conn.execute("UPDATE content SET vault_path = ? WHERE hash = ?",
                             (old_path, hash_new))
```

Also update the `event_log.since(...)` call in `run()` to include `superseded`:

Find:
```python
            for evt in event_log.since(conn, cursor, types=["ingested", "merged"]):
```
Replace with:
```python
            for evt in event_log.since(conn, cursor, types=["ingested", "merged", "superseded"]):
```

- [ ] **Step 4: Run, verify pass**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_projector_superseded.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Run full suite**

```bash
/home/nemy/.engram/.venv/bin/pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/engram/projector/projector.py tests/sources/test_projector_superseded.py
git commit -m "projector: handle superseded events (overwrite same vault path)"
```

---

## Task 7: Sitemap adapter

**Files:**
- Create: `src/engram/poller/adapters/sitemap.py`
- Test: `tests/sources/test_sitemap_adapter.py`
- Fixture: `tests/fixtures/sitemap_minimal.xml`

- [ ] **Step 1: Create the fixture sitemap**

Create `tests/fixtures/sitemap_minimal.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://docs.example.com/engine/install/linux/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://docs.example.com/engine/cli/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://docs.example.com/desktop/install/macos/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://docs.example.com/blog/2026/announcement/</loc><lastmod>2026-05-01</lastmod></url>
</urlset>
```

- [ ] **Step 2: Write the failing test**

Create `tests/sources/test_sitemap_adapter.py`:

```python
"""Sitemap adapter: parses sitemap.xml, applies include/exclude globs,
fetches pages with ETag honoring, yields Candidates."""
import json
from pathlib import Path

import httpx
import pytest

from engram.poller.adapters.sitemap import SitemapAdapter

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "tests" / "fixtures"


def _build_source(**overrides) -> dict:
    base = {
        "id": "test",
        "url": "https://docs.example.com/sitemap.xml",
        "config": json.dumps({
            "include": ["*/engine/*"],
            "exclude": ["*/macos/*"],
        }),
        "cursor": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_filter_includes_engine_excludes_macos(monkeypatch):
    """Adapter filters URLs through include/exclude globs."""
    sitemap_xml = (FIXTURE_DIR / "sitemap_minimal.xml").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        # Return page bodies for individual fetches
        return httpx.Response(200, text=f"<html><body><h1>{request.url.path}</h1>page body</body></html>",
                              headers={"content-type": "text/html", "etag": f'"{request.url.path}"'})

    transport = httpx.MockTransport(handler)
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))

    cands = []
    async for c in adapter.fetch(_build_source()):
        cands.append(c)

    urls = [c.source_url for c in cands]
    assert "https://docs.example.com/engine/install/linux/" in urls
    assert "https://docs.example.com/engine/cli/" in urls
    assert all("/macos/" not in u for u in urls)
    assert all("/blog/" not in u for u in urls)
    assert len(cands) == 2


@pytest.mark.asyncio
async def test_etag_skips_unchanged(monkeypatch):
    """If a URL's etag matches the cursor, the adapter does not yield it again."""
    sitemap_xml = (FIXTURE_DIR / "sitemap_minimal.xml").read_text()

    target_url = "https://docs.example.com/engine/install/linux/"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        # Honor If-None-Match
        if request.headers.get("if-none-match") == '"abc"' and str(request.url) == target_url:
            return httpx.Response(304)
        return httpx.Response(200, text="page body",
                              headers={"content-type": "text/html", "etag": '"new"'})

    transport = httpx.MockTransport(handler)
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))

    src = _build_source(cursor=json.dumps({"etags": {target_url: '"abc"'}}))
    cands = []
    async for c in adapter.fetch(src):
        cands.append(c)

    urls = [c.source_url for c in cands]
    # The Linux page returned 304 → not in candidates. The CLI page still yielded.
    assert target_url not in urls
    assert "https://docs.example.com/engine/cli/" in urls


@pytest.mark.asyncio
async def test_updates_cursor_etags(monkeypatch):
    """After fetching, the adapter writes new etags into the source's cursor field."""
    sitemap_xml = (FIXTURE_DIR / "sitemap_minimal.xml").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        return httpx.Response(200, text="page",
                              headers={"content-type": "text/html",
                                       "etag": f'"etag-{request.url.path}"'})

    transport = httpx.MockTransport(handler)
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=transport))

    src = _build_source()
    async for _ in adapter.fetch(src):
        pass
    new_cursor = json.loads(src["cursor"])
    assert "etags" in new_cursor
    assert new_cursor["etags"]["https://docs.example.com/engine/install/linux/"] == \
        '"etag-/engine/install/linux/"'
```

- [ ] **Step 3: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_sitemap_adapter.py -v
```

Expected: ImportError (no `sitemap.py`).

- [ ] **Step 4: Implement the adapter**

Create `src/engram/poller/adapters/sitemap.py`:

```python
"""Sitemap adapter: walks a site's sitemap.xml, optionally honoring sitemap-index
files, applies URL globs, fetches matching pages with ETag-based 304 handling,
extracts text via trafilatura, yields Candidates."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator
from xml.etree import ElementTree as ET

import httpx
import trafilatura

from . import Adapter, Candidate, matches_globs, register

logger = logging.getLogger("engram.poller.sitemap")
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class SitemapAdapter:
    name = "sitemap"

    def __init__(self, *, _client: httpx.AsyncClient | None = None,
                 user_agent: str = "engram/0.1 (+source-poller)") -> None:
        self._client = _client or httpx.AsyncClient(
            headers={"user-agent": user_agent}, timeout=30.0,
        )

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        cfg = json.loads(source.get("config") or "{}")
        include = cfg.get("include", [])
        exclude = cfg.get("exclude", [])
        cursor = json.loads(source.get("cursor") or "{}")
        etags: dict[str, str] = cursor.get("etags", {})

        urls = await self._collect_urls(source["url"])
        new_etags: dict[str, str] = {}
        for u in urls:
            if not matches_globs(u, include, exclude):
                continue
            previous = etags.get(u)
            cand = await self._fetch_one(u, previous_etag=previous)
            if cand is None:
                # 304 not modified — keep the prior etag, do not yield
                if previous:
                    new_etags[u] = previous
                continue
            etag, candidate = cand
            if etag:
                new_etags[u] = etag
            yield candidate

        # Mutate source cursor in place so the poller writes it back atomically.
        source["cursor"] = json.dumps({
            "etags": new_etags,
            "last_seen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    async def _collect_urls(self, sitemap_url: str) -> list[str]:
        """Fetch sitemap.xml; if it's a sitemap-index, descend one level."""
        out: list[str] = []
        resp = await self._client.get(sitemap_url)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        # Sitemap-index?
        if root.tag.endswith("sitemapindex"):
            for sm in root.findall("sm:sitemap/sm:loc", NS):
                if sm.text:
                    out.extend(await self._collect_urls(sm.text.strip()))
        else:
            for loc in root.findall("sm:url/sm:loc", NS):
                if loc.text:
                    out.append(loc.text.strip())
        return out

    async def _fetch_one(
        self, url: str, *, previous_etag: str | None
    ) -> tuple[str | None, Candidate] | None:
        headers: dict[str, str] = {}
        if previous_etag:
            headers["if-none-match"] = previous_etag
        resp = await self._client.get(url, headers=headers)
        if resp.status_code == 304:
            return None
        resp.raise_for_status()
        body_html = resp.text
        extracted = trafilatura.extract(body_html, include_comments=False) or body_html
        title = trafilatura.extract_metadata(body_html)
        title_str = title.title if title and title.title else None
        etag = resp.headers.get("etag")
        cand = Candidate(
            source_url=url,
            body=extracted,
            title=title_str,
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            metadata={"etag": etag, "content_type": resp.headers.get("content-type")},
        )
        return etag, cand


register(SitemapAdapter())
```

- [ ] **Step 5: Run tests**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_sitemap_adapter.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/engram/poller/adapters/sitemap.py tests/sources/test_sitemap_adapter.py tests/fixtures/sitemap_minimal.xml
git commit -m "poller: sitemap adapter with ETag-based change detection"
```

---

## Task 8: GitHub-repo adapter

**Files:**
- Create: `src/engram/poller/adapters/github_repo.py`
- Test: `tests/sources/test_github_adapter.py`
- Fixture: `tests/fixtures/github_compare_response.json`, `tests/fixtures/github_tree_response.json`

- [ ] **Step 1: Create fixtures**

Create `tests/fixtures/github_tree_response.json`:

```json
{
  "sha": "head1",
  "tree": [
    {"path": "docs/engine/install.md", "type": "blob", "sha": "blob1"},
    {"path": "docs/engine/upgrade.md", "type": "blob", "sha": "blob2"},
    {"path": "docs/desktop/macos.md",  "type": "blob", "sha": "blob3"},
    {"path": "README.md",              "type": "blob", "sha": "blob4"}
  ],
  "truncated": false
}
```

Create `tests/fixtures/github_compare_response.json`:

```json
{
  "files": [
    {"filename": "docs/engine/install.md", "status": "modified"},
    {"filename": "docs/engine/new-page.md", "status": "added"},
    {"filename": "docs/desktop/macos.md", "status": "modified"}
  ],
  "merge_base_commit": {"sha": "head1"},
  "commits": [{"sha": "head2"}]
}
```

- [ ] **Step 2: Write the failing test**

Create `tests/sources/test_github_adapter.py`:

```python
import json
from pathlib import Path

import httpx
import pytest

from engram.poller.adapters.github_repo import GitHubRepoAdapter

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures"


def _src(**overrides):
    base = {
        "id": "docker-docs",
        "url": "https://github.com/docker/docs",
        "config": json.dumps({"include": ["docs/engine/**"], "branch": "main"}),
        "cursor": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_first_run_walks_tree_and_filters(monkeypatch):
    """No cursor → walks tree at HEAD, applies include glob."""
    tree = (FIX / "github_tree_response.json").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/repos/docker/docs/branches/main":
            return httpx.Response(200, json={"commit": {"sha": "head1"}})
        if p == "/repos/docker/docs/git/trees/head1":
            return httpx.Response(200, text=tree, headers={"content-type": "application/json"})
        if p.startswith("/repos/docker/docs/contents/docs/engine/install.md"):
            return httpx.Response(200, text="install body",
                                  headers={"content-type": "text/plain"})
        if p.startswith("/repos/docker/docs/contents/docs/engine/upgrade.md"):
            return httpx.Response(200, text="upgrade body",
                                  headers={"content-type": "text/plain"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    adapter = GitHubRepoAdapter(_client=httpx.AsyncClient(transport=transport))

    src = _src()
    cands = [c async for c in adapter.fetch(src)]
    paths = sorted(c.source_url for c in cands)
    assert paths == sorted([
        "https://github.com/docker/docs/blob/head1/docs/engine/install.md",
        "https://github.com/docker/docs/blob/head1/docs/engine/upgrade.md",
    ])
    # Cursor advanced
    cursor = json.loads(src["cursor"])
    assert cursor["last_sha"] == "head1"


@pytest.mark.asyncio
async def test_subsequent_run_uses_compare(monkeypatch):
    """With cursor present, adapter calls compare-API and only fetches changed files."""
    cmp_resp = (FIX / "github_compare_response.json").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/repos/docker/docs/branches/main":
            return httpx.Response(200, json={"commit": {"sha": "head2"}})
        if p == "/repos/docker/docs/compare/head1...head2":
            return httpx.Response(200, text=cmp_resp,
                                  headers={"content-type": "application/json"})
        if "/contents/docs/engine/install.md" in p:
            return httpx.Response(200, text="updated install")
        if "/contents/docs/engine/new-page.md" in p:
            return httpx.Response(200, text="new page body")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    adapter = GitHubRepoAdapter(_client=httpx.AsyncClient(transport=transport))

    src = _src(cursor=json.dumps({"last_sha": "head1"}))
    cands = [c async for c in adapter.fetch(src)]
    urls = sorted(c.source_url for c in cands)
    # macos file excluded by glob; only engine/* matches
    assert urls == sorted([
        "https://github.com/docker/docs/blob/head2/docs/engine/install.md",
        "https://github.com/docker/docs/blob/head2/docs/engine/new-page.md",
    ])
    cursor = json.loads(src["cursor"])
    assert cursor["last_sha"] == "head2"
```

- [ ] **Step 3: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_github_adapter.py -v
```

Expected: ImportError.

- [ ] **Step 4: Implement adapter**

Create `src/engram/poller/adapters/github_repo.py`:

```python
"""GitHub-repo adapter: tracks a public repo by branch HEAD; uses the compare
API for incremental updates after the first walk."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from . import Adapter, Candidate, matches_globs, register

logger = logging.getLogger("engram.poller.github_repo")

_REPO_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)/?(?:$|\.git$)")


def _parse_repo(url: str) -> tuple[str, str]:
    m = _REPO_RE.match(url)
    if not m:
        raise ValueError(f"not a github.com/<org>/<repo> URL: {url}")
    return m.group(1), m.group(2)


class GitHubRepoAdapter:
    name = "github-repo"

    def __init__(
        self,
        *,
        _client: httpx.AsyncClient | None = None,
        user_agent: str = "engram/0.1 (+source-poller)",
    ) -> None:
        token = os.environ.get("GITHUB_TOKEN")
        headers: dict[str, str] = {"user-agent": user_agent,
                                    "accept": "application/vnd.github+json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._client = _client or httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30.0,
        )

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        cfg = json.loads(source.get("config") or "{}")
        include = cfg.get("include", [])
        exclude = cfg.get("exclude", [])
        branch = cfg.get("branch", "main")
        cursor = json.loads(source.get("cursor") or "{}")
        last_sha: str | None = cursor.get("last_sha")
        owner, repo = _parse_repo(source["url"])

        head_resp = await self._client.get(f"/repos/{owner}/{repo}/branches/{branch}")
        head_resp.raise_for_status()
        head_sha = head_resp.json()["commit"]["sha"]

        if last_sha and last_sha != head_sha:
            paths = await self._changed_paths(owner, repo, last_sha, head_sha)
        elif last_sha == head_sha:
            paths = []
        else:
            paths = await self._tree_paths(owner, repo, head_sha)

        for path in paths:
            if not matches_globs(path, include, exclude):
                continue
            body = await self._fetch_file(owner, repo, head_sha, path)
            if body is None:
                continue
            url = f"https://github.com/{owner}/{repo}/blob/{head_sha}/{path}"
            yield Candidate(
                source_url=url,
                body=body,
                title=path.rsplit("/", 1)[-1],
                fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                metadata={"sha": head_sha, "path": path},
            )

        source["cursor"] = json.dumps({"last_sha": head_sha})

    async def _tree_paths(self, owner: str, repo: str, sha: str) -> list[str]:
        r = await self._client.get(f"/repos/{owner}/{repo}/git/trees/{sha}",
                                    params={"recursive": "1"})
        r.raise_for_status()
        data = r.json()
        return [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]

    async def _changed_paths(self, owner: str, repo: str, base: str, head: str) -> list[str]:
        r = await self._client.get(f"/repos/{owner}/{repo}/compare/{base}...{head}")
        r.raise_for_status()
        data = r.json()
        return [
            f["filename"] for f in data.get("files", [])
            if f.get("status") in ("added", "modified", "renamed")
        ]

    async def _fetch_file(self, owner: str, repo: str, sha: str, path: str) -> str | None:
        r = await self._client.get(
            f"/repos/{owner}/{repo}/contents/{path}", params={"ref": sha},
            headers={"accept": "application/vnd.github.v3.raw"},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text


register(GitHubRepoAdapter())
```

- [ ] **Step 5: Run tests**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_github_adapter.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/engram/poller/adapters/github_repo.py tests/sources/test_github_adapter.py tests/fixtures/github_compare_response.json tests/fixtures/github_tree_response.json
git commit -m "poller: github-repo adapter with compare-API change detection"
```

---

## Task 9: Poller main loop

**Files:**
- Create: `src/engram/poller/poller.py`
- Test: `tests/sources/test_poller_loop.py`

- [ ] **Step 1: Write the failing test**

Create `tests/sources/test_poller_loop.py`:

```python
"""Poller main loop: scan due sources, dispatch adapter, gate candidates,
update source state."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    from engram.common import config as cfg_mod
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    yield c


class FakeAdapter:
    """Yields a fixed list of Candidates. Bumps cursor to 'fake-cursor-N' each call."""
    name = "fake"
    def __init__(self, candidates):
        self._cands = candidates
        self.calls = 0
    async def fetch(self, source):
        self.calls += 1
        for c in self._cands:
            yield c
        source["cursor"] = json.dumps({"n": self.calls})


@pytest.mark.asyncio
async def test_poll_one_runs_due_source_and_advances_state(conn, monkeypatch):
    from engram.poller.poller import poll_one
    from engram.poller.adapters import Candidate, ADAPTERS

    fake = FakeAdapter([
        Candidate(source_url="https://x/a", body="A body", title="A"),
        Candidate(source_url="https://x/b", body="B body", title="B"),
    ])
    monkeypatch.setitem(ADAPTERS, "fake", fake)

    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier) "
        "VALUES ('s1', 'Test', 'fake', 'https://x', '1d', 'manual')"
    )
    src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
    await poll_one(conn, src)

    rows = conn.execute("SELECT outcome FROM (SELECT type AS outcome FROM events WHERE type='ingested')").fetchall()
    assert len(rows) == 2

    final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()
    assert final["last_polled_at"] is not None
    assert final["last_success_at"] is not None
    assert final["next_poll_at"] is not None
    assert final["error_count"] == 0
    assert json.loads(final["cursor"])["n"] == 1


@pytest.mark.asyncio
async def test_due_query_skips_paused_and_future(conn):
    from engram.poller.poller import select_due
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, paused, next_poll_at) "
        "VALUES ('past', 'p', 'fake', 'x', '1d', 0, NULL),"
        "       ('paused', 'q', 'fake', 'x', '1d', 1, NULL),"
        "       ('future', 'r', 'fake', 'x', '1d', 0, ?)",
        (future,),
    )
    due = select_due(conn)
    ids = sorted(s["id"] for s in due)
    assert ids == ["past"]
```

- [ ] **Step 2: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_poller_loop.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/engram/poller/poller.py`:

```python
"""Poller daemon main loop. Per-tick: scan due sources, dispatch adapter,
push candidates through dedup.gate, update source state in one tx."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import log as event_log
from ..common.config import load_config
from ..common.db import get_connection
from ..dedup import gate
from .adapters import ADAPTERS
from .schedule import parse_interval

logger = logging.getLogger("engram.poller")

CIRCUIT_BREAK_THRESHOLD = 5


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sources "
        "WHERE paused = 0 "
        "  AND (next_poll_at IS NULL OR next_poll_at <= ?)",
        (_utcnow_iso(),),
    ).fetchall()


def _classify_error(exc: Exception) -> tuple[bool, str]:
    """Return (retryable, short_message)."""
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status >= 500
        return retryable, f"HTTP {status}: {exc.response.url}"
    if isinstance(exc, (httpx.NetworkError, httpx.TimeoutException)):
        return True, f"network: {exc.__class__.__name__}"
    return False, f"{exc.__class__.__name__}: {exc}"


async def poll_one(conn: sqlite3.Connection, source: dict[str, Any]) -> dict[str, int]:
    """Poll one source. Returns {ingested, superseded, exact_dup, errors}."""
    adapter = ADAPTERS.get(source["adapter"])
    if adapter is None:
        raise ValueError(f"unknown adapter: {source['adapter']}")
    src_dict = dict(source)  # mutable copy adapter writes cursor into
    counts = {"ingested": 0, "superseded": 0, "exact_dup": 0, "errors": 0,
              "candidates_seen": 0}
    error_msg = None
    try:
        async for cand in adapter.fetch(src_dict):
            counts["candidates_seen"] += 1
            try:
                # Inject source_id on the way through; apply via UPDATE post-insert
                # since gate's signature doesn't take it.
                r = gate(
                    conn, body=cand.body, title=cand.title,
                    source_url=cand.source_url, source_tier=source["source_tier"],
                    confidence=0.7, kind="research",
                )
                if r.outcome == "new":
                    conn.execute(
                        "UPDATE content SET source_id = ? WHERE hash = ?",
                        (source["id"], r.hash),
                    )
                    counts["ingested"] += 1
                elif r.outcome == "superseded":
                    conn.execute(
                        "UPDATE content SET source_id = ? WHERE hash = ?",
                        (source["id"], r.hash),
                    )
                    counts["superseded"] += 1
                elif r.outcome == "exact_dup":
                    counts["exact_dup"] += 1
            except Exception:
                logger.exception("gate failed for %s", cand.source_url)
                counts["errors"] += 1
    except Exception as exc:
        retryable, msg = _classify_error(exc)
        error_msg = msg
        counts["errors"] += 1
        event_log.append(
            conn, "source_error",
            {"source_id": source["id"], "error": msg, "retryable": retryable},
            actor="poller",
        )

    # Compute next_poll_at and update state.
    interval = parse_interval(source["schedule"])
    next_at = (datetime.now(timezone.utc) + interval).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_error_count = (source["error_count"] or 0) + 1 if error_msg else 0
    paused = 1 if new_error_count >= CIRCUIT_BREAK_THRESHOLD else (source["paused"] or 0)
    conn.execute(
        "UPDATE sources SET cursor = ?, last_polled_at = ?, "
        " last_success_at = COALESCE(?, last_success_at), "
        " next_poll_at = ?, error_count = ?, last_error = ?, paused = ?, "
        " updated_at = ? WHERE id = ?",
        (
            src_dict.get("cursor"),
            _utcnow_iso(),
            None if error_msg else _utcnow_iso(),
            next_at,
            new_error_count,
            error_msg,
            paused,
            _utcnow_iso(),
            source["id"],
        ),
    )
    if paused == 1 and new_error_count >= CIRCUIT_BREAK_THRESHOLD:
        event_log.append(
            conn, "source_circuit_broken",
            {"source_id": source["id"], "error_count": new_error_count},
            actor="poller",
        )
    event_log.append(
        conn, "source_polled",
        {
            "source_id": source["id"],
            "candidates_seen": counts["candidates_seen"],
            "ingested": counts["ingested"],
            "superseded": counts["superseded"],
            "exact_dup": counts["exact_dup"],
            "errors": counts["errors"],
        },
        actor="poller",
    )
    conn.commit()
    return counts


async def run() -> None:
    cfg = load_config()
    conn = get_connection()
    logger.info("poller starting")
    while True:
        try:
            for src in select_due(conn):
                try:
                    counts = await poll_one(conn, dict(src))
                    logger.info("polled %s: %s", src["id"], counts)
                except Exception:
                    logger.exception("poll_one failed for %s", src["id"])
        except Exception:
            logger.exception("poller tick failed")
        await asyncio.sleep(60)
```

- [ ] **Step 4: Run tests**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_poller_loop.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/engram/poller/poller.py tests/sources/test_poller_loop.py
git commit -m "poller: main loop with per-source polling and state updates"
```

---

## Task 10: Poller entrypoint + console script + systemd unit

**Files:**
- Create: `src/engram/poller/__main__.py`
- Modify: `pyproject.toml`
- Create: `systemd/engram-poller.service`

- [ ] **Step 1: Create `__main__.py`**

```python
"""Entry point: engram-poller console script."""
from __future__ import annotations

import asyncio
import logging
import sys

from .poller import run


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add the console script to `pyproject.toml`**

In `pyproject.toml`, in `[project.scripts]`, add a new line so the section reads:

```toml
[project.scripts]
engram-mcp        = "engram.mcp_server.__main__:main"
engram-projector  = "engram.projector.__main__:main"
engram-watcher    = "engram.watcher.__main__:main"
engram-reactor    = "engram.reactor.__main__:main"
engram-rag        = "engram.rag.__main__:main"
engram-poller     = "engram.poller.__main__:main"
```

- [ ] **Step 3: Create the systemd unit**

Create `systemd/engram-poller.service`:

```
[Unit]
Description=Engram source poller (sitemap + github-repo adapters)
After=network.target

[Service]
Type=simple
Environment=ENGRAM_CONFIG=%h/.engram/config.yml
EnvironmentFile=-%h/.engram/.env
ExecStart=%h/.engram/.venv/bin/engram-poller
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

- [ ] **Step 4: Re-install editable so the console script lands in the venv**

```bash
uv pip install --python /home/nemy/.engram/.venv/bin/python -e .
ls -la /home/nemy/.engram/.venv/bin/engram-poller
```

Expected: file exists, executable.

- [ ] **Step 5: Smoke-test the entrypoint** (5 seconds, ctrl-c)

```bash
timeout 3 /home/nemy/.engram/.venv/bin/engram-poller 2>&1 | head -3
```

Expected: at least one "poller starting" log line.

- [ ] **Step 6: Install and start the systemd unit**

```bash
cp systemd/engram-poller.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now engram-poller.service
systemctl --user status engram-poller.service --no-pager | head -10
```

Expected: `Active: active (running)`.

- [ ] **Step 7: Commit**

```bash
git add src/engram/poller/__main__.py pyproject.toml systemd/engram-poller.service
git commit -m "poller: entrypoint, console script, systemd unit"
```

---

## Task 11: MCP `sources.*` namespace

**Files:**
- Create: `src/engram/mcp_server/tools/sources.py`
- Modify: `src/engram/mcp_server/tools/__init__.py`
- Test: `tests/sources/test_mcp_sources.py`

- [ ] **Step 1: Read existing tool registration pattern**

```bash
head -40 src/engram/mcp_server/tools/__init__.py
head -40 src/engram/mcp_server/tools/kb.py
```

(This step is a read-only context check; no commit. The new file mirrors `kb.py`'s structure: a `register(conn)` function that returns a dict mapping tool names to handler dicts.)

- [ ] **Step 2: Write the failing test**

Create `tests/sources/test_mcp_sources.py`:

```python
"""sources.* MCP tools: add/list/get/set/remove/fetch_now."""
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    _apply(c)
    yield c


def test_add_creates_row(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    handler = tools["sources.add"]["handler"]
    out = handler({
        "id": "docker-docs",
        "name": "Docker Docs",
        "adapter": "sitemap",
        "url": "https://docs.docker.com/sitemap.xml",
        "config": {"include": ["*/engine/*"]},
        "schedule": "7d",
    })
    assert out["id"] == "docker-docs"
    row = conn.execute("SELECT * FROM sources WHERE id='docker-docs'").fetchone()
    assert row["adapter"] == "sitemap"
    assert json.loads(row["config"])["include"] == ["*/engine/*"]


def test_add_uses_default_schedule_per_adapter(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    h = tools["sources.add"]["handler"]
    h({"id": "a", "name": "a", "adapter": "sitemap", "url": "u"})
    h({"id": "b", "name": "b", "adapter": "github-repo", "url": "https://github.com/x/y"})
    rows = {r["id"]: r["schedule"] for r in conn.execute("SELECT id, schedule FROM sources")}
    assert rows == {"a": "7d", "b": "1d"}


def test_list_returns_all(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    add = tools["sources.add"]["handler"]
    add({"id": "x", "name": "x", "adapter": "sitemap", "url": "u"})
    add({"id": "y", "name": "y", "adapter": "sitemap", "url": "u"})
    out = tools["sources.list"]["handler"]({})
    ids = sorted(s["id"] for s in out)
    assert ids == ["x", "y"]


def test_get_returns_full_row(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "Xx", "adapter": "sitemap", "url": "u"})
    out = tools["sources.get"]["handler"]({"id": "x"})
    assert out["name"] == "Xx"


def test_set_updates_fields(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u"})
    out = tools["sources.set"]["handler"]({"id": "x", "paused": True, "schedule": "1d"})
    assert "paused" in out["updated_fields"]
    row = conn.execute("SELECT paused, schedule FROM sources WHERE id='x'").fetchone()
    assert row["paused"] == 1
    assert row["schedule"] == "1d"


def test_remove_deletes(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u"})
    tools["sources.remove"]["handler"]({"id": "x"})
    assert conn.execute("SELECT 1 FROM sources WHERE id='x'").fetchone() is None


def test_fetch_now_clears_next_poll_at(conn):
    """Triggering fetch_now sets next_poll_at to NULL so the daemon picks it up next tick."""
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u",
        "schedule": "7d",
    })
    # Initial add should set next_poll_at to NULL already; force a future value to test clearing.
    conn.execute("UPDATE sources SET next_poll_at='2099-01-01T00:00:00Z' WHERE id='x'")
    out = tools["sources.fetch_now"]["handler"]({"id": "x"})
    assert out["triggered"] is True
    after = conn.execute("SELECT next_poll_at FROM sources WHERE id='x'").fetchone()
    assert after["next_poll_at"] is None
```

- [ ] **Step 3: Run, verify failure**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_mcp_sources.py -v
```

Expected: ImportError (no `sources.py` in tools).

- [ ] **Step 4: Implement**

Create `src/engram/mcp_server/tools/sources.py`:

```python
"""MCP tools: sources.* namespace for CRUD on the sources registry."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

DEFAULT_SCHEDULE = {"sitemap": "7d", "github-repo": "1d"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "config" in d and isinstance(d["config"], str):
        try:
            d["config"] = json.loads(d["config"])
        except (TypeError, ValueError):
            pass
    return d


def register(conn: sqlite3.Connection) -> dict[str, dict]:

    def add(args: dict[str, Any]) -> dict[str, Any]:
        adapter = args["adapter"]
        if adapter not in DEFAULT_SCHEDULE:
            return {"error": f"unknown adapter: {adapter}"}
        schedule = args.get("schedule") or DEFAULT_SCHEDULE[adapter]
        config = json.dumps(args.get("config") or {})
        conn.execute(
            "INSERT INTO sources "
            "(id, name, adapter, url, config, schedule, source_tier, paused, next_poll_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                args["id"],
                args["name"],
                adapter,
                args["url"],
                config,
                schedule,
                args.get("source_tier") or "vendor-doc",
                1 if args.get("paused") else 0,
            ),
        )
        conn.commit()
        return {"id": args["id"], "next_poll_at": None}

    def list_(args: dict[str, Any]) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources WHERE 1=1"
        params: list[Any] = []
        if args.get("paused_only"):
            sql += " AND paused = 1"
        if args.get("with_errors"):
            sql += " AND error_count > 0"
        sql += " ORDER BY id"
        return [_row_to_dict(r) for r in conn.execute(sql, params)]

    def get_(args: dict[str, Any]) -> dict[str, Any] | dict[str, str]:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (args["id"],)).fetchone()
        if not row:
            return {"error": "not found"}
        d = _row_to_dict(row)
        # Truncate cursor if huge (>2KB)
        if d.get("cursor") and isinstance(d["cursor"], str) and len(d["cursor"]) > 2048:
            d["cursor"] = d["cursor"][:2048] + "...[truncated]"
        return d

    def remove(args: dict[str, Any]) -> dict[str, Any]:
        cur = conn.execute("DELETE FROM sources WHERE id = ?", (args["id"],))
        conn.commit()
        return {"removed": cur.rowcount > 0}

    def fetch_now(args: dict[str, Any]) -> dict[str, Any]:
        cur = conn.execute(
            "UPDATE sources SET next_poll_at = NULL, updated_at = ? WHERE id = ?",
            (_utcnow_iso(), args["id"]),
        )
        conn.commit()
        return {"triggered": cur.rowcount > 0, "id": args["id"]}

    def set_(args: dict[str, Any]) -> dict[str, Any]:
        fields = []
        params: list[Any] = []
        if "paused" in args:
            fields.append("paused = ?")
            params.append(1 if args["paused"] else 0)
        if "schedule" in args:
            fields.append("schedule = ?")
            params.append(args["schedule"])
        if "config" in args:
            fields.append("config = ?")
            params.append(json.dumps(args["config"]))
        if "source_tier" in args:
            fields.append("source_tier = ?")
            params.append(args["source_tier"])
        if not fields:
            return {"updated_fields": []}
        fields.append("updated_at = ?")
        params.append(_utcnow_iso())
        params.append(args["id"])
        conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        return {"updated_fields": [f.split(" = ")[0] for f in fields if not f.startswith("updated_at")]}

    return {
        "sources.add": {
            "description": "Register a new polled source.",
            "input_schema": {
                "type": "object",
                "required": ["id", "name", "adapter", "url"],
                "properties": {
                    "id":          {"type": "string"},
                    "name":        {"type": "string"},
                    "adapter":     {"type": "string", "enum": ["sitemap", "github-repo"]},
                    "url":         {"type": "string"},
                    "config":      {"type": "object"},
                    "schedule":    {"type": "string"},
                    "source_tier": {"type": "string"},
                    "paused":      {"type": "boolean"},
                },
            },
            "handler": add,
        },
        "sources.list": {
            "description": "List configured sources.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "paused_only": {"type": "boolean"},
                    "with_errors": {"type": "boolean"},
                },
            },
            "handler": list_,
        },
        "sources.get": {
            "description": "Get one source's full record.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "handler": get_,
        },
        "sources.remove": {
            "description": "Delete a source. Does not tombstone its content.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "handler": remove,
        },
        "sources.fetch_now": {
            "description": "Force immediate poll on next daemon tick.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "handler": fetch_now,
        },
        "sources.set": {
            "description": "Update one or more fields on an existing source.",
            "input_schema": {
                "type": "object", "required": ["id"],
                "properties": {
                    "id":          {"type": "string"},
                    "paused":      {"type": "boolean"},
                    "schedule":    {"type": "string"},
                    "config":      {"type": "object"},
                    "source_tier": {"type": "string"},
                },
            },
            "handler": set_,
        },
    }
```

- [ ] **Step 5: Wire the namespace into `tools/__init__.py`**

Open `src/engram/mcp_server/tools/__init__.py`. Find the section that imports and registers each namespace's `register(conn)` function. Add the sources import and registration in the same style. Concretely: if the file looks like

```python
from . import goals, kb, playbook, rag, research

def register_all(conn):
    tools = {}
    tools.update(goals.register(conn))
    tools.update(kb.register(conn))
    ...
    return tools
```

then change the import line to also import `sources` and add `tools.update(sources.register(conn))`. (Engineer: read the file to confirm the exact pattern; the change is one new import + one new line, mirroring the existing namespaces.)

- [ ] **Step 6: Run tests**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/sources/test_mcp_sources.py -v
```

Expected: 7 passed.

- [ ] **Step 7: Run full suite**

```bash
/home/nemy/.engram/.venv/bin/pytest -q
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/engram/mcp_server/tools/sources.py src/engram/mcp_server/tools/__init__.py tests/sources/test_mcp_sources.py
git commit -m "mcp: sources.* namespace (add/list/get/set/remove/fetch_now)"
```

---

## Task 12: Integration test — full end-to-end flow

**Files:**
- Create: `tests/integration/test_poller_end_to_end.py`
- Create: `tests/fixtures/docs_v1/index.html`, `tests/fixtures/docs_v2/index.html`

- [ ] **Step 1: Create fixtures**

Create `tests/fixtures/docs_v1/index.html`:

```html
<!doctype html><html><head><title>Engine Install</title></head>
<body><main><h1>Engine Install</h1><p>Version 1 instructions for installing the engine on Linux.</p></main></body></html>
```

Create `tests/fixtures/docs_v2/index.html`:

```html
<!doctype html><html><head><title>Engine Install</title></head>
<body><main><h1>Engine Install</h1><p>Version 2 updated instructions, now covering systemd integration on Linux.</p></main></body></html>
```

- [ ] **Step 2: Write the test**

Create `tests/integration/test_poller_end_to_end.py`:

```python
"""End-to-end: serve a fixture sitemap from a local httpd, run the poller,
modify content, run again, assert the supersede flow worked top to bottom."""
import asyncio
import http.server
import json
import socketserver
import sqlite3
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures"


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def httpd(tmp_path):
    """Serve docs_v1 then swap to docs_v2 between calls."""
    serve_root = tmp_path / "site"
    serve_root.mkdir()
    (serve_root / "engine").mkdir()
    (serve_root / "engine" / "install").mkdir()
    (serve_root / "engine" / "install" / "linux").mkdir()
    (serve_root / "engine" / "install" / "linux" / "index.html").write_bytes(
        (FIX / "docs_v1" / "index.html").read_bytes()
    )
    (serve_root / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>http://127.0.0.1:{port}/engine/install/linux/</loc></url>'
        '</urlset>'
    )

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(serve_root), **kw)
        def log_message(self, *a, **kw): pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    # Patch sitemap with the actual port
    sm = (serve_root / "sitemap.xml").read_text().replace("{port}", str(port))
    (serve_root / "sitemap.xml").write_text(sm)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield {"port": port, "root": serve_root}
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_full_supersede_flow(tmp_path, monkeypatch, httpd):
    """1) ingest revision 1, 2) modify file, 3) ingest revision 2, 4) verify chain."""
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn)

    from engram.common import config as cfg_mod
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)

    from engram.poller.poller import poll_one

    sitemap_url = f"http://127.0.0.1:{httpd['port']}/sitemap.xml"
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, config, schedule, source_tier) "
        "VALUES ('test', 't', 'sitemap', ?, ?, '7d', 'vendor-doc')",
        (sitemap_url, json.dumps({})),
    )
    src = dict(conn.execute("SELECT * FROM sources WHERE id='test'").fetchone())

    # First poll: should ingest one new entry
    counts1 = await poll_one(conn, src)
    assert counts1["ingested"] == 1, f"got: {counts1}"

    rev1 = conn.execute(
        "SELECT hash, revision, is_current FROM content "
        "WHERE source_id='test' ORDER BY revision DESC"
    ).fetchall()
    assert len(rev1) == 1
    assert rev1[0]["revision"] == 1
    assert rev1[0]["is_current"] == 1

    # Modify the served file
    target = httpd["root"] / "engine" / "install" / "linux" / "index.html"
    target.write_bytes((FIX / "docs_v2" / "index.html").read_bytes())

    # Force re-poll (clear cursor so etag doesn't trip us up)
    conn.execute("UPDATE sources SET cursor=NULL WHERE id='test'")
    src = dict(conn.execute("SELECT * FROM sources WHERE id='test'").fetchone())
    counts2 = await poll_one(conn, src)
    assert counts2["superseded"] == 1, f"got: {counts2}"

    rev2 = conn.execute(
        "SELECT hash, revision, is_current, superseded_by FROM content "
        "WHERE source_id='test' ORDER BY revision"
    ).fetchall()
    assert len(rev2) == 2
    assert rev2[0]["revision"] == 1 and rev2[0]["is_current"] == 0
    assert rev2[0]["superseded_by"] == rev2[1]["hash"]
    assert rev2[1]["revision"] == 2 and rev2[1]["is_current"] == 1
```

- [ ] **Step 3: Run the integration test**

```bash
/home/nemy/.engram/.venv/bin/pytest tests/integration/test_poller_end_to_end.py -v -s
```

Expected: 1 passed.

- [ ] **Step 4: Run the full test suite**

```bash
/home/nemy/.engram/.venv/bin/pytest -q
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_poller_end_to_end.py tests/fixtures/docs_v1/index.html tests/fixtures/docs_v2/index.html
git commit -m "tests: integration coverage for full sitemap-supersede flow"
```

---

## Task 13: CLI — `bin/eos-source`

**Files:**
- Create: `bin/eos-source`

- [ ] **Step 1: Create the CLI wrapper**

Create `bin/eos-source` (executable shell script that shells into the venv's Python):

```bash
#!/usr/bin/env bash
set -e
exec "${ENGRAM_VENV:-$HOME/.engram/.venv}/bin/python" -m engram.cli.eos_source "$@"
```

- [ ] **Step 2: Create the CLI module**

Create `src/engram/cli/__init__.py`:

```python
"""CLI commands shipped as bin/eos-* scripts."""
```

Create `src/engram/cli/eos_source.py`:

```python
"""CLI mirror of the sources.* MCP tools, for shell use."""
from __future__ import annotations

import argparse
import json
import sys

from ..common.db import get_connection
from ..mcp_server.tools.sources import register


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eos-source")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a new source")
    a.add_argument("id")
    a.add_argument("--name", required=True)
    a.add_argument("--adapter", choices=["sitemap", "github-repo"], required=True)
    a.add_argument("--url", required=True)
    a.add_argument("--include", action="append", default=[])
    a.add_argument("--exclude", action="append", default=[])
    a.add_argument("--schedule")
    a.add_argument("--source-tier")
    a.add_argument("--paused", action="store_true")

    sub.add_parser("list", help="list sources").add_argument(
        "--with-errors", action="store_true",
    )

    g = sub.add_parser("get", help="show one source")
    g.add_argument("id")

    rm = sub.add_parser("remove", help="delete a source")
    rm.add_argument("id")

    fn = sub.add_parser("fetch-now", help="force immediate poll")
    fn.add_argument("id")

    s = sub.add_parser("set", help="update fields")
    s.add_argument("id")
    s.add_argument("--paused", choices=["true", "false"])
    s.add_argument("--schedule")
    s.add_argument("--source-tier")
    s.add_argument("--include", action="append")
    s.add_argument("--exclude", action="append")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn = get_connection()
    tools = register(conn)

    if args.cmd == "add":
        cfg = {}
        if args.include:
            cfg["include"] = args.include
        if args.exclude:
            cfg["exclude"] = args.exclude
        out = tools["sources.add"]["handler"]({
            "id": args.id, "name": args.name, "adapter": args.adapter,
            "url": args.url, "config": cfg,
            "schedule": args.schedule,
            "source_tier": args.source_tier,
            "paused": args.paused,
        })
    elif args.cmd == "list":
        out = tools["sources.list"]["handler"]({"with_errors": args.with_errors})
    elif args.cmd == "get":
        out = tools["sources.get"]["handler"]({"id": args.id})
    elif args.cmd == "remove":
        out = tools["sources.remove"]["handler"]({"id": args.id})
    elif args.cmd == "fetch-now":
        out = tools["sources.fetch_now"]["handler"]({"id": args.id})
    elif args.cmd == "set":
        body: dict = {"id": args.id}
        if args.paused is not None:
            body["paused"] = args.paused == "true"
        if args.schedule:
            body["schedule"] = args.schedule
        if args.source_tier:
            body["source_tier"] = args.source_tier
        if args.include or args.exclude:
            body["config"] = {}
            if args.include: body["config"]["include"] = args.include
            if args.exclude: body["config"]["exclude"] = args.exclude
        out = tools["sources.set"]["handler"](body)
    else:
        return 2

    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Make the script executable and smoke-test**

```bash
chmod +x bin/eos-source
./bin/eos-source list
```

Expected: `[]` (no sources yet) printed as JSON.

- [ ] **Step 4: Round-trip test**

```bash
./bin/eos-source add demo \
  --name "Demo" --adapter sitemap --url "https://example.com/sitemap.xml" \
  --include '*/engine/*' --exclude '*/macos/*'
./bin/eos-source list
./bin/eos-source remove demo
```

Expected: add returns the id; list shows the source; remove returns `{"removed": true}`.

- [ ] **Step 5: Commit**

```bash
git add bin/eos-source src/engram/cli/__init__.py src/engram/cli/eos_source.py
git commit -m "cli: bin/eos-source mirror of sources.* MCP tools"
```

---

## Task 14: Daily-digest playbook — Source curation section

**Files:**
- Modify: `playbooks/scratch/daily-digest.ipynb`

This task modifies a notebook. Steps below describe the cell-edit; engineer applies via `papermill`-style param injection or by hand-editing the JSON.

- [ ] **Step 1: Locate the section that lists ingestions and add a new section after it**

Open `playbooks/scratch/daily-digest.ipynb`. Find the cell that produces the "## Ingested" markdown section. After it, add a new cell that emits the "## Source curation" section.

Add this code cell (Python, agentos kernel):

```python
# Source curation section
src_rows = conn.execute(
    "SELECT id, name, last_polled_at, last_success_at, paused, error_count, last_error, next_poll_at "
    "FROM sources ORDER BY id"
).fetchall()

# Tally per-source events in window
since_id = window_start_event_id  # already defined earlier in the notebook
poll_events = conn.execute(
    "SELECT payload FROM events WHERE id > ? AND type='source_polled'",
    (since_id,),
).fetchall()

counts = {}
for r in poll_events:
    p = json.loads(r["payload"])
    sid = p.get("source_id")
    if not sid: continue
    c = counts.setdefault(sid, {"ingested": 0, "superseded": 0, "errors": 0, "candidates": 0})
    c["ingested"]   += p.get("ingested", 0)
    c["superseded"] += p.get("superseded", 0)
    c["errors"]     += p.get("errors", 0)
    c["candidates"] += p.get("candidates_seen", 0)

src_md = ["## Source curation"] if src_rows else []
for s in src_rows:
    icon = "⛔" if s["paused"] else ("⚠" if s["error_count"] else "✓")
    c = counts.get(s["id"], {})
    fragment = (
        f"{c.get('ingested', 0)} new, {c.get('superseded', 0)} superseded"
        if (c.get('ingested') or c.get('superseded'))
        else "0 changes"
    )
    err = f" — {s['last_error']}" if s["error_count"] and s["last_error"] else ""
    src_md.append(
        f"- {icon} `{s['id']}`: {fragment} (last poll {s['last_polled_at'] or 'never'}, "
        f"next {s['next_poll_at'] or 'unscheduled'}){err}"
    )

if not src_rows:
    src_md = ["## Source curation", "_no sources configured_"]

print("\n".join(src_md))
```

Append the printed block to whatever string accumulates the digest body (engineer: follow the same pattern as the other sections in this notebook).

- [ ] **Step 2: Smoke-test the playbook with no sources**

Through MCP (in a Claude Code session, or via the CLI): `playbook.run` with name `daily-digest`.
Expected: completes exit 0; the rendered digest contains a `## Source curation\n_no sources configured_` line.

- [ ] **Step 3: Smoke-test with a source**

```bash
./bin/eos-source add demo \
  --name "Demo" --adapter sitemap --url "https://example.com/sitemap.xml"
# (run daily-digest playbook again)
./bin/eos-source remove demo
```

Expected: digest now lists `demo` under "Source curation".

- [ ] **Step 4: Commit**

```bash
git add playbooks/scratch/daily-digest.ipynb
git commit -m "playbook: daily-digest — Source curation section"
```

---

## Task 15: Manual smoke test against live Docker docs

**Files:** none (operational verification)

This task does no code changes — it confirms the feature works end-to-end against the real target.

- [ ] **Step 1: Reconnect MCP so the new `sources.*` namespace is available**

In Claude Code, run `/mcp` to reconnect the `engram` server. Verify `mcp__engram__sources_*` tools now appear.

- [ ] **Step 2: Add the Docker docs source**

Via MCP (or the CLI):

```bash
./bin/eos-source add docker-docs-linux \
  --name "Docker Docs (Linux)" \
  --adapter sitemap \
  --url "https://docs.docker.com/sitemap.xml" \
  --include '*/engine/*' \
  --include '*/desktop/install/linux*' \
  --exclude '*/manuals/desktop/install/macos*' \
  --exclude '*/manuals/desktop/install/windows*' \
  --schedule 7d
```

- [ ] **Step 3: Trigger an immediate poll**

```bash
./bin/eos-source fetch-now docker-docs-linux
```

Wait ~30 seconds for the poller daemon to pick it up and complete (depends on size).

- [ ] **Step 4: Verify state and content**

```bash
./bin/eos-source get docker-docs-linux
```

Expected: `error_count == 0`, `last_success_at` populated, `cursor` populated with etags.

```bash
/home/nemy/.engram/.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('/home/nemy/.engram/db.sqlite')
n = c.execute(\"SELECT count(*) FROM content WHERE source_id='docker-docs-linux' AND is_current=1\").fetchone()[0]
print(f'{n} live entries')
"
ls /home/nemy/.engram/vault/030-research/ | grep docker | head
```

Expected: ≥30 live entries; vault directory contains rendered Docker docs markdown.

- [ ] **Step 5: Document the result and commit**

Append a short note to `docs/superpowers/specs/2026-05-06-source-curation-design.md` (e.g. an "Outcome" section) with the actual page count and any anomalies. Commit:

```bash
git add docs/superpowers/specs/2026-05-06-source-curation-design.md
git commit -m "spec: record live Docker-docs smoke-test outcome"
```

---

## Final sweep

- [ ] **Run the full test suite**

```bash
/home/nemy/.engram/.venv/bin/pytest -q
```

Expected: all green, no warnings about missing fixtures or async-mode.

- [ ] **Verify the running daemons**

```bash
systemctl --user status engram-poller engram-projector engram-reactor engram-watcher --no-pager | grep -E '^●|Active'
```

Expected: 4 active running services.

- [ ] **Confirm the spec was updated with the Docker-docs outcome**

```bash
git log --oneline | head -20
```

Expected: a sequence of commits matching the task headings, ending with the spec outcome update.

- [ ] **Tag pre-release** (optional)

If the smoke test passed and you want a checkpoint before any further work:

```bash
git tag -a v0.2.0-alpha.1 -m "v0.2.0-alpha.1 — source curation (sitemap + github-repo adapters)"
git push origin main
git push origin v0.2.0-alpha.1
```

---

## Self-review checks (already applied to this plan)

**Spec coverage:** every requirement in the spec maps to a task above —
- sources table & content rev columns → Task 1
- schedule grammar → Task 2
- glob filter → Task 3
- supersede dedup outcome → Task 4
- URL-derived stable vault path → Task 5
- projector handles superseded → Task 6
- sitemap adapter → Task 7
- github-repo adapter → Task 8
- poller main loop + error/circuit-break → Task 9
- daemon entrypoint + systemd unit + console script → Task 10
- sources.* MCP namespace (six tools) → Task 11
- integration test → Task 12
- bin/eos-source CLI mirror → Task 13
- daily-digest playbook section → Task 14
- worked-example smoke test → Task 15

**No placeholders:** every code step contains the actual code. Every command step contains the actual command + expected output.

**Type consistency:** `Outcome` literal, `GateResult` shape, `Candidate` dataclass fields, `Adapter` Protocol method names, `register(conn) -> dict[str, dict]` return type, and the column names introduced in Task 1 are referenced consistently across Tasks 4, 5, 6, 9, 11.
