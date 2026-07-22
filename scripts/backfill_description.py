#!/usr/bin/env python3
"""
Backfill: push enriched descriptions (and corrected Type/Date) to Notion
pages where SQLite was updated after the initial write.

Usage:
    NOTION_TOKEN=ntn_... python3 scripts/backfill_description.py --db event-recommender.db [--dry-run]
    python3 scripts/backfill_description.py --db event-recommender.db --smoke-test
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.write_notion import _notion_text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.notion import NOTION_TEXT_LIMIT, NotionClient, NotionError  # noqa: E402


def find_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Find active Notion events with enriched descriptions to sync."""
    return conn.execute("""
        SELECT id, name, description, description_source, format_type,
               date, end_date, time, notion_page_id
        FROM candidates
        WHERE notion_status = 'active'
          AND notion_page_id IS NOT NULL
          AND description_source = 'page'
          AND description IS NOT NULL
          AND length(description) > 0
          AND COALESCE(end_date, date) >= date('now')
    """).fetchall()


def _patch_page(token: str, page_id: str, properties: dict) -> bool:
    try:
        NotionClient(token).request("PATCH", f"/pages/{page_id}", {"properties": properties})
        return True
    except NotionError as e:
        print(f"  FAILED: {e}")
        return False


def _build_patch(row: sqlite3.Row) -> dict:
    """Build the properties dict for a PATCH request."""
    desc = _notion_text(row["description"])
    props = {
        "Description": {"rich_text": [{"text": {"content": desc}}]},
    }

    if row["format_type"]:
        props["Type"] = {"select": {"name": row["format_type"]}}

    if row["end_date"]:
        props["Date"] = {"date": {"start": row["date"], "end": row["end_date"]}}

    return props


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    if args.smoke_test:
        return smoke_test(args.db)

    token = os.environ.get("NOTION_TOKEN")
    if not token and not args.dry_run:
        print("NOTION_TOKEN must be set.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = find_candidates(conn)
    print(f"Enriched events to sync to Notion: {len(rows)}")

    updated = 0
    failed = 0

    for row in rows:
        patch = _build_patch(row)
        patch_fields = list(patch.keys())
        print(f"  [{row['id']}] {row['name'][:60]}: {', '.join(patch_fields)} "
              f"(desc: {len(row['description'])} chars)")

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


def smoke_test(db_path: Path) -> int:
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
        id INTEGER PRIMARY KEY, name TEXT, description TEXT,
        description_source TEXT, format_type TEXT,
        date TEXT, end_date TEXT, time TEXT,
        notion_page_id TEXT, notion_status TEXT,
        pipeline_state TEXT
    )""")
    conn.execute("""INSERT INTO candidates VALUES
        (1, 'Enriched Event', 'Full description here', 'page', 'Exhibition',
         '2027-01-01', '2027-02-01', '19:00', 'abc-123', 'active', 'written')""")
    conn.execute("""INSERT INTO candidates VALUES
        (2, 'Not Enriched', 'Short', NULL, 'Talk',
         '2027-01-15', NULL, NULL, 'def-456', 'active', 'written')""")
    conn.execute("""INSERT INTO candidates VALUES
        (3, 'Enriched But Archived', 'Full desc', 'page', 'Workshop',
         '2027-02-01', NULL, NULL, 'ghi-789', 'archived', 'written')""")
    conn.execute("""INSERT INTO candidates VALUES
        (4, 'Enriched LLM Rejected', 'Full desc from page', 'page', 'Immersive experience',
         '2027-01-10', '2027-03-01', '10:00', 'jkl-012', 'active', 'llm_rejected')""")
    conn.commit()

    rows = find_candidates(conn)
    ids = [r["id"] for r in rows]
    check("finds enriched active events", 1 in ids, True)
    check("skips non-enriched events", 2 not in ids, True)
    check("skips archived events", 3 not in ids, True)
    check("includes llm_rejected if active", 4 in ids, True)
    check("total found", len(ids), 2)

    row1 = [r for r in rows if r["id"] == 1][0]
    patch1 = _build_patch(row1)
    check("patch has Description", "Description" in patch1, True)
    check("patch has Type", "Type" in patch1, True)
    check("patch has Date (end_date present)", "Date" in patch1, True)
    check("Date has start and end",
          patch1["Date"]["date"].get("end") == "2027-02-01", True)
    check("Type is select",
          patch1["Type"]["select"]["name"], "Exhibition")

    row4 = [r for r in rows if r["id"] == 4][0]
    patch4 = _build_patch(row4)
    check("llm_rejected event gets Description", "Description" in patch4, True)
    check("llm_rejected event gets Date range",
          patch4["Date"]["date"].get("end") == "2027-03-01", True)

    conn.execute("""INSERT INTO candidates VALUES
        (5, 'No End Date', 'Page desc', 'page', 'Talk',
         '2027-03-01', NULL, '18:00', 'mno-345', 'active', 'written')""")
    conn.commit()
    row5 = conn.execute("SELECT * FROM candidates WHERE id = 5").fetchone()
    patch5 = _build_patch(row5)
    check("no end_date omits Date", "Date" not in patch5, True)

    long_desc = "x" * 2500
    conn.execute("""INSERT INTO candidates VALUES
        (6, 'Long Desc', ?, 'page', 'Other',
         '2027-04-01', NULL, NULL, 'pqr-678', 'active', 'written')""",
                 (long_desc,))
    conn.commit()
    row6 = conn.execute("SELECT * FROM candidates WHERE id = 6").fetchone()
    patch6 = _build_patch(row6)
    patched_desc = patch6["Description"]["rich_text"][0]["text"]["content"]
    check("long desc truncated", len(patched_desc) <= NOTION_TEXT_LIMIT, True)

    conn.close()

    total = 14
    if not ok:
        return 1
    print(f"\nAll {total} smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
