#!/usr/bin/env python3
"""
One-off backfill: update Travel property on Notion pages where the DB now
has travel data but Notion still shows 'Route unavailable'.

Usage:
    NOTION_TOKEN=ntn_... python3 scripts/backfill_travel.py --db event-recommender.db [--dry-run]
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.write_notion import transit_only

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.notion import NotionClient, NotionError  # noqa: E402


def update_page_travel(token: str, page_id: str, travel_text: str) -> bool:
    properties = {
        "Travel from home": {
            "rich_text": [{"text": {"content": travel_text}}]
        }
    }
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
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("NOTION_TOKEN must be set.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, name, notion_page_id, travel_display, travel_lookup_failed, venue_name
        FROM candidates
        WHERE notion_status = 'active'
          AND notion_page_id IS NOT NULL
          AND travel_display IS NOT NULL
          AND travel_display != ''
          AND travel_lookup_failed = 0
    """).fetchall()

    print(f"Candidates with travel data and Notion pages: {len(rows)}")

    updated = 0
    skipped = 0
    failed = 0

    for row in rows:
        travel_text = transit_only(
            row["travel_display"], row["travel_lookup_failed"] or 0, row["venue_name"]
        )
        if travel_text in ("Route unavailable", "Venue unknown"):
            skipped += 1
            continue

        print(f"  [{row['id']}] {row['name'][:50]} -> {travel_text}")

        if args.dry_run:
            updated += 1
            continue

        if update_page_travel(token, row["notion_page_id"], travel_text):
            updated += 1
            time.sleep(0.35)
        else:
            failed += 1

    print(f"\nDone: {updated} updated, {skipped} skipped (still no route), {failed} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
