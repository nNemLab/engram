"""Session priming (#42): assemble active goals + recent high-confidence entries
into a context block. Deterministic; no model calls. Used by the session.prime MCP
tool and (Phase 2) the daemon /prime endpoint.

cwd-scoped priming (#181): when the caller passes the session's working directory,
entries whose `source_url` points inside that directory tree (project-local
knowledge) are surfaced first, then the remaining slots are backfilled with the
globally highest-confidence entries. With no `cwd` the selection reduces exactly
to the prior global "top high-confidence" ordering, so the behaviour is
backward-compatible.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def _normalize_cwd(cwd: str | None) -> str | None:
    """Canonicalize the working dir for prefix matching, or None if unusable.

    Trailing slashes are stripped so `/a/b/` and `/a/b` match identically. An
    empty/whitespace string (or the filesystem root, which would scope to
    "everything") is treated as absent so priming stays global.
    """
    if not cwd:
        return None
    cwd = cwd.strip()
    if not cwd:
        return None
    cwd = cwd.rstrip("/")
    if not cwd:  # was "/" (or all slashes) -- too broad to scope on
        return None
    return cwd


def _like_escape(text: str) -> str:
    r"""Escape LIKE wildcards so a path is matched literally (ESCAPE '\').

    Filesystem paths can legitimately contain `%` and `_`, which are LIKE
    wildcards; left unescaped they would over-match. The escape char itself
    (`\`) is escaped first so it isn't doubly-interpreted.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _local_entries(conn: sqlite3.Connection, cwd: str, limit: int) -> list[sqlite3.Row]:
    """Entries whose source_url is the cwd or a path beneath it.

    Matches both bare paths (`/proj/x`) and `file://`-scheme URLs
    (`file:///proj/x`, as written by playbook runs), accepting the directory
    itself and any descendant (`<dir>/...`). Ordered by the same
    confidence/recency key the global tier uses.
    """
    bases = [cwd, f"file://{cwd}"]
    clauses: list[str] = []
    params: list[str] = []
    for base in bases:
        clauses.append("source_url = ?")
        params.append(base)
        clauses.append("source_url LIKE ? ESCAPE '\\'")
        params.append(_like_escape(base) + "/%")
    where = " OR ".join(clauses)
    params.append(str(limit))
    return conn.execute(
        f"SELECT title, hash FROM content WHERE tombstoned=0 AND ({where}) "
        f"ORDER BY confidence DESC, fetched_at DESC LIMIT ?",
        params,
    ).fetchall()


def _global_entries(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT title, hash FROM content WHERE tombstoned=0 "
        "ORDER BY confidence DESC, fetched_at DESC LIMIT ?", (limit,),
    ).fetchall()


def _select_entries(conn: sqlite3.Connection, cwd: str | None,
                    max_entries: int) -> list[sqlite3.Row]:
    """Pick the entries to prime, project-local first when a cwd is given.

    Local entries fill the slots first (regardless of their global confidence
    rank, so a project's own knowledge is never crowded out); any remaining
    slots are backfilled with the globally highest-confidence entries. With no
    cwd this is just the global ordering -- the original behaviour.
    """
    if cwd is None:
        return _global_entries(conn, max_entries)
    selected = _local_entries(conn, cwd, max_entries)
    if len(selected) >= max_entries:
        return selected[:max_entries]
    seen = {r["hash"] for r in selected}
    for r in _global_entries(conn, max_entries):
        if r["hash"] not in seen:
            selected.append(r)
            if len(selected) >= max_entries:
                break
    return selected


def prime(conn: sqlite3.Connection, *, cwd: str | None = None,
          token_budget: int = 1500, max_goals: int = 5, max_entries: int = 5) -> dict[str, Any]:
    cwd = _normalize_cwd(cwd)
    goals = conn.execute(
        "SELECT text, priority FROM goals WHERE status='active' "
        "ORDER BY priority DESC, updated_at DESC LIMIT ?", (max_goals,),
    ).fetchall()
    entries = _select_entries(conn, cwd, max_entries)
    if not goals and not entries:
        return {"block": ""}
    lines = ["## Engram session priming"]
    if goals:
        lines.append("**Active goals:**")
        lines += [f"- {g['text']}" for g in goals]
    if entries:
        lines.append("**Recent high-confidence knowledge:**")
        lines += [f"- {e['title'] or '(untitled)'} `[{e['hash'][:12]}]`" for e in entries]
    block = "\n".join(lines)
    # crude budget guard
    if len(block) // 4 > token_budget:
        block = block[: token_budget * 4]
    return {"block": block}
