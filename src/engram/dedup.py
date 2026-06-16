"""Dedup gate. Every content write goes through here.

Outcomes:
  - 'new'         : content is novel, inserted, ingested event emitted
  - 'exact_dup'   : SHA-256 collision with a live row, no-op (existing hash returned)
  - 'resurrected' : SHA-256 collision with a TOMBSTONED row — un-tombstoned so it
                    resurfaces and is re-embedded/re-projected (#168)
  - 'near_dup'    : embedding cosine > threshold, merged into existing entry
  - 'superseded'  : same source_url, different bytes — old row marked is_current=0, new row inserted with bumped revision

The decision + write + event for each outcome runs inside ONE transaction and the
outcome is derived from the write itself (row count / unique-index conflict), not
a prior separate read, so concurrent identical writes can't both "win" (#152, #153).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from . import log as event_log
from .common.config import load_config
from .common.db import transaction
from .rag._cosine import l2_to_cosine

Outcome = Literal[
    "new", "exact_dup", "resurrected", "near_dup", "superseded", "supersede_blocked"
]

# A genuinely new write retries this many times if a concurrent writer wins the
# race for the same source_url (unique-index conflict) or invalidates our WAL
# read snapshot. Each retry re-reads fresh state, so the loser resolves as a
# supersede / exact_dup instead of crashing (#153). Bounded so a real, persistent
# constraint error still surfaces rather than spinning forever.
_MAX_GATE_RETRIES = 8

# Test hook: invoked (when set) each time `gate` retries after losing a write
# race, so tests can prove the retry path actually fired. None in production.
_on_gate_retry: Callable[[], None] | None = None


class _SupersedeAbort(RuntimeError):
    """Internal: roll back a resolve_supersede transaction and surface an error dict."""


class _GateRetry(RuntimeError):
    """Internal: roll back this `_gate_once` attempt and re-resolve from fresh state.

    Raised when our content hash already exists by insert time (a concurrent writer
    committed it after this transaction's in-snapshot reads). The in-snapshot
    decision is then stale -- the pre-existing row may belong to a DIFFERENT
    source_url -- so we must reclassify (exact_dup / resurrection / a clean
    supersede on the now-current row) rather than commit a half-applied swap.
    """


def _is_one_current_violation(exc: sqlite3.IntegrityError) -> bool:
    """True only for the one-current-per-source_url backstop.

    `idx_content_one_current_per_url` is the sole UNIQUE constraint on
    `content.source_url`, which SQLite reports as
    ``UNIQUE constraint failed: content.source_url``. Matching that exact text
    keeps unrelated UNIQUE failures from spinning the whole retry budget -- they
    propagate immediately.
    """
    return "unique constraint failed: content.source_url" in str(exc).lower()


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    """True for a WAL busy / stale-snapshot error from a concurrent writer."""
    return "locked" in str(exc).lower()


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
    """Return (hash, similarity) of nearest current neighbor if cosine similarity >= threshold.
    sqlite-vec returns L2 distance by default — converted to cosine via normalised vectors.
    """
    # Pull a small nearest-neighbor pool first, then filter to live/current rows.
    # LIMIT 1 can miss a valid candidate when the top vector belongs to a stale row.
    cur = conn.execute(
        "SELECT content_hash, distance FROM embeddings "
        "WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT 10",
        (embedding,),
    )
    for row in cur.fetchall():
        live = conn.execute(
            "SELECT 1 FROM content WHERE hash = ? AND tombstoned = 0 AND is_current = 1",
            (row["content_hash"],),
        ).fetchone()
        if not live:
            continue
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
) -> bool:
    """INSERT the content row if its hash is new; return True iff a row was inserted.

    Uses INSERT OR IGNORE so a primary-key (hash) collision is a silent no-op. The
    caller DERIVES its outcome from the returned flag (the row count) rather than
    from a prior, separate `find_exact` read, closing the TOCTOU where two
    concurrent identical writes both passed the check and both emitted `ingested`
    (#153): the writer that lands second sees `False` and treats it as an exact dup.
    """
    h = content_hash(body)
    cur = conn.execute(
        """INSERT OR IGNORE INTO content
           (hash, body, title, source_url, source_tier, fetched_at, confidence, ttl_days,
            kind, revision, is_current, source_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (h, body, title, source_url, source_tier, fetched_at, confidence, ttl_days,
         kind, revision, is_current, source_id),
    )
    return cur.rowcount == 1


def _record_supersede_contradiction(
    conn: sqlite3.Connection, human_hash: str, upstream_hash: str,
    source_url: str | None, actor: str, correlation_id: str | None,
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
        actor=actor, correlation_id=correlation_id,
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
        # upstream row behaves like any normal sourced row from here on. The whole
        # sequence is atomic (#83): a failure mid-flip must not leave zero or two
        # current rows for the source_url. Demote-before-promote also keeps at most
        # one is_current row at each step, satisfying the one-current-per-url index.
        try:
            with transaction(conn):
                # #153: confirm the upstream revision exists AND belongs to the SAME
                # source_url as the human row BEFORE touching anything. A missing
                # upstream would trip the superseded_by FK and leave the source_url
                # with zero current rows; an upstream owned by a DIFFERENT source_url
                # would (after demoting the human row) promote FOREIGN content and
                # leave the human's source_url at ZERO current rows. The rowcount==1
                # guard alone can't catch this -- it only confirms one row changed,
                # not that it's the right source_url's row. Abort cleanly so ROLLBACK
                # keeps the human row current instead.
                human_row = conn.execute(
                    "SELECT source_url FROM content WHERE hash = ?", (human_hash,)
                ).fetchone()
                if human_row is None:
                    raise _SupersedeAbort(
                        f"cannot accept upstream: human row {human_hash} no longer "
                        "exists; nothing to supersede"
                    )
                human_url = human_row["source_url"]
                upstream_row = conn.execute(
                    "SELECT source_url, revision FROM content WHERE hash = ?", (upstream_hash,)
                ).fetchone()
                if upstream_row is None:
                    raise _SupersedeAbort(
                        f"cannot accept upstream: revision {upstream_hash} no longer "
                        "exists, so promoting it would leave no current row for the "
                        "source_url; keeping the human row current"
                    )
                if upstream_row["source_url"] != human_url:
                    raise _SupersedeAbort(
                        f"cannot accept upstream: revision {upstream_hash} belongs to "
                        f"source_url {upstream_row['source_url']!r}, not the human row's "
                        f"{human_url!r}; refusing to promote foreign content under the "
                        "wrong source_url"
                    )
                conn.execute(
                    "UPDATE content SET is_current = 0, superseded_by = ? WHERE hash = ?",
                    (upstream_hash, human_hash),
                )
                promoted = conn.execute(
                    "UPDATE content SET is_current = 1, protected = 0, tombstoned = 0 "
                    "WHERE hash = ?",
                    (upstream_hash,),
                )
                # Defensive: the existence check above already guarantees a match,
                # but assert the promote affected exactly one row before committing
                # so we never demote the human row without a replacement current row.
                if promoted.rowcount != 1:
                    raise _SupersedeAbort(
                        f"cannot accept upstream: promoting revision {upstream_hash} "
                        "affected no row; keeping the human row current"
                    )
                # MINIMUM BAR (#153): the human's source_url must end with EXACTLY ONE
                # current, non-tombstoned row -- never zero, never two. (NULL
                # source_url is exempt: the one-current index does not constrain it
                # and `source_url = NULL` never matches.)
                if human_url is not None:
                    n_current = conn.execute(
                        "SELECT COUNT(*) FROM content "
                        "WHERE source_url = ? AND is_current = 1 AND tombstoned = 0",
                        (human_url,),
                    ).fetchone()[0]
                    if n_current != 1:
                        raise _SupersedeAbort(
                            f"accept upstream would leave source_url {human_url!r} with "
                            f"{n_current} current rows; aborting"
                        )
                conn.execute(
                    "UPDATE contradictions SET resolved = 1, resolution = 'kept_b' WHERE id = ?",
                    (cid,),
                )
                # A `superseded` event re-projects the vault file (human bytes -> upstream).
                event_log.append(
                    conn, "superseded",
                    {
                        "hash_old": human_hash,
                        "hash_new": upstream_hash,
                        "source_url": upstream_row["source_url"],
                        "revision": upstream_row["revision"],
                        "reason": "resolve_accept_upstream",
                    },
                    actor=actor,
                )
        except _SupersedeAbort as exc:
            return {"error": str(exc)}
        return {"outcome": "accept_upstream", "hash": upstream_hash,
                "contradiction_id": cid, "resolution": "kept_b"}

    # keep_mine: human row is already current+protected; nothing to change there.
    with transaction(conn):
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
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
        event_log.append(
            conn, "contradiction_resolved",
            {"hash_a": human_hash, "hash_b": upstream_hash, "resolution": "kept_a",
             "tombstoned_upstream": bool(tombstone_upstream)},
            actor=actor,
        )
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

    The whole decision (exact-dup / resurrect / supersede / near-dup / new) runs
    inside ONE transaction per attempt (`_gate_once`) and the outcome is derived
    from the write itself, so two connections writing concurrently can't both emit
    `ingested` or both leave a current row (#152, #153). If a concurrent writer
    wins the race for the same source_url -- tripping the one-current-per-url
    unique index, or invalidating this connection's WAL read snapshot -- the loser
    rolls back and retries against fresh state instead of crashing.
    """
    if not body or not body.strip():
        raise ValueError("body must be a non-empty string")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError("confidence must be between 0.0 and 1.0")
    if ttl_days is not None and ttl_days < 0:
        raise ValueError("ttl_days must be >= 0 when provided")

    cfg = load_config()
    h = content_hash(body)
    for attempt in range(_MAX_GATE_RETRIES):
        try:
            return _gate_once(
                conn, cfg=cfg, h=h, body=body, title=title, source_url=source_url,
                source_tier=source_tier, confidence=confidence, ttl_days=ttl_days,
                kind=kind, actor=actor, correlation_id=correlation_id,
                embedding=embedding, source_id=source_id,
            )
        except _GateRetry:
            # Our in-snapshot decision went stale (the hash now exists, possibly for
            # another source_url) or the invariant guard tripped; `transaction`
            # already rolled this attempt back. Re-read fresh state and reclassify.
            if attempt < _MAX_GATE_RETRIES - 1:
                if _on_gate_retry is not None:
                    _on_gate_retry()
                continue
            raise RuntimeError(
                "gate: content write could not be resolved after "
                f"{_MAX_GATE_RETRIES} contention retries"
            ) from None
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            retryable = (
                isinstance(exc, sqlite3.IntegrityError) and _is_one_current_violation(exc)
            ) or (
                isinstance(exc, sqlite3.OperationalError) and _is_locked_error(exc)
            )
            if retryable and attempt < _MAX_GATE_RETRIES - 1:
                # A concurrent writer beat us to the one-current-per-url backstop or
                # invalidated our WAL snapshot; `transaction` already rolled our
                # attempt back. Loop to re-read the now-current state and resolve as a
                # supersede / exact_dup rather than racing or crashing (#153).
                if _on_gate_retry is not None:
                    _on_gate_retry()
                continue
            raise
    # Unreachable: the final attempt above either returns or re-raises.
    raise AssertionError("gate retry loop exited without a result")


def _gate_once(
    conn: sqlite3.Connection,
    *,
    cfg: Any,
    h: str,
    body: str,
    title: str | None,
    source_url: str | None,
    source_tier: str,
    confidence: float,
    ttl_days: int | None,
    kind: str,
    actor: str,
    correlation_id: str | None,
    embedding: bytes | None,
    source_id: str | None,
) -> GateResult:
    """One transactional attempt of the dedup decision (see `gate`)."""
    with transaction(conn):
        # Exact primary-key (hash) hit, read INSIDE the transaction so the
        # decision and the write that follows can't straddle a concurrent commit.
        existing = conn.execute(
            "SELECT tombstoned, source_url FROM content WHERE hash = ?", (h,)
        ).fetchone()
        if existing is not None:
            if not existing["tombstoned"]:
                # A live row already carries these exact bytes (current or a
                # non-current revision): genuine no-op.
                return GateResult(outcome="exact_dup", hash=h)
            # #168: the bytes match a TOMBSTONED row. The old code let this fall
            # through to INSERT OR IGNORE -- a no-op on the PK -- yet still emitted
            # `ingested` and returned "new", so callers thought it was live while
            # retrieval (which filters tombstoned=0) never surfaced it. Instead,
            # un-tombstone the row so it resurfaces and gets re-embedded/
            # re-projected by the `ingested` consumers, and return a distinct
            # outcome.
            target_is_current = 1
            row_url = existing["source_url"]
            if row_url is not None:
                # Resurrecting to current would create a second current row if the
                # source_url already has one; keep this revision non-current then.
                clash = conn.execute(
                    "SELECT 1 FROM content WHERE source_url = ? AND is_current = 1 "
                    "AND tombstoned = 0 AND hash != ? LIMIT 1",
                    (row_url, h),
                ).fetchone()
                if clash:
                    target_is_current = 0
            conn.execute(
                "UPDATE content SET tombstoned = 0, is_current = ?, confidence = ?, "
                "fetched_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE hash = ?",
                (target_is_current, confidence, h),
            )
            event_log.append(
                conn, "ingested",
                {"hash": h, "title": title, "source_url": row_url, "kind": kind,
                 "source_tier": source_tier, "resurrected": True},
                actor=actor, correlation_id=correlation_id,
            )
            return GateResult(outcome="resurrected", hash=h)

        # Source-URL supersede: a live entry at the same source_url with different
        # bytes makes this write a new revision rather than a fresh ingest.
        if source_url:
            live = conn.execute(
                "SELECT hash, revision, protected FROM content "
                "WHERE source_url = ? AND is_current = 1 AND tombstoned = 0 "
                "ORDER BY revision DESC LIMIT 1",
                (source_url,),
            ).fetchone()
            if live:
                new_revision = live["revision"] + 1
                if live["protected"]:
                    # #37: never silently supersede a human-edited row. Preserve the
                    # upstream change as a non-current revision and raise a
                    # contradiction instead of overwriting the human's edit. No
                    # `superseded` event -> the projector leaves the vault file alone.
                    inserted = insert_content(
                        conn, body=body, title=title, source_url=source_url,
                        source_tier=source_tier, confidence=confidence, ttl_days=ttl_days,
                        kind=kind, revision=new_revision, is_current=0, source_id=source_id,
                    )
                    if not inserted:
                        # The upstream hash already exists -- a concurrent writer
                        # committed it (possibly for a DIFFERENT source_url) after our
                        # in-snapshot reads. Recording a contradiction whose hash_b is
                        # a foreign-source row would later mis-resolve (accept_upstream
                        # promoting foreign content and zeroing THIS source_url). Roll
                        # back and re-resolve instead (#153): the fresh re-read sees
                        # the now-existing body and reclassifies as exact_dup.
                        raise _GateRetry()
                    _record_supersede_contradiction(
                        conn, live["hash"], h, source_url, actor, correlation_id
                    )
                    return GateResult(outcome="supersede_blocked", hash=h)
                # Unprotected supersede. Order matters so we never violate either
                # constraint mid-sequence:
                #   1. insert the new revision NON-current -> its hash now exists for
                #      the superseded_by FK, and the one-current-per-url unique index
                #      never sees two is_current=1 rows;
                #   2. demote the old current row (and point it at the new hash);
                #   3. promote the new revision to current -> exactly one current row.
                # All atomic (#83/#152): a failure ROLLs BACK to one current row.
                inserted = insert_content(
                    conn, body=body, title=title, source_url=source_url,
                    source_tier=source_tier, confidence=confidence, ttl_days=ttl_days,
                    kind=kind, revision=new_revision, is_current=0, source_id=source_id,
                )
                if not inserted:
                    # The hash already exists -- a concurrent writer committed it
                    # (possibly for a DIFFERENT source_url, or NULL) after our
                    # in-snapshot `existing`/`live` reads. Demoting `live` and then
                    # promoting `WHERE hash = h` would promote a row that may not
                    # belong to THIS source_url, leaving it with ZERO current rows.
                    # Roll back and re-resolve instead (#153): the fresh re-read
                    # reclassifies as exact_dup / resurrection / a clean supersede.
                    raise _GateRetry()
                conn.execute(
                    "UPDATE content SET is_current = 0, superseded_by = ? WHERE hash = ?",
                    (h, live["hash"]),
                )
                conn.execute(
                    "UPDATE content SET is_current = 1 WHERE hash = ?", (h,)
                )
                # Invariant guard (#153): this source_url must end with EXACTLY ONE
                # current row -- never zero, never two. If a concurrent change slipped
                # past, roll back and retry rather than commit a broken state.
                n_current = conn.execute(
                    "SELECT COUNT(*) FROM content "
                    "WHERE source_url = ? AND is_current = 1 AND tombstoned = 0",
                    (source_url,),
                ).fetchone()[0]
                if n_current != 1:
                    raise _GateRetry()
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
                kept_hash, sim = near
                event_log.append(
                    conn, "merged",
                    {
                        "hash_kept": kept_hash,
                        "hash_tombstoned": h,
                        "reason": "near_dup_at_write",
                        "similarity": sim,
                    },
                    actor=actor, correlation_id=correlation_id,
                )
                return GateResult(outcome="near_dup", hash=h, merged_into=kept_hash)

        # Genuinely new content. Derive the outcome from the insert itself: if a
        # concurrent identical writer committed first, INSERT OR IGNORE is a no-op
        # (inserted=False) and we classify this as an exact dup, emitting NO second
        # `ingested` (#153).
        inserted = insert_content(
            conn, body=body, title=title, source_url=source_url, source_tier=source_tier,
            confidence=confidence, ttl_days=ttl_days, kind=kind, source_id=source_id,
        )
        if not inserted:
            return GateResult(outcome="exact_dup", hash=h)
        event_log.append(
            conn, "ingested",
            {"hash": h, "title": title, "source_url": source_url, "kind": kind,
             "source_tier": source_tier},
            actor=actor, correlation_id=correlation_id,
        )
        return GateResult(outcome="new", hash=h)
