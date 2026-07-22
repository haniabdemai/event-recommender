#!/usr/bin/env python3
"""
Backfill: add Source Detail property to existing newsletter event pages in Notion.

Sets "Source Detail" (rich_text) to "{sender}, {date}" for newsletter events
that have a source_snapshot linking to processed_emails. Only targets future
events with active Notion pages.

Usage:
    NOTION_TOKEN=ntn_... python3 scripts/backfill_source_detail.py --db event-recommender.db [--dry-run]
    python3 scripts/backfill_source_detail.py --db event-recommender.db --smoke-test
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.write_notion import _format_email_date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.notion import NotionClient, NotionError  # noqa: E402
GMAIL_URL = "https://mail.google.com/mail/u/0/#all/{}"


def find_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Find active future newsletter events with source_snapshot linked to processed_emails."""
    rows = conn.execute("""
        SELECT c.id, c.name, c.notion_page_id, c.source_snapshot,
               pe.sender, pe.email_date
        FROM candidates c
        JOIN processed_emails pe ON c.source_snapshot = pe.gmail_message_id
        WHERE c.notion_status = 'active'
          AND c.source = 'Newsletter'
          AND c.source_snapshot IS NOT NULL
          AND c.notion_page_id IS NOT NULL
          AND COALESCE(c.end_date, c.date) >= date('now')
    """).fetchall()
    return [dict(r) for r in rows]


def _build_patch(row: dict) -> dict:
    """Build the properties dict for a PATCH request."""
    sender = row["sender"]
    formatted_date = _format_email_date(row["email_date"])
    detail = f"{sender} Newsletter, {formatted_date}" if formatted_date else f"{sender} Newsletter"

    return {
        "Source Detail": {"rich_text": [{"text": {"content": detail}}]},
    }


def _patch_page(token: str, page_id: str, properties: dict) -> bool:
    try:
        NotionClient(token).request("PATCH", f"/pages/{page_id}", {"properties": properties})
        return True
    except NotionError as e:
        print(f"  FAILED: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    if args.smoke_test:
        return smoke_test()

    token = os.environ.get("NOTION_TOKEN")
    if not token and not args.dry_run:
        print("NOTION_TOKEN must be set.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = find_candidates(conn)
    print(f"Newsletter events to backfill Source Detail: {len(rows)}")

    updated = 0
    failed = 0

    for row in rows:
        patch = _build_patch(row)
        detail = patch["Source Detail"]["rich_text"][0]["text"]["content"]
        gmail = GMAIL_URL.format(row["source_snapshot"])
        print(f"  [{row['id']}] {row['name'][:60]}: {detail} ({gmail})")

        if args.dry_run:
            updated += 1
            continue

        if _patch_page(token, row["notion_page_id"], patch):
            updated += 1
            time.sleep(0.35)
        else:
            failed += 1

    conn.close()

    print(f"\nDone: {updated} updated, {failed} failed")
    return 0 if failed == 0 else 1


def smoke_test() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY, name TEXT, source TEXT,
        source_snapshot TEXT, date TEXT, end_date TEXT,
        notion_page_id TEXT, notion_status TEXT
    )""")
    conn.execute("""CREATE TABLE processed_emails (
        gmail_message_id TEXT PRIMARY KEY, sender TEXT, email_date TEXT
    )""")

    conn.execute("""INSERT INTO processed_emails VALUES
        ('msg1', 'Riverfront Arts', '2026-05-14')""")
    conn.execute("""INSERT INTO processed_emails VALUES
        ('msg2', 'City Gallery', NULL)""")

    # Future, active, has snapshot: should be found
    conn.execute("""INSERT INTO candidates VALUES
        (1, 'Future Event', 'Newsletter', 'msg1', '2027-06-15', NULL, 'page-1', 'active')""")
    # Future, active, has snapshot, no email_date: should still be found
    conn.execute("""INSERT INTO candidates VALUES
        (2, 'No Date Event', 'Newsletter', 'msg2', '2027-07-01', NULL, 'page-2', 'active')""")
    # Past event: should NOT be found
    conn.execute("""INSERT INTO candidates VALUES
        (3, 'Past Event', 'Newsletter', 'msg1', '2025-01-01', NULL, 'page-3', 'active')""")
    # Archived: should NOT be found
    conn.execute("""INSERT INTO candidates VALUES
        (4, 'Archived Event', 'Newsletter', 'msg1', '2027-08-01', NULL, 'page-4', 'archived')""")
    # Meetup: should NOT be found
    conn.execute("""INSERT INTO candidates VALUES
        (5, 'Meetup Event', 'Meetup', 'json-blob', '2027-09-01', NULL, 'page-5', 'active')""")
    # No snapshot: should NOT be found
    conn.execute("""INSERT INTO candidates VALUES
        (6, 'No Snapshot', 'Newsletter', NULL, '2027-10-01', NULL, 'page-6', 'active')""")
    # Multi-day event, start in past but end in future: should be found
    conn.execute("""INSERT INTO candidates VALUES
        (7, 'Running Show', 'Newsletter', 'msg1', '2025-06-01', '2027-12-31', 'page-7', 'active')""")

    rows = find_candidates(conn)
    found_ids = {r["id"] for r in rows}
    check("finds future active newsletter with snapshot", 1 in found_ids, True)
    check("finds newsletter with no email_date", 2 in found_ids, True)
    check("excludes past event", 3 in found_ids, False)
    check("excludes archived event", 4 in found_ids, False)
    check("excludes meetup event", 5 in found_ids, False)
    check("excludes no-snapshot event", 6 in found_ids, False)
    check("includes running show with future end_date", 7 in found_ids, True)

    # _build_patch
    row_with_date = {"sender": "Riverfront Arts", "email_date": "2026-05-14"}
    patch = _build_patch(row_with_date)
    check("patch has Source Detail",
          patch["Source Detail"]["rich_text"][0]["text"]["content"],
          "Riverfront Arts Newsletter, 14 May")

    row_no_date = {"sender": "City Gallery", "email_date": None}
    patch2 = _build_patch(row_no_date)
    check("patch no date, sender only",
          patch2["Source Detail"]["rich_text"][0]["text"]["content"],
          "City Gallery Newsletter")

    conn.close()
    print(f"\n{'All passed' if ok else 'FAILURES detected'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
