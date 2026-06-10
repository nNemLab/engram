"""One-shot vault → log reconciliation.

Use case: the watcher missed edits (was down, system rebooted mid-edit, you
synced the vault from another device). Walks the vault, compares each file
against vault_state.rendered_body, and emits vault_edit events for any
divergence — making the on-disk version authoritative.

Files in the vault that have NO vault_state row are treated as inbox drops
(kind=kb, actor=human) and pushed through the dedup gate.

Usage:
    python -m scripts.reconcile_vault [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from engram import dedup
from engram import log as event_log
from engram.common.config import load_config
from engram.common.db import get_connection
from engram.watcher.differ import unified_diff


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    vault = cfg.paths.vault
    if not vault.exists():
        logging.error("vault does not exist: %s", vault)
        return 2

    conn = get_connection()
    rows = {r["vault_path"]: r for r in conn.execute(
        "SELECT vault_path, content_hash, rendered_body FROM vault_state"
    )}

    edited = 0
    new_inbox = 0

    for path in vault.rglob("*.md"):
        try:
            rel = str(path.relative_to(vault))
        except ValueError:
            continue
        if rel.startswith(".obsidian/") or rel.startswith(".trash/"):
            continue

        body = path.read_text()
        row = rows.get(rel)
        if row is None:
            logging.info("inbox: %s", rel)
            new_inbox += 1
            if not args.dry_run:
                dedup.gate(
                    conn, body=body, title=path.stem, source_tier="manual",
                    confidence=0.7, kind="kb", actor="human",
                )
            continue
        if body == row["rendered_body"]:
            continue
        logging.info("edited: %s (hash=%s)", rel, row["content_hash"])
        edited += 1
        if not args.dry_run:
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            diff = unified_diff(row["rendered_body"], body, rel)
            conn.execute(
                "UPDATE content SET body = ?, updated_at = ? WHERE hash = ?",
                (body, now, row["content_hash"]),
            )
            conn.execute(
                "UPDATE vault_state SET rendered_body = ?, rendered_at = ? WHERE vault_path = ?",
                (body, now, rel),
            )
            event_log.append(
                conn, "vault_edit",
                {"path": rel, "hash": row["content_hash"], "diff": diff[:8000],
                 "source": "reconcile"},
                actor="human",
            )

    print(f"\nresult: edited={edited} inbox={new_inbox} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
