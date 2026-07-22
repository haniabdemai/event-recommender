#!/usr/bin/env python3
"""
One-off remediation: overwrite corrupted Notion pages with correct SQLite data.

The 3 May batch was written by the LLM agent via MCP, which hallucinated URLs,
dates, descriptions, and venues instead of copying from SQLite. This script
patches each page with the correct data.

Usage:
    NOTION_TOKEN=ntn_... python3 scripts/remediate_notion.py [--dry-run] [--run-date 2026-05-03]
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.write_notion import (  # noqa: E402
    build_properties, build_body, get_venue_geo,
)
from erlib.notion import NotionClient, NotionError  # noqa: E402
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402

SCRIPT_DIR = Path(__file__).parent.parent
PACE_SECONDS = 0.35


class NotionUpdater:
    def __init__(self, token: str):
        self._client = NotionClient(token)

    def update_properties(self, page_id, properties):
        return self._client.request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def get_children(self, page_id):
        return list(self._client.paginate("GET", f"/blocks/{page_id}/children?page_size=100"))

    def delete_block(self, block_id):
        return self._client.request("DELETE", f"/blocks/{block_id}")

    def append_children(self, page_id, children):
        return self._client.request("PATCH", f"/blocks/{page_id}/children", {"children": children})

    def replace_body(self, page_id, new_blocks):
        # No per-delete sleep: the erlib client's bounded 429 backoff is the
        # rate-limit defence. The old 0.1s/block pacing added minutes to the
        # QA auto-fix phase (verify-run 13m on 2026-07-05).
        existing = self.get_children(page_id)
        for block in existing:
            self.delete_block(block["id"])
        if new_blocks:
            self.append_children(page_id, new_blocks)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run-date", default="2026-05-03",
                   help="Only remediate pages from this run_date")
    p.add_argument("--ids", default=None,
                   help="Comma-separated candidate ids: remediate ONLY these "
                        "pages (qa_autofix passes the diagnostic's mismatched "
                        "ids; rewriting a whole run_date took 12m41s of the "
                        "13m verify-run budget on 2026-07-05)")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token and not args.dry_run:
        print("NOTION_TOKEN required (or use --dry-run)", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.ids:
        try:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            print(f"--ids must be comma-separated integers, got {args.ids!r}",
                  file=sys.stderr)
            return 1
        placeholders = ",".join("?" * len(ids))
        rows = list(conn.execute(
            f"""SELECT * FROM candidates
                WHERE id IN ({placeholders})
                  AND notion_page_id IS NOT NULL
                  AND (notion_status IS NULL OR notion_status = 'active')
                ORDER BY id""",
            ids,
        ))
        scope = f"ids={args.ids}"
    else:
        rows = list(conn.execute(
            """SELECT * FROM candidates
               WHERE run_date = ?
                 AND notion_page_id IS NOT NULL
                 AND (notion_status IS NULL OR notion_status = 'active')
               ORDER BY id""",
            (args.run_date,),
        ))
        scope = f"run_date={args.run_date}"
    if args.limit:
        rows = rows[:args.limit]

    print(f"Pages to remediate: {len(rows)} ({scope})")

    if args.dry_run:
        for row in rows:
            print(f"  [{row['id']}] {row['name']} -> {row['notion_page_id']}")
            print(f"       URL: {row['url']}")
        conn.close()
        return 0

    notion = NotionUpdater(token)
    today_iso = args.run_date

    updated = 0
    skipped = 0
    errors = 0
    for row in rows:
        rid = row["id"]
        page_id = row["notion_page_id"]
        print(f"\n[{rid}] {row['name']}")

        if not row["llm_tier"]:
            tier_fallback = row["tier"] or "Borderline"
            print(f"  SKIP: llm_tier is NULL (python tier={tier_fallback})")
            skipped += 1
            continue

        venue_geo = get_venue_geo(conn, row["venue_name"], row["venue_postcode"])
        try:
            properties = build_properties(row, today_iso, venue_geo)
            body_blocks = build_body(row)
        except ValueError as e:
            print(f"  SKIP: {e}")
            skipped += 1
            continue

        try:
            notion.update_properties(page_id, properties)
            notion.replace_body(page_id, body_blocks)
            print(f"  UPDATED {page_id}")
            updated += 1
        except NotionError as e:
            if "archived" in str(e).lower():
                print(f"  SKIP (archived): {page_id}")
                skipped += 1
            else:
                print(f"  ERROR: {e}")
                errors += 1

        time.sleep(PACE_SECONDS)

    conn.close()
    print(f"\nDone: {updated} updated, {skipped} skipped, {errors} errors, {len(rows)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
