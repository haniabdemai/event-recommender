#!/usr/bin/env python3
"""Tests for sync_verdicts.py (WP2 task 2.2, audit P0-2).

Run: python3 tests/test_sync_verdicts.py
Also wired to `python3 sync_verdicts.py --smoke-test`.

The P0 bug: a past event whose Verdict was still empty at first sync got
`synced_at` stamped anyway. The sync query filters `synced_at IS NULL`, so an
event the user triaged AFTER the first sync pass could never sync again.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import sync_verdicts  # noqa: E402


class FakeNotion:
    """Duck-typed stand-in for sync_verdicts.Notion: no network.

    `pages` mirrors what the database query returns (live pages only);
    `archived_pages` are reachable only via get_page, like the real API.
    """

    def __init__(self, pages: dict[str, dict], archived_pages: dict[str, dict] | None = None):
        self.pages = pages
        self.archived_pages = archived_pages or {}
        self.get_page_calls = 0

    def all_pages(self) -> dict[str, dict]:
        return dict(self.pages)

    def get_page(self, page_id: str) -> dict:
        self.get_page_calls += 1
        if page_id in self.archived_pages:
            return self.archived_pages[page_id]
        if page_id in self.pages:
            return self.pages[page_id]
        raise RuntimeError(f"Notion GET /pages/{page_id} -> HTTP 404: not found")


def notion_page(verdict=None, reason=None, notes=None) -> dict:
    props = {}
    props["Verdict"] = {"select": {"name": verdict} if verdict else None}
    props["Reason it failed"] = {"select": {"name": reason} if reason else None}
    props["Notes"] = {
        "rich_text": [{"plain_text": notes}] if notes else []
    }
    return {"properties": props}


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE candidates (
            id INTEGER PRIMARY KEY,
            name TEXT, date TEXT, end_date TEXT,
            notion_page_id TEXT,
            verdict TEXT, verdict_reason TEXT, verdict_notes TEXT,
            synced_at TIMESTAMP, notion_status TEXT
        )"""
    )
    return conn


def main() -> int:
    ok = True
    # Kill pacing and redirect the log file so tests never touch the repo tree.
    sync_verdicts.BATCH_PAUSE_SEC = 0
    sync_verdicts.SYNC_LOG = Path(tempfile.mkstemp(suffix="_sync_log.txt")[1])

    # --- Case 1: Verdict empty -> row must stay eligible for future syncs ---
    conn = make_db()
    conn.execute(
        "INSERT INTO candidates (id, name, date, notion_page_id) "
        "VALUES (1, 'Untriaged past event', '2020-01-01', 'p1')"
    )
    fake = FakeNotion({"p1": notion_page(verdict=None, notes="some note")})
    sync_verdicts.sync_verdicts(conn, fake, dry_run=False)
    row = conn.execute("SELECT verdict, synced_at FROM candidates WHERE id = 1").fetchone()
    if row["synced_at"] is not None:
        print(f"FAIL: empty Verdict stamped synced_at={row['synced_at']!r}: "
              "row is now locked out of every future sync")
        ok = False
    if row["verdict"] is not None:
        print(f"FAIL: empty Verdict wrote verdict={row['verdict']!r}")
        ok = False

    # --- Case 2: Verdict present -> verdict AND synced_at both set ---
    conn2 = make_db()
    conn2.execute(
        "INSERT INTO candidates (id, name, date, notion_page_id) "
        "VALUES (2, 'Triaged past event', '2020-01-01', 'p2')"
    )
    fake2 = FakeNotion({"p2": notion_page(verdict="Going", reason=None, notes="great")})
    sync_verdicts.sync_verdicts(conn2, fake2, dry_run=False)
    row2 = conn2.execute(
        "SELECT verdict, verdict_notes, synced_at FROM candidates WHERE id = 2"
    ).fetchone()
    if row2["verdict"] != "Going":
        print(f"FAIL: expected verdict 'Going', got {row2['verdict']!r}")
        ok = False
    if row2["synced_at"] is None:
        print("FAIL: real verdict did not stamp synced_at")
        ok = False
    if row2["verdict_notes"] != "great":
        print(f"FAIL: expected notes 'great', got {row2['verdict_notes']!r}")
        ok = False

    # --- Case 3: dry run never writes, regardless of verdict ---
    conn3 = make_db()
    conn3.execute(
        "INSERT INTO candidates (id, name, date, notion_page_id) "
        "VALUES (3, 'Dry run event', '2020-01-01', 'p3')"
    )
    fake3 = FakeNotion({"p3": notion_page(verdict="Going")})
    sync_verdicts.sync_verdicts(conn3, fake3, dry_run=True)
    row3 = conn3.execute("SELECT verdict, synced_at FROM candidates WHERE id = 3").fetchone()
    if row3["verdict"] is not None or row3["synced_at"] is not None:
        print("FAIL: dry run wrote to the DB")
        ok = False
    if fake3.get_page_calls != 0:
        print(f"FAIL: live page hit per-page GET {fake3.get_page_calls}x: "
              "should come from the single all_pages query")
        ok = False

    # --- Case 4: archived page (absent from the query) syncs via get_page fallback ---
    conn4 = make_db()
    conn4.execute(
        "INSERT INTO candidates (id, name, date, notion_page_id) "
        "VALUES (4, 'Archived triaged event', '2020-01-01', 'p4')"
    )
    fake4 = FakeNotion({}, archived_pages={"p4": notion_page(verdict="Not Going")})
    sync_verdicts.sync_verdicts(conn4, fake4, dry_run=False)
    row4 = conn4.execute("SELECT verdict, synced_at FROM candidates WHERE id = 4").fetchone()
    if row4["verdict"] != "Not Going" or row4["synced_at"] is None:
        print(f"FAIL: archived page did not sync via fallback (verdict={row4['verdict']!r})")
        ok = False
    if fake4.get_page_calls != 1:
        print(f"FAIL: expected exactly 1 fallback GET, got {fake4.get_page_calls}")
        ok = False

    # --- Case 5: page deleted everywhere -> counted skipped_404, row untouched ---
    conn5 = make_db()
    conn5.execute(
        "INSERT INTO candidates (id, name, date, notion_page_id) "
        "VALUES (5, 'Deleted page event', '2020-01-01', 'p5')"
    )
    fake5 = FakeNotion({})
    counts5 = sync_verdicts.sync_verdicts(conn5, fake5, dry_run=False)
    row5 = conn5.execute("SELECT verdict, synced_at FROM candidates WHERE id = 5").fetchone()
    if counts5["skipped_404"] != 1 or row5["synced_at"] is not None:
        print(f"FAIL: deleted page not skipped cleanly (counts={counts5})")
        ok = False

    sync_verdicts.SYNC_LOG.unlink(missing_ok=True)
    print("OK: sync_verdicts tests passed" if ok else "FAILED: sync_verdicts")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
