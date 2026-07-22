#!/usr/bin/env python3
"""
Fortnightly verdict sync + archive.

Syncs Verdict/Reason/Notes from Notion to SQLite for past events,
then archives Notion pages older than 30 days.

Deterministic. No LLM. Designed to run in GitHub Actions on a cron.

Spec: automation.md §Verdict sync and archive.

Usage:
    NOTION_TOKEN=... NOTION_DATABASE_ID=... python3 sync_verdicts.py [--dry-run] [--force]

Cadence: GitHub Actions cron fires every Monday; this script checks
ISO week parity and exits early on off-weeks unless --force is set.
"""
from __future__ import annotations

# Runnable as `python3 pipeline/<name>.py` or importable as a module:
# put the repo root (for erlib) and this package dir (for sibling
# modules) on sys.path before the repo imports below.
import sys as _sys
import pathlib as _pl
_r = _pl.Path(__file__).resolve().parent.parent
for _p in (str(_r / "pipeline"), str(_r)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from erlib.config import DB_PATH, NOTION_DATABASE_ID
from erlib.notion import NotionClient, extract_rich_text, extract_select

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)

BATCH_PAUSE_SEC = 0.35

VERDICT_ALLOWLIST = {"Going", "Maybe", "Not Going", "Undecided"}

SYNC_LOG = SCRIPT_DIR / "sync_log.txt"
ARCHIVE_LOG = SCRIPT_DIR / "archive_log.txt"

ARCHIVE_AFTER_DAYS = 30


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    if "synced_at" not in cols:
        conn.execute("ALTER TABLE candidates ADD COLUMN synced_at TIMESTAMP NULL")
        print("MIGRATE: added column synced_at")
    conn.commit()


class Notion:
    """Page-level interface over erlib.notion.NotionClient.

    The erlib client provides BOUNDED 429 retries: the previous private
    client here retried by unbounded recursion.
    """

    def __init__(self, token: str):
        self._client = NotionClient(token)

    def get_page(self, page_id: str) -> dict:
        return self._client.request("GET", f"/pages/{page_id}")

    def all_pages(self) -> dict[str, dict]:
        """Every live page in the events database via ONE paginated query.

        The query API never returns archived pages: callers fall back to
        get_page() for ids missing from this map (archived or deleted).
        Replaces the old per-candidate GET loop (+0.35s pause each).
        """
        return {
            p["id"]: p
            for p in self._client.paginate(
                "POST", f"/databases/{NOTION_DATABASE_ID}/query", {"page_size": 100}
            )
        }

    def archive_page(self, page_id: str) -> dict:
        return self._client.request("PATCH", f"/pages/{page_id}", {"archived": True})


def _extract_select(props: dict, name: str) -> str | None:
    return extract_select(props.get(name))


def _extract_rich_text(props: dict, name: str) -> str | None:
    return extract_rich_text(props.get(name)).strip() or None


def _log(path: Path, line: str) -> None:
    with open(path, "a") as f:
        f.write(line + "\n")


def _is_sync_week() -> bool:
    _, week, _ = date.today().isocalendar()
    return week % 2 == 0


def sync_verdicts(
    conn: sqlite3.Connection, notion: Notion, *, dry_run: bool
) -> dict[str, int]:
    rows = conn.execute(
        "SELECT id, name, date, notion_page_id, verdict AS old_verdict "
        "FROM candidates "
        "WHERE synced_at IS NULL "
        "  AND COALESCE(end_date, date) < date('now', '-1 day') "
        "  AND notion_page_id IS NOT NULL "
        "ORDER BY id ASC"
    ).fetchall()

    print(f"\nSYNC: {len(rows)} candidates to sync  (dry_run={dry_run})")
    counts = {"synced": 0, "skipped_404": 0, "no_verdict": 0, "errors": 0}

    # One paginated query for every live page; per-page GET only as the
    # fallback for ids the query can't see (archived or deleted pages).
    # A bulk-query failure degrades to the per-page path instead of
    # aborting the sync before any row is processed.
    pages = {}
    if rows:
        try:
            pages = notion.all_pages()
        except RuntimeError as e:
            print(f"  WARN: bulk page query failed ({e}): falling back to per-page GETs")

    for row in rows:
        rid = row["id"]
        name = row["name"]
        page_id = row["notion_page_id"]

        page = pages.get(page_id)
        if page is None:
            try:
                page = notion.get_page(page_id)
            except RuntimeError as e:
                if "404" in str(e):
                    print(f"  [{rid}] NOT_FOUND: skipping (page deleted from Notion)")
                    counts["skipped_404"] += 1
                    continue
                print(f"  [{rid}] ERROR: {e}")
                counts["errors"] += 1
                continue
            time.sleep(BATCH_PAUSE_SEC)

        props = page.get("properties", {})
        verdict = _extract_select(props, "Verdict")
        reason = _extract_select(props, "Reason it failed")
        notes = _extract_rich_text(props, "Notes")

        if verdict and verdict not in VERDICT_ALLOWLIST:
            print(
                f"  [{rid}] WARN: unexpected Verdict value {verdict!r} "
                f"for {name!r}. Skipping this row.",
                file=sys.stderr,
            )
            counts["errors"] += 1
            continue

        if not verdict:
            # Not triaged yet. Do NOT stamp synced_at: the sync query filters
            # on synced_at IS NULL, so stamping here would lock this event out
            # of every future sync even after the user sets a Verdict.
            print(f"  [{rid}] NO_VERDICT: leaving eligible for next sync  {name!r}")
            counts["no_verdict"] += 1
            continue

        now_iso = datetime.now(timezone.utc).isoformat()
        old_verdict = row["old_verdict"]

        if dry_run:
            print(
                f"  [{rid}] DRY_RUN: verdict={verdict!r} reason={reason!r} "
                f"notes={notes!r}  {name!r}"
            )
        else:
            conn.execute(
                "UPDATE candidates "
                "SET verdict = ?, verdict_reason = ?, verdict_notes = ?, synced_at = ? "
                "WHERE id = ?",
                (verdict, reason, notes, now_iso, rid),
            )
            conn.commit()

            log_line = (
                f"{now_iso}  id={rid}  verdict: {old_verdict!r} -> {verdict!r}  "
                f"reason={reason!r}  {name!r}"
            )
            _log(SYNC_LOG, log_line)

            print(f"  [{rid}] SYNCED: {verdict}  {name!r}")

        counts["synced"] += 1

    return counts


def archive_old_pages(
    conn: sqlite3.Connection, notion: Notion, *, dry_run: bool
) -> dict[str, int]:
    rows = conn.execute(
        "SELECT id, name, notion_page_id "
        "FROM candidates "
        "WHERE notion_page_id IS NOT NULL "
        f"  AND COALESCE(end_date, date) < date('now', '-{ARCHIVE_AFTER_DAYS} days') "
        "  AND (notion_status IS NULL OR notion_status = 'active') "
        "ORDER BY id ASC"
    ).fetchall()

    print(f"\nARCHIVE: {len(rows)} pages older than {ARCHIVE_AFTER_DAYS} days  "
          f"(dry_run={dry_run})")
    counts = {"archived": 0, "skipped_404": 0, "already_archived": 0, "errors": 0}

    for row in rows:
        rid = row["id"]
        name = row["name"]
        page_id = row["notion_page_id"]

        if dry_run:
            print(f"  [{rid}] DRY_RUN: would archive  {name!r}")
            counts["archived"] += 1
            continue

        try:
            page = notion.get_page(page_id)
        except RuntimeError as e:
            if "404" in str(e):
                print(f"  [{rid}] NOT_FOUND: marking notion_status='deleted'")
                conn.execute(
                    "UPDATE candidates SET notion_status = 'deleted' WHERE id = ?", (rid,)
                )
                conn.commit()
                counts["skipped_404"] += 1
                continue
            print(f"  [{rid}] ERROR: {e}")
            counts["errors"] += 1
            continue

        if page.get("archived"):
            conn.execute(
                "UPDATE candidates SET notion_status = 'archived' WHERE id = ?", (rid,)
            )
            conn.commit()
            counts["already_archived"] += 1
            continue

        try:
            notion.archive_page(page_id)
        except RuntimeError as e:
            print(f"  [{rid}] ERROR archiving: {e}")
            counts["errors"] += 1
            continue

        conn.execute(
            "UPDATE candidates SET notion_status = 'archived' WHERE id = ?", (rid,)
        )
        conn.commit()

        now_iso = datetime.now(timezone.utc).isoformat()
        _log(ARCHIVE_LOG, f"{now_iso}  id={rid}  archived  {name!r}")
        print(f"  [{rid}] ARCHIVED  {name!r}")
        counts["archived"] += 1
        time.sleep(BATCH_PAUSE_SEC)

    return counts


def run(db_path: Path, *, dry_run: bool, force: bool) -> int:
    if not force and not _is_sync_week():
        print("SKIP: not a sync week (even ISO week). Use --force to override.")
        return 0

    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not token:
        print("NOTION_TOKEN must be set.", file=sys.stderr)
        return 2
    if not db_id:
        print("NOTION_DATABASE_ID must be set.", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    _ensure_columns(conn)

    notion = Notion(token)

    sync_counts = sync_verdicts(conn, notion, dry_run=dry_run)
    print(f"\nSync summary: {sync_counts}")

    if sync_counts["errors"] > 0:
        print("SYNC had errors: skipping archive step.", file=sys.stderr)
        return 1

    archive_counts = archive_old_pages(conn, notion, dry_run=dry_run)
    print(f"Archive summary: {archive_counts}")

    conn.close()
    return 0 if archive_counts["errors"] == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DB_PATH), type=Path)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Notion, log what would change, but don't write to SQLite or archive",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Run even if this isn't a sync week",
    )
    p.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run the offline test suite (tests/test_sync_verdicts.py) and exit",
    )
    args = p.parse_args()
    if args.smoke_test:
        import subprocess

        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "tests" / "test_sync_verdicts.py")]
        ).returncode
    return run(args.db, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
