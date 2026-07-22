#!/usr/bin/env python3
"""
One-off backfill: reclassify 'Social meetup' candidates that should be 'Outdoor',
then push all format_types to Notion Type property.

Only reclassifies candidates with format_type='Social meetup'. Never overwrites
format_types assigned by the LLM (Exhibition, Book event, Immersive experience, etc.).

Usage:
    NOTION_TOKEN=ntn_... python3 scripts/backfill_type.py --db event-recommender.db [--dry-run]
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.fetch_meetup import classify_format

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.notion import NotionClient, NotionError  # noqa: E402


def update_page_type(token: str, page_id: str, format_type: str) -> bool:
    try:
        NotionClient(token).request(
            "PATCH", f"/pages/{page_id}",
            {"properties": {"Type": {"select": {"name": format_type}}}},
        )
        return True
    except NotionError as e:
        print(f" FAILED: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token and not args.dry_run:
        print("NOTION_TOKEN must be set.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Migrate retired categories
    migrations = {
        "Hackday": "Workshop",
        "Course": "Workshop",
        "Walking tour": "Outdoor",
        "Museum visit": "Exhibition",
    }
    migrated = 0
    for old, new in migrations.items():
        cur = conn.execute(
            "UPDATE candidates SET format_type = ? WHERE format_type = ?",
            (new, old),
        )
        if cur.rowcount:
            print(f"  Migrated {cur.rowcount} rows: {old!r} -> {new!r}")
            migrated += cur.rowcount
    if migrated:
        conn.commit()
        print(f"Migrated {migrated} rows from retired categories")
    else:
        print("No retired categories to migrate")

    # Step 1: Only reclassify candidates currently tagged 'Social meetup'
    # This preserves LLM-assigned types (Exhibition, Book event, etc.)
    social_rows = list(conn.execute(
        "SELECT id, name, description FROM candidates WHERE format_type = 'Social meetup'"
    ))
    reclassified = 0
    for row in social_rows:
        new_type = classify_format(row["name"] or "", row["description"] or "")
        if new_type != "Social meetup":
            if not args.dry_run:
                conn.execute(
                    "UPDATE candidates SET format_type = ? WHERE id = ?",
                    (new_type, row["id"]),
                )
            reclassified += 1
            print(f"  Reclassified [{row['id']}] {row['name']!r}: "
                  f"'Social meetup' -> {new_type!r}")

    if not args.dry_run:
        conn.commit()
    print(f"\nReclassified {reclassified} of {len(social_rows)} 'Social meetup' candidates")

    # Step 2: Push format_type to Notion for all active pages
    notion_rows = list(conn.execute("""
        SELECT id, name, notion_page_id, format_type
        FROM candidates
        WHERE notion_status = 'active'
          AND notion_page_id IS NOT NULL
          AND format_type IS NOT NULL
          AND format_type != ''
    """))

    print(f"\n{len(notion_rows)} active Notion pages to update")

    updated = 0
    errors = 0

    for row in notion_rows:
        pid = row["notion_page_id"]
        ft = row["format_type"]
        print(f"  [{row['id']}] {row['name']!r} -> Type={ft!r}", end="")

        if args.dry_run:
            print(" (dry run)")
            updated += 1
            continue

        if update_page_type(token, pid, ft):
            print(" ok")
            updated += 1
        else:
            print(" FAILED")
            errors += 1

        time.sleep(0.35)

    print(f"\nNotion update: {updated} updated, {errors} errors")
    conn.close()
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
