"""Render content rows into vault markdown. One renderer per kind."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from urllib.parse import urlparse

import yaml


def _frontmatter(d: dict) -> str:
    return "---\n" + yaml.safe_dump(d, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n"


def _safe_slug(s: str | None, fallback: str) -> str:
    base = (s or fallback).lower().strip()
    out = []
    for ch in base:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("-")
    return "".join(out).strip("-")[:80] or fallback


def render_kb(row: sqlite3.Row, kind_dir: str) -> tuple[str, str]:
    fm = {
        "engram_hash": row["hash"],
        "kind": row["kind"],
        "title": row["title"],
        "source_url": row["source_url"],
        "source_tier": row["source_tier"],
        "fetched_at": row["fetched_at"],
        "confidence": row["confidence"],
        "ttl_days": row["ttl_days"],
    }
    try:
        sid = row["source_id"]
    except (KeyError, IndexError):
        sid = None
    if row["source_url"] and sid:
        url_path = urlparse(row["source_url"]).path.rstrip("/")
        tail = url_path.rsplit("/", 1)[-1] or "index"
        slug = _safe_slug(tail, row["hash"][:12])
        suffix = sid[:8] or row["hash"][:8]
        path = f"{kind_dir}/{slug}-{suffix}.md"
    else:
        slug = _safe_slug(row["title"], row["hash"][:12])
        path = f"{kind_dir}/{slug}-{row['hash'][:8]}.md"
    body = _frontmatter(fm) + (row["body"] or "")
    return path, body


# Kind-specific renderers can override layout. Default falls through to render_kb.
RENDERERS: dict[str, Callable[[sqlite3.Row, str], tuple[str, str]]] = {
    "kb":               render_kb,
    "episode":          render_kb,
    "entity":           render_kb,
    "research":         render_kb,
    "playbook-summary": render_kb,
}
