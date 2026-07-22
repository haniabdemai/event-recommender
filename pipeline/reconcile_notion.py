#!/usr/bin/env python3
"""
Reconcile SQLite with actual Notion state.

Queries every page in the Notion database, reads key properties
(Verdict, Reason it failed, Notes, To delete), and updates SQLite
to match reality. Also detects pages that SQLite thinks are active
but no longer exist in Notion.

Exit codes:
    0 = success, no orphans found
    1 = network/API error (could not reach Notion: soft-fail, retry later)
    2 = data discrepancy: Notion pages exist with no matching SQLite row
        (hard-fail: means a write succeeded but its DB commit didn't)

Usage:
    NOTION_TOKEN=ntn_... python3 reconcile_notion.py [--dry-run]
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
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from erlib import db as erdb
from erlib import notion as ernotion
from erlib.config import DB_PATH, NOTION_DATABASE_ID
from erlib.notion import NotionError

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)


def extract_select(props: dict, name: str) -> str | None:
    return ernotion.extract_select(props.get(name))


def extract_checkbox(props: dict, name: str) -> bool:
    return ernotion.extract_checkbox(props.get(name))


def extract_rich_text(props: dict, name: str) -> str | None:
    parts = ernotion.extract_rich_text(props.get(name))
    return parts or None


def extract_date(props: dict, name: str) -> str | None:
    """Extract the start date (YYYY-MM-DD) from a Notion date property."""
    start = ernotion.extract_date(props.get(name))
    return start[:10] if start else None


from erlib.normalise import normalise_name  # noqa: E402


def normalize_title(s: str) -> str:
    """Normalize a title for fuzzy matching (erlib.normalise.normalise_name)."""
    return normalise_name(s)


def query_all_notion_pages(token: str) -> list[dict]:
    client = ernotion.NotionClient(token)
    pages = []
    for page in client.paginate(
        "POST", f"/databases/{NOTION_DATABASE_ID}/query", {"page_size": 100}
    ):
        props = page.get("properties", {})
        pages.append({
            "page_id": page["id"],
            "archived": page.get("archived", False),
            "in_trash": page.get("in_trash", False),
            "verdict": extract_select(props, "Verdict"),
            "reason": extract_select(props, "Reason it failed"),
            "notes": extract_rich_text(props, "Notes"),
            "to_delete": extract_checkbox(props, "To delete"),
            "tier": extract_select(props, "Tier"),
            "date": extract_date(props, "Date"),
            "title": "".join(
                p.get("plain_text", "")
                for p in props.get("Event", {}).get("title", [])
            ),
        })
    return pages


def recover_orphans(
    conn: sqlite3.Connection,
    orphan_pages: list[dict],
    dry_run: bool,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Match orphan Notion pages to SQLite candidates by normalized title+date.

    Returns (recovered, unrecoverable, user_added) where each is a list of dicts.
    Prefers candidates in ready_to_write or write_failed state.
    Orphans with no matching candidate are treated as user-added pages:
    a stub SQLite record is created so subsequent reconcile runs skip them.
    """
    recovered = []
    unrecoverable = []
    user_added = []

    for orphan in orphan_pages:
        title_norm = normalize_title(orphan["title"])
        orphan_date = orphan.get("date")

        if not title_norm or not orphan_date:
            unrecoverable.append({
                "page_id": orphan["page_id"],
                "title": orphan["title"][:80],
                "date": orphan_date,
                "reason": "missing title or date on Notion page",
            })
            continue

        candidates = conn.execute(
            "SELECT id, name, date, pipeline_state FROM candidates "
            "WHERE date = ? AND notion_page_id IS NULL "
            "ORDER BY CASE pipeline_state "
            "  WHEN 'ready_to_write' THEN 1 "
            "  WHEN 'write_failed' THEN 2 "
            "  ELSE 3 "
            "END, id ASC",
            (orphan_date,),
        ).fetchall()

        match = None
        for c in candidates:
            if normalize_title(c["name"]) == title_norm:
                match = c
                break

        if not match:
            # Check if a candidate exists but already has a different page_id
            already_tracked = conn.execute(
                "SELECT id, notion_page_id FROM candidates "
                "WHERE date = ? AND notion_page_id IS NOT NULL",
                (orphan_date,),
            ).fetchall()
            dup = None
            for at in already_tracked:
                if normalize_title(
                    conn.execute("SELECT name FROM candidates WHERE id=?", (at["id"],)).fetchone()[0]
                ) == title_norm:
                    dup = at
                    break

            if dup:
                unrecoverable.append({
                    "page_id": orphan["page_id"],
                    "title": orphan["title"][:80],
                    "date": orphan_date,
                    "reason": (f"duplicate: candidate {dup['id']} already tracked by page "
                               f"{dup['notion_page_id'][:12]}... (flag this page 'To delete')"),
                })
            else:
                # No candidate matches: this is a user-added page.
                # Create a stub record so the pipeline acknowledges it without blocking.
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if not dry_run:
                    conn.execute(
                        """INSERT INTO candidates
                           (run_date, name, date, source, score, tier,
                            pipeline_state, notion_page_id, notion_status,
                            travel_lookup_failed)
                           VALUES (?, ?, ?, 'manual', 0, 'Manual',
                                   'written', ?, 'active', 1)""",
                        (today, orphan["title"], orphan_date, orphan["page_id"]),
                    )
                user_added.append({
                    "page_id": orphan["page_id"],
                    "title": orphan["title"][:80],
                    "date": orphan_date,
                })
            continue

        if not dry_run:
            conn.execute(
                "UPDATE candidates SET notion_page_id=?, pipeline_state='written', "
                "notion_status='active' WHERE id=?",
                (orphan["page_id"], match["id"]),
            )

        recovered.append({
            "candidate_id": match["id"],
            "page_id": orphan["page_id"],
            "name": orphan["title"][:80],
            "previous_state": match["pipeline_state"],
        })

    if (recovered or user_added) and not dry_run:
        conn.commit()

    return recovered, unrecoverable, user_added


def write_report(
    recovered: list[dict],
    unrecoverable: list[dict],
    user_added: list[dict],
    total_notion: int,
    total_sqlite: int,
) -> None:
    """Write structured JSON report for debugging and pipeline summary."""
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "total_notion_pages": total_notion,
        "total_sqlite_tracked": total_sqlite,
        "orphans_found": len(recovered) + len(unrecoverable) + len(user_added),
        "recovered": len(recovered),
        "unrecoverable": len(unrecoverable),
        "user_added": len(user_added),
        "recovered_details": recovered,
        "unrecoverable_details": unrecoverable,
        "user_added_details": user_added,
    }
    with open(".last_split_state_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print("Report written to .last_split_state_report.json")


def _make_test_conn(candidates: list[tuple]) -> sqlite3.Connection:
    """Create an in-memory DB with the candidates schema for flow tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, date TEXT NOT NULL,
            run_date TEXT, source TEXT, score INTEGER, tier TEXT,
            pipeline_state TEXT, notion_page_id TEXT, notion_status TEXT,
            travel_lookup_failed INTEGER DEFAULT 0
        )
    """)
    if candidates:
        conn.executemany(
            "INSERT INTO candidates (id, name, date, pipeline_state) VALUES (?, ?, ?, ?)",
            candidates,
        )
        conn.commit()
    return conn


def _smoke_tests() -> int:
    ok = True

    def check(label: str, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    print("normalize_title tests:")
    check("empty string", normalize_title(""), "")
    check("basic lowercase", normalize_title("Jazz Night"), "jazz night")
    check("extra whitespace", normalize_title("  Jazz   Night  "), "jazz night")
    check("unicode curly quote",
          normalize_title("Mara Lune’s We Chree"),
          "mara lune's we chree")
    check("curly quote matches straight",
          normalize_title("Mara Lune’s We Chree"),
          normalize_title("Mara Lune's We Chree"))
    check("unicode em dash",
          normalize_title("Event — Special"),
          "event - special")
    check("em dash matches hyphen",
          normalize_title("Event — Special"),
          normalize_title("Event - Special"))
    check("unicode en dash",
          normalize_title("6–15 June"),
          "6-15 june")
    check("mixed case + whitespace",
          normalize_title("  THE  Big  Event  "),
          "the big event")
    check("NFKD ligature",
          normalize_title("ﬁnale"),
          "finale")
    check("NBSP becomes space",
          normalize_title("Sat\xa06\xa0Jun"),
          "sat 6 jun")
    check("zero-width space removed",
          normalize_title("Hello​World"),
          "helloworld")

    print("\nextract_date tests:")
    check("valid datetime",
          extract_date({"Date": {"type": "date", "date": {"start": "2026-06-15T19:00:00.000+01:00"}}}, "Date"),
          "2026-06-15")
    check("date only",
          extract_date({"Date": {"type": "date", "date": {"start": "2026-06-15"}}}, "Date"),
          "2026-06-15")
    check("null date",
          extract_date({"Date": {"type": "date", "date": None}}, "Date"),
          None)
    check("missing property",
          extract_date({}, "Date"),
          None)

    print("\nrecover_orphans matching tests:")
    conn = _make_test_conn([
        (1, "Jazz Night at the City Arts Centre", "2026-06-15", "ready_to_write"),
        (2, "Outdoor Cinema", "2026-06-20", "expired"),
        (3, "Jazz Night at the City Arts Centre", "2026-06-22", "ready_to_write"),
        (4, "Film Club", "2026-06-15", "written"),
        (5, "Tech Meetup", "2026-06-15", "ready_to_write"),
    ])
    conn.execute("UPDATE candidates SET notion_page_id='existing-page' WHERE id=4")
    conn.commit()

    # Test 1: exact match, ready_to_write candidate
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-aaa", "title": "Jazz Night at the City Arts Centre", "date": "2026-06-15", "to_delete": False},
    ], dry_run=True)
    check("exact match finds ready_to_write candidate",
          len(recovered), 1)
    check("exact match correct candidate_id",
          recovered[0]["candidate_id"] if recovered else None, 1)

    # Test 2: match with different Unicode (curly quote)
    conn.execute("INSERT INTO candidates (id, name, date, pipeline_state) VALUES (10, 'Mara Lune''s Show', '2026-07-01', 'write_failed')")
    conn.commit()
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-bbb", "title": "Mara Lune’s Show", "date": "2026-07-01", "to_delete": False},
    ], dry_run=True)
    check("unicode normalization matches curly quote to straight",
          len(recovered), 1)

    # Test 3: no match (wrong date) → user_added, not unrecoverable
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-ccc", "title": "Jazz Night at the City Arts Centre", "date": "2026-09-01", "to_delete": False},
    ], dry_run=True)
    check("no match creates user_added entry",
          len(user_added), 1)
    check("user_added has correct page_id",
          user_added[0]["page_id"] if user_added else None, "page-ccc")

    # Test 4: expired candidate still matches (scope is ALL states)
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-ddd", "title": "Outdoor Cinema", "date": "2026-06-20", "to_delete": False},
    ], dry_run=True)
    check("expired candidate is matchable",
          len(recovered), 1)
    check("expired match records previous state",
          recovered[0]["previous_state"] if recovered else None, "expired")

    # Test 5: prefers ready_to_write over other states
    conn.execute("INSERT INTO candidates (id, name, date, pipeline_state) VALUES (20, 'Tech Meetup', '2026-06-15', 'expired')")
    conn.commit()
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-eee", "title": "Tech Meetup", "date": "2026-06-15", "to_delete": False},
    ], dry_run=True)
    check("prefers ready_to_write over expired",
          recovered[0]["candidate_id"] if recovered else None, 5)

    # Test 6: missing title on Notion page → unrecoverable
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-fff", "title": "", "date": "2026-06-15", "to_delete": False},
    ], dry_run=True)
    check("empty title is unrecoverable",
          len(unrecoverable), 1)
    check("empty title reason",
          "missing title" in unrecoverable[0].get("reason", "") if unrecoverable else False, True)

    # Test 7: missing date on Notion page → unrecoverable
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-ggg", "title": "Some Event", "date": None, "to_delete": False},
    ], dry_run=True)
    check("missing date is unrecoverable",
          len(unrecoverable), 1)

    # Test 8: candidate with existing page_id → unrecoverable with duplicate reason
    recovered, unrecoverable, user_added = recover_orphans(conn, [
        {"page_id": "page-hhh", "title": "Film Club", "date": "2026-06-15", "to_delete": False},
    ], dry_run=True)
    check("candidate with existing page_id is skipped",
          len(unrecoverable), 1)
    check("duplicate reason mentions existing candidate",
          "duplicate" in unrecoverable[0].get("reason", "") if unrecoverable else False, True)
    check("duplicate reason includes candidate id",
          "candidate 4" in unrecoverable[0].get("reason", "") if unrecoverable else False, True)

    # Test 9: dry_run=False actually writes to DB (pipeline recovery)
    conn2 = _make_test_conn([
        (100, "Live Write Test", "2026-08-01", "ready_to_write"),
    ])
    recovered, _, _ = recover_orphans(conn2, [
        {"page_id": "page-live", "title": "Live Write Test", "date": "2026-08-01", "to_delete": False},
    ], dry_run=False)
    row = conn2.execute("SELECT pipeline_state, notion_page_id, notion_status FROM candidates WHERE id=100").fetchone()
    check("dry_run=False writes notion_page_id",
          row["notion_page_id"] if row else None, "page-live")
    check("dry_run=False sets pipeline_state=written",
          row["pipeline_state"] if row else None, "written")
    check("dry_run=False sets notion_status=active",
          row["notion_status"] if row else None, "active")
    conn2.close()

    # Test 10: user_added dry_run=False creates stub record
    conn_ua = _make_test_conn([])
    recovered, unrecoverable, user_added = recover_orphans(conn_ua, [
        {"page_id": "page-manual", "title": "GearExpo UK", "date": "2026-06-27", "to_delete": False},
    ], dry_run=False)
    check("user_added creates stub", len(user_added), 1)
    stub = conn_ua.execute(
        "SELECT name, date, source, score, tier, pipeline_state, notion_page_id, notion_status, travel_lookup_failed "
        "FROM candidates WHERE notion_page_id='page-manual'"
    ).fetchone()
    check("stub has correct name", stub["name"] if stub else None, "GearExpo UK")
    check("stub has source=manual", stub["source"] if stub else None, "manual")
    check("stub has score=0", stub["score"] if stub else None, 0)
    check("stub has tier=Manual", stub["tier"] if stub else None, "Manual")
    check("stub has pipeline_state=written", stub["pipeline_state"] if stub else None, "written")
    check("stub has notion_status=active", stub["notion_status"] if stub else None, "active")
    check("stub has travel_lookup_failed=1", stub["travel_lookup_failed"] if stub else None, 1)
    conn_ua.close()

    # Test 11: user_added dry_run=True does NOT create stub
    conn_dry = _make_test_conn([])
    recovered, unrecoverable, user_added = recover_orphans(conn_dry, [
        {"page_id": "page-dry", "title": "Dry Run Event", "date": "2026-07-01", "to_delete": False},
    ], dry_run=True)
    check("dry_run user_added still reported", len(user_added), 1)
    stub_count = conn_dry.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    check("dry_run user_added does not write", stub_count, 0)
    conn_dry.close()

    conn.close()

    # ---- Flow tests ----
    print("\nflow tests:")

    # Flow test 1: archived/in_trash pages excluded from real_orphans
    notion_only_mixed = [
        {"page_id": "p1", "title": "Active orphan", "date": "2026-07-01",
         "to_delete": False, "archived": False, "in_trash": False},
        {"page_id": "p2", "title": "Archived page", "date": "2026-07-02",
         "to_delete": False, "archived": True, "in_trash": False},
        {"page_id": "p3", "title": "Trashed page", "date": "2026-07-03",
         "to_delete": False, "archived": False, "in_trash": True},
        {"page_id": "p4", "title": "Flagged delete", "date": "2026-07-04",
         "to_delete": True, "archived": False, "in_trash": False},
    ]
    real_orphans = [
        p for p in notion_only_mixed
        if not p.get("to_delete") and not p.get("archived") and not p.get("in_trash")
    ]
    check("filter excludes archived/trashed/flagged", len(real_orphans), 1)
    check("filter keeps active orphan", real_orphans[0]["page_id"] if real_orphans else None, "p1")

    # Flow test 2: write_report produces valid JSON
    write_report(
        [{"candidate_id": 1, "page_id": "p1", "name": "Test", "previous_state": "ready_to_write"}],
        [{"page_id": "p2", "title": "Unmatchable", "date": "2026-07-01", "reason": "missing title or date on Notion page"}],
        [{"page_id": "p3", "title": "User Event", "date": "2026-07-01"}],
        500, 400,
    )
    with open(".last_split_state_report.json") as f:
        report = json.load(f)
    check("report has correct orphan count", report["orphans_found"], 3)
    check("report has correct recovered count", report["recovered"], 1)
    check("report has correct unrecoverable count", report["unrecoverable"], 1)
    check("report has correct user_added count", report["user_added"], 1)
    check("report has timestamp", "timestamp_utc" in report, True)
    os.remove(".last_split_state_report.json")

    # Flow test 3: multiple orphans, mixed recovered/user_added/unrecoverable
    conn3 = _make_test_conn([
        (1, "Matchable Event", "2026-08-01", "ready_to_write"),
        (2, "Another Event", "2026-08-02", "expired"),
    ])
    recovered, unrecoverable, user_added = recover_orphans(conn3, [
        {"page_id": "pa", "title": "Matchable Event", "date": "2026-08-01", "to_delete": False},
        {"page_id": "pb", "title": "Another Event", "date": "2026-08-02", "to_delete": False},
        {"page_id": "pc", "title": "No Match At All", "date": "2026-12-25", "to_delete": False},
        {"page_id": "pd", "title": "", "date": "2026-12-25", "to_delete": False},
    ], dry_run=True)
    check("mixed: 2 recovered", len(recovered), 2)
    check("mixed: 1 user_added (no-match)", len(user_added), 1)
    check("mixed: 1 unrecoverable (empty title)", len(unrecoverable), 1)
    check("mixed: user_added is the no-match",
          user_added[0]["page_id"] if user_added else None, "pc")
    check("mixed: unrecoverable is the empty-title",
          unrecoverable[0]["page_id"] if unrecoverable else None, "pd")
    conn3.close()

    print(f"\n{'PASS' if ok else 'FAIL'}: smoke tests {'all passed' if ok else 'had failures'}")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recover", action="store_true",
                        help="Attempt to match orphan Notion pages to candidates by title+date")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run offline unit tests of normalization and matching")
    args = parser.parse_args()

    if args.smoke_test:
        sys.exit(_smoke_tests())

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("ERROR: NOTION_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    conn = erdb.connect(DB_PATH)

    sqlite_rows = conn.execute(
        "SELECT id, notion_page_id, verdict, verdict_reason, verdict_notes, "
        "notion_status, pipeline_state, llm_tier "
        "FROM candidates WHERE notion_page_id IS NOT NULL"
    ).fetchall()
    sqlite_by_page_id = {}
    for row in sqlite_rows:
        pid = row["notion_page_id"].replace("-", "")
        sqlite_by_page_id[pid] = dict(row)

    print(f"SQLite: {len(sqlite_by_page_id)} candidates with notion_page_id")

    print("Querying Notion database...")
    try:
        notion_pages = query_all_notion_pages(token)
    except (NotionError, OSError) as e:
        print(f"ERROR: could not reach Notion API: {e}", file=sys.stderr)
        conn.close()
        sys.exit(1)  # exit 1 = network error (soft-fail)
    print(f"Notion: {len(notion_pages)} pages found")

    notion_by_page_id = {}
    for p in notion_pages:
        pid = p["page_id"].replace("-", "")
        notion_by_page_id[pid] = p

    updates = []
    orphans_in_sqlite = []
    notion_only = []

    for pid, sq in sqlite_by_page_id.items():
        np = notion_by_page_id.get(pid)
        if not np:
            orphans_in_sqlite.append(sq)
            continue

        changes = {}

        if np["in_trash"]:
            new_status = "deleted"
        elif np["archived"]:
            new_status = "archived"
        elif np["to_delete"]:
            new_status = "flagged_delete"
        else:
            new_status = "active"

        if sq["notion_status"] != new_status:
            changes["notion_status"] = new_status

        if np["verdict"] and sq["verdict"] != np["verdict"]:
            changes["verdict"] = np["verdict"]

        if np["reason"] and sq["verdict_reason"] != np["reason"]:
            changes["verdict_reason"] = np["reason"]

        if np["notes"] and sq["verdict_notes"] != np["notes"]:
            changes["verdict_notes"] = np["notes"]

        if changes:
            updates.append({"sqlite_id": sq["id"], "page_id": pid, "changes": changes, "name": np["title"]})

    for pid, np in notion_by_page_id.items():
        if pid not in sqlite_by_page_id:
            notion_only.append(np)

    print(f"\n{'=' * 60}")
    print("RECONCILIATION REPORT")
    print(f"{'=' * 60}")

    verdict_syncs = sum(1 for u in updates if "verdict" in u["changes"])
    status_syncs = sum(1 for u in updates if "notion_status" in u["changes"])
    reason_syncs = sum(1 for u in updates if "verdict_reason" in u["changes"])
    notes_syncs = sum(1 for u in updates if "verdict_notes" in u["changes"])

    status_breakdown = {}
    for u in updates:
        if "notion_status" in u["changes"]:
            s = u["changes"]["notion_status"]
            status_breakdown[s] = status_breakdown.get(s, 0) + 1

    verdict_breakdown = {}
    for u in updates:
        if "verdict" in u["changes"]:
            v = u["changes"]["verdict"]
            verdict_breakdown[v] = verdict_breakdown.get(v, 0) + 1

    print(f"\nTotal SQLite rows with page_id: {len(sqlite_by_page_id)}")
    print(f"Total Notion pages:             {len(notion_by_page_id)}")
    print(f"Rows needing updates:           {len(updates)}")
    print(f"  - notion_status changes:      {status_syncs}")
    for s, c in sorted(status_breakdown.items()):
        print(f"      {s}: {c}")
    print(f"  - verdict syncs:              {verdict_syncs}")
    for v, c in sorted(verdict_breakdown.items()):
        print(f"      {v}: {c}")
    print(f"  - reason syncs:               {reason_syncs}")
    print(f"  - notes syncs:                {notes_syncs}")
    print(f"Orphans (in SQLite, gone from Notion): {len(orphans_in_sqlite)}")
    print(f"Notion-only (no SQLite match):  {len(notion_only)}")

    flagged = [u for u in updates if u["changes"].get("notion_status") == "flagged_delete"]
    if flagged:
        print(f"\nPages flagged 'To delete' in Notion: {len(flagged)}")

    if args.dry_run and not args.recover:
        print("\n[DRY RUN: no changes written]")
        print("\nSample updates (first 10):")
        for u in updates[:10]:
            print(f"  [{u['sqlite_id']}] {u['name']}: {u['changes']}")
        if orphans_in_sqlite:
            print("\nSample orphans (first 5):")
            for o in orphans_in_sqlite[:5]:
                print(f"  [{o['id']}] page_id={o['notion_page_id']}")
        return

    if args.dry_run and args.recover:
        # --recover --dry-run: skip sync writes, fall through to recovery
        print(f"\n[DRY RUN: skipping {len(updates)} sync updates, proceeding to recovery check]")
        applied = 0
    else:
        applied = 0
        for u in updates:
            sets = []
            vals = []
            for col, val in u["changes"].items():
                sets.append(f"{col} = ?")
                vals.append(val)
            vals.append(u["sqlite_id"])
            conn.execute(
                f"UPDATE candidates SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            applied += 1

        for o in orphans_in_sqlite:
            conn.execute(
                "UPDATE candidates SET notion_status = 'gone' WHERE id = ?",
                (o["id"],),
            )

    conn.commit()

    if args.dry_run and args.recover:
        print(f"\n[DRY RUN: would apply {len(updates)} sync updates, mark {len(orphans_in_sqlite)} SQLite orphans 'gone']")
    else:
        print(f"\nAPPLIED: {applied} row updates, {len(orphans_in_sqlite)} orphans marked 'gone'")

    # Orphans = Notion pages with no matching SQLite row.
    # Exclude: to_delete (user already flagged), archived, in_trash (already gone from user's view).
    real_orphans = [
        p for p in notion_only
        if not p.get("to_delete") and not p.get("archived") and not p.get("in_trash")
    ]

    if not real_orphans:
        conn.close()
        print("Done. No orphan Notion pages found.")
        return

    if not args.recover:
        conn.close()
        print(f"\nORPHAN_ALERT: {len(real_orphans)} Notion pages have no matching SQLite row:")
        for p in real_orphans[:10]:
            print(f"  {p['page_id']}: {p['title'][:60]}")
        if len(real_orphans) > 10:
            print(f"  ... and {len(real_orphans) - 10} more")
        print("This means a Notion write succeeded but its DB commit didn't.")
        print("Exiting with code 2: investigate before continuing the pipeline.")
        sys.exit(2)

    # --recover mode: attempt to match orphans to candidates
    print(f"\nRECOVER: {len(real_orphans)} orphan Notion pages found. Attempting recovery...")

    recovered, unrecoverable, user_added = recover_orphans(conn, real_orphans, args.dry_run)
    conn.close()

    write_report(recovered, unrecoverable, user_added, len(notion_pages), len(sqlite_by_page_id))

    if recovered:
        label = "would recover" if args.dry_run else "recovered"
        print(f"\nRECOVER_OK: {label} {len(recovered)} orphan(s):")
        for r in recovered:
            print(f"  [{r['candidate_id']}] {r['name']} (was {r['previous_state']})")

    if user_added:
        label = "would create stub for" if args.dry_run else "created stub record for"
        print(f"\nUSER_ADDED: {label} {len(user_added)} user-added page(s):")
        for ua in user_added:
            print(f"  {ua['page_id'][:12]}...: {ua['title']} ({ua['date']})")

    if unrecoverable:
        print(f"\nRECOVER_FAIL: {len(unrecoverable)} orphan(s) could not be matched:")
        for u in unrecoverable:
            print(f"  {u['page_id']}: {u['title']} ({u['date']}): {u['reason']}")
        if args.dry_run:
            print("\n[DRY RUN: no changes written. Unrecoverable orphans would cause exit 4.]")
        else:
            print("\nExiting with code 4: unrecoverable split state. Manual intervention required.")
            print("See .last_split_state_report.json for full details.")
            sys.exit(4)
        return

    if args.dry_run:
        print("\n[DRY RUN: no changes written]")
    else:
        total = len(recovered) + len(user_added)
        print(f"\nAll {total} orphan(s) resolved successfully.")


if __name__ == "__main__":
    main()
