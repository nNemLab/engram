"""Dedup gate. Every content write goes through here.

Outcomes:
  - 'new'        : content is novel, inserted, ingested event emitted
  - 'exact_dup'  : SHA-256 collision, no-op (existing hash returned)
  - 'near_dup'   : embedding cosine > threshold, merged into existing entry
  - 'contradicts': flagged for human resolution (high overlap + high disagreement signal — stub for now)
  - 'superseded' : same source_url, different bytes — old row marked is_current=0, new row inserted with bumped revision
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from . import log as event_log
from .common.config import load_config
from .rag._cosine import l2_to_cosine

Outcome = Literal["new", "exact_dup", "near_dup", "contradicts", "superseded", "supersede_blocked"]


@dataclass
class GateResult:
    outcome: Outcome
    hash: str
    merged_into: str | None = None


_WS = re.compile(r"\s+")


def normalize(body: str) -> str:
    return _WS.sub(" ", body).strip().lower()


def content_hash(body: str) -> str:
    return hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()


def find_exact(conn: sqlite3.Connection, h: str) -> str | None:
    row = conn.execute(
        "SELECT hash FROM content WHERE hash = ? AND tombstoned = 0",
        (h,),
    ).fetchone()
    return row["hash"] if row else None


def find_near(conn: sqlite3.Connection, embedding: bytes, threshold: float) -> tuple[str, float] | None:
    """Return (hash, similarity) of nearest neighbor if cosine similarity > threshold.
    sqlite-vec returns L2 distance by default — converted to cosine via normalised vectors.
    """
    cur = conn.execute(
        "SELECT content_hash, distance FROM embeddings "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (embedding,),
    )
    row = cur.fetchone()
    if not row:
        return None
    similarity = l2_to_cosine(float(row["distance"]))
    if similarity >= threshold:
        return row["content_hash"], similarity
    return None


def insert_content(
    conn: sqlite3.Connection,
    *,
    body: str,
    title: str | None = None,
    source_url: str | None = None,
    source_tier: str = "agent-derived",
    fetched_at: str | None = None,
    confidence: float = 0.5,
    ttl_days: int | None = None,
    kind: str = "kb",
    revision: int = 1,
    is_current: int = 1,
    source_id: str | None = None,
) -> str:
    h = content_hash(body)
    conn.execute(
        """INSERT OR IGNORE INTO content
           (hash, body, title, source_url, source_tier, fetched_at, confidence, ttl_days,
            kind, revision, is_current, source_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (h, body, title, source_url, source_tier, fetched_at, confidence, ttl_days,
         kind, revision, is_current, source_id),
    )
    return h


def _record_supersede_contradiction(
    conn: sqlite3.Connection, human_hash: str, upstream_hash: str,
    source_url: str | None, actor: str,
) -> None:
    """Upsert a single unresolved contradiction for a protected row (#37).

    Keeps exactly one pending contradiction per protected row: if one already
    exists for this human hash, advance its hash_b to the newest upstream;
    otherwise insert. Emits a `contradicted` event either way.
    """
    existing = conn.execute(
        "SELECT id FROM contradictions WHERE hash_a = ? AND resolved = 0 "
        "ORDER BY id DESC LIMIT 1",
        (human_hash,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE contradictions SET hash_b = ?, detected_by = 'poller', "
            "detected_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (upstream_hash, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO contradictions (hash_a, hash_b, detected_by) VALUES (?, ?, 'poller')",
            (human_hash, upstream_hash),
        )
    event_log.append(
        conn, "contradicted",
        {"hash_a": human_hash, "hash_b": upstream_hash,
         "detected_by": "poller", "source_url": source_url},
        actor=actor,
    )


def resolve_supersede(
    conn: sqlite3.Connection,
    human_hash: str,
    choice: Literal["accept_upstream", "keep_mine"],
    *,
    tombstone_upstream: bool = False,
    actor: str = "human",
) -> dict[str, Any]:
    """Act on a blocked-supersede contradiction raised against a protected row (#54).

    Follow-up to #37: the dedup gate keeps a human-edited (`protected`) row current,
    preserves the rejected upstream change as a non-current revision, and raises one
    unresolved contradiction (hash_a=human, hash_b=upstream). This resolves it.

    choice='accept_upstream':
        Promote the pending upstream revision to current, demote the human row
        (is_current=0, superseded_by=upstream), clear `protected` on the now-current
        upstream row, emit a `superseded` event so the projector re-projects the vault
        file, and mark the contradiction resolved with resolution='kept_b'.

    choice='keep_mine':
        Leave the human row current and protected; mark the contradiction resolved with
        resolution='kept_a'. By default the rejected upstream revision is retained as a
        non-current revision (tombstone_upstream=False) — this is the durable path: a
        re-poll of the same unchanged upstream bytes resolves to `exact_dup` and raises
        no fresh contradiction. Passing tombstone_upstream=True purges the upstream
        revision, but then an identical upstream re-poll re-enters the protected branch
        and re-raises the contradiction every cycle.

    Returns a dict with `outcome` on success or `error` on failure. Idempotent in the
    sense that a second call finds no unresolved contradiction and errors cleanly.
    """
    if choice not in ("accept_upstream", "keep_mine"):
        return {"error": f"invalid choice: {choice!r} (expected 'accept_upstream' or 'keep_mine')"}

    contradiction = conn.execute(
        "SELECT id, hash_a, hash_b FROM contradictions "
        "WHERE hash_a = ? AND resolved = 0 ORDER BY id DESC LIMIT 1",
        (human_hash,),
    ).fetchone()
    if not contradiction:
        return {"error": f"no unresolved supersede contradiction for hash {human_hash}"}

    cid = contradiction["id"]
    upstream_hash = contradiction["hash_b"]

    if choice == "accept_upstream":
        # Promote upstream to current, demote the human row, clear protection so the
        # upstream row behaves like any normal sourced row from here on.
        conn.execute(
            "UPDATE content SET is_current = 0, superseded_by = ? WHERE hash = ?",
            (upstream_hash, human_hash),
        )
        conn.execute(
            "UPDATE content SET is_current = 1, protected = 0, tombstoned = 0 WHERE hash = ?",
            (upstream_hash,),
        )
        conn.execute(
            "UPDATE contradictions SET resolved = 1, resolution = 'kept_b' WHERE id = ?",
            (cid,),
        )
        # A `superseded` event re-projects the vault file (human bytes -> upstream).
        source_url_row = conn.execute(
            "SELECT source_url, revision FROM content WHERE hash = ?", (upstream_hash,)
        ).fetchone()
        event_log.append(
            conn, "superseded",
            {
                "hash_old": human_hash,
                "hash_new": upstream_hash,
                "source_url": source_url_row["source_url"] if source_url_row else None,
                "revision": source_url_row["revision"] if source_url_row else None,
                "reason": "resolve_accept_upstream",
            },
            actor=actor,
        )
        conn.commit()
        return {"outcome": "accept_upstream", "hash": upstream_hash,
                "contradiction_id": cid, "resolution": "kept_b"}

    # keep_mine: human row is already current+protected; nothing to change there.
    conn.execute(
        "UPDATE contradictions SET resolved = 1, resolution = 'kept_a' WHERE id = ?",
        (cid,),
    )
    if tombstone_upstream:
        conn.execute(
            "UPDATE content SET tombstoned = 1 WHERE hash = ?", (upstream_hash,)
        )
        # Drop the embedding too, if the vec table is present (created at runtime).
        try:
            conn.execute("DELETE FROM embeddings WHERE content_hash = ?", (upstream_hash,))
        except sqlite3.OperationalError:
            pass
    event_log.append(
        conn, "contradiction_resolved",
        {"hash_a": human_hash, "hash_b": upstream_hash, "resolution": "kept_a",
         "tombstoned_upstream": bool(tombstone_upstream)},
        actor=actor,
    )
    conn.commit()
    return {"outcome": "keep_mine", "hash": human_hash,
            "contradiction_id": cid, "resolution": "kept_a"}


def gate(
    conn: sqlite3.Connection,
    *,
    body: str,
    title: str | None = None,
    source_url: str | None = None,
    source_tier: str = "agent-derived",
    confidence: float = 0.5,
    ttl_days: int | None = None,
    kind: str = "kb",
    actor: str = "agent",
    correlation_id: str | None = None,
    embedding: bytes | None = None,
    source_id: str | None = None,
) -> GateResult:
    """Single entry point for any content-write into the system.

    Embedding is optional at write-time; if absent, near-dup check is deferred until
    the reactor's embed handler runs, which may then emit a 'merged' event.
    """
    cfg = load_config()
    h = content_hash(body)

    if find_exact(conn, h):
        return GateResult(outcome="exact_dup", hash=h)

    # Source-URL supersede: if a live entry exists at the same source_url with
    # different bytes, treat this write as a new revision rather than a fresh ingest.
    if source_url:
        live = conn.execute(
            "SELECT hash, revision, protected FROM content "
            "WHERE source_url = ? AND is_current = 1 AND tombstoned = 0 "
            "ORDER BY revision DESC LIMIT 1",
            (source_url,),
        ).fetchone()
        if live:
            if live["protected"]:
                # #37: never silently supersede a human-edited row. Preserve the
                # upstream change as a non-current revision and raise a
                # contradiction instead of overwriting the human's edit. No
                # `superseded` event -> the projector leaves the vault file alone.
                # Note: the protected row is never bumped, so repeated upstream
                # changes all land at the same revision number — harmless, since
                # these rows are non-current and tracked via the contradiction.
                new_revision = live["revision"] + 1
                insert_content(
                    conn, body=body, title=title, source_url=source_url,
                    source_tier=source_tier, confidence=confidence, ttl_days=ttl_days,
                    kind=kind, revision=new_revision, is_current=0, source_id=source_id,
                )
                _record_supersede_contradiction(conn, live["hash"], h, source_url, actor)
                return GateResult(outcome="supersede_blocked", hash=h)
            # --- existing (unprotected) supersede logic continues unchanged below ---
            new_revision = live["revision"] + 1
            insert_content(
                conn, body=body, title=title, source_url=source_url,
                source_tier=source_tier, confidence=confidence, ttl_days=ttl_days,
                kind=kind, revision=new_revision, is_current=1, source_id=source_id,
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
        near = find_near(conn, embedding, cfg.rag.near_dup_threshold)
        if near:
            kept_hash, _sim = near
            event_log.append(
                conn, "merged",
                {"hash_kept": kept_hash, "hash_tombstoned": h, "reason": "near_dup_at_write"},
                actor=actor, correlation_id=correlation_id,
            )
            return GateResult(outcome="near_dup", hash=h, merged_into=kept_hash)

    insert_content(
        conn, body=body, title=title, source_url=source_url, source_tier=source_tier,
        confidence=confidence, ttl_days=ttl_days, kind=kind, source_id=source_id,
    )
    event_log.append(
        conn, "ingested",
        {"hash": h, "title": title, "source_url": source_url, "kind": kind, "source_tier": source_tier},
        actor=actor, correlation_id=correlation_id,
    )
    return GateResult(outcome="new", hash=h)
