#!/usr/bin/env python3
"""
Merge multi-day event splits from newsletter extraction.

When an LLM subagent creates separate candidates for each date of a multi-day
event instead of using end_date, this script detects and merges them.

Only operates on Newsletter-source candidates in pending_llm state.
Integrated into cmd_score_travel() in weekly_run.sh: runs automatically
before scoring, no LLM instruction needed.

Usage:
    python3 merge_multiday.py --dry-run [db_path]
    python3 merge_multiday.py --apply [db_path]
    python3 merge_multiday.py --smoke-test
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
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402

MAX_CONSECUTIVE_GAP_DAYS = 2
MAX_TOTAL_SPAN_DAYS = 7


# Upgraded from strip/lower-only: curly-quote/em-dash title variants of the
# same multi-day event now merge (erlib.normalise, WP3.4).
from erlib.normalise import normalise_name as _normalise  # noqa: E402


def _normalise_venue(venue: str | None) -> str | None:
    if not venue:
        return None
    return venue.strip().lower()


def find_merge_groups(db_path: Path) -> list[list[dict]]:
    """Find groups of candidates that are split multi-day events."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    candidates = conn.execute("""
        SELECT id, name, date, end_date, venue_name, run_date, description
        FROM candidates
        WHERE pipeline_state = 'pending_llm'
          AND source = 'Newsletter'
          AND COALESCE(end_date, date) >= date('now')
        ORDER BY name, date
    """).fetchall()
    conn.close()

    if not candidates:
        return []

    keyed: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in candidates:
        key = (_normalise(row["name"]), row["run_date"])
        keyed[key].append(dict(row))

    groups = []
    for _key, members in keyed.items():
        if len(members) < 2:
            continue

        members.sort(key=lambda c: c["date"])

        if not _dates_form_contiguous_span(members):
            continue

        if not _venues_compatible(members):
            continue

        groups.append(members)

    return groups


def _dates_form_contiguous_span(members: list[dict]) -> bool:
    """Check dates form a contiguous span: max 2-day gap between consecutive,
    max 7-day total span."""
    dates = [date.fromisoformat(m["date"]) for m in members]

    total_span = (dates[-1] - dates[0]).days
    if total_span > MAX_TOTAL_SPAN_DAYS:
        return False

    for i in range(1, len(dates)):
        gap = (dates[i] - dates[i - 1]).days
        if gap > MAX_CONSECUTIVE_GAP_DAYS:
            return False

    return True


def _venues_compatible(members: list[dict]) -> bool:
    """If venue_name is set on multiple members, they must match."""
    venues = set()
    for m in members:
        v = _normalise_venue(m["venue_name"])
        if v:
            venues.add(v)
    return len(venues) <= 1


def _best_description(members: list[dict]) -> str | None:
    """Return the longest non-null description from the group."""
    best = None
    best_len = 0
    for m in members:
        desc = m.get("description")
        if desc and len(desc) > best_len:
            best = desc
            best_len = len(desc)
    return best


def merged_end_date(group: list[dict]) -> str:
    """The end date a merge will produce: used by apply AND dry-run preview
    (audit P2: the preview showed group[-1]['date'], diverging from apply
    whenever a member's end_date ran past the last start date)."""
    all_dates = [m["date"] for m in group]
    all_end_dates = [m["end_date"] for m in group if m.get("end_date")]
    return max(all_dates + all_end_dates)


def merge_group(conn: sqlite3.Connection, group: list[dict]) -> dict:
    """Merge a group: keep earliest-date candidate, set end_date, mark rest."""
    keep = group[0]  # earliest date (already sorted)
    discard = group[1:]
    end_date = merged_end_date(group)

    best_desc = _best_description(group)
    updates = {"end_date": end_date}
    if best_desc and (not keep["description"] or len(best_desc) > len(keep["description"])):
        updates["description"] = best_desc

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE candidates SET {set_clause} WHERE id = ?",
        (*updates.values(), keep["id"]),
    )

    discard_ids = [d["id"] for d in discard]
    placeholders = ",".join("?" for _ in discard_ids)
    conn.execute(
        f"UPDATE candidates SET pipeline_state = 'duplicate' WHERE id IN ({placeholders})",
        discard_ids,
    )

    return {
        "kept_id": keep["id"],
        "name": keep["name"],
        "date": keep["date"],
        "end_date": end_date,
        "discarded_ids": discard_ids,
        "description_updated": "description" in updates,
    }


def cmd_apply(db_path: Path) -> int:
    groups = find_merge_groups(db_path)
    if not groups:
        print("MERGE_MULTIDAY: 0 groups found. Nothing to merge.")
        return 0

    conn = sqlite3.connect(db_path)
    results = []
    for group in groups:
        result = merge_group(conn, group)
        results.append(result)
        print(f"  MERGED: '{result['name']}': kept id={result['kept_id']}, "
              f"date={result['date']}→{result['end_date']}, "
              f"discarded ids={result['discarded_ids']}")

    conn.commit()
    conn.close()

    total_discarded = sum(len(r["discarded_ids"]) for r in results)
    print(f"MERGE_MULTIDAY: {len(results)} groups merged, "
          f"{total_discarded} candidates marked duplicate.")
    return 0


def cmd_dry_run(db_path: Path) -> int:
    groups = find_merge_groups(db_path)
    if not groups:
        print("MERGE_MULTIDAY: 0 groups found. Nothing to merge.")
        return 0

    for group in groups:
        print(f"  Would merge {len(group)} candidates: '{group[0]['name']}'")
        for m in group:
            print(f"    ID {m['id']}: date={m['date']}, "
                  f"venue={m['venue_name'] or '(none)'}")
        print(f"    → Keep id={group[0]['id']}, "
              f"end_date={merged_end_date(group)}")

    total_discarded = sum(len(g) - 1 for g in groups)
    print(f"MERGE_MULTIDAY: {len(groups)} groups would merge, "
          f"{total_discarded} candidates would be marked duplicate.")
    return 0


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    import tempfile
    passed = 0
    failed = 0
    today = date.today()
    future = (today + timedelta(days=10)).isoformat()
    future2 = (today + timedelta(days=11)).isoformat()
    future3 = (today + timedelta(days=12)).isoformat()
    future_gap = (today + timedelta(days=13)).isoformat()
    future_far = (today + timedelta(days=20)).isoformat()

    def check(name: str, condition: bool):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name}")

    # merged_end_date: preview and apply share this: a member's end_date
    # past the last start date must win (dry-run used to show the last
    # start date instead).
    check("merged_end_date takes max end_date over last start",
          merged_end_date([
              {"date": future, "end_date": future_far},
              {"date": future2, "end_date": None},
          ]) == future_far)
    check("merged_end_date falls back to last start date",
          merged_end_date([
              {"date": future, "end_date": None},
              {"date": future2, "end_date": None},
          ]) == future2)

    def make_db():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115  path must outlive handle
        conn = sqlite3.connect(tmp.name)
        conn.execute("""CREATE TABLE candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, date TEXT NOT NULL, end_date TEXT,
            venue_name TEXT, run_date TEXT, source TEXT,
            pipeline_state TEXT DEFAULT 'pending_llm',
            description TEXT
        )""")
        return conn, Path(tmp.name)

    # Test 1: Basic merge: 3 consecutive dates
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future2, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future3, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("3 consecutive dates → 1 group", len(groups) == 1)
    check("group has 3 members", len(groups[0]) == 3)
    db.unlink()

    # Test 1b (WP3.4): curly-quote title variant still merges: pins the
    # upgrade from strip/lower-only _normalise to erlib normalise_name.
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Ada\u2019s Lantern Walk", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Ada's Lantern Walk", future2, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("curly-quote variant merges with straight-quote", len(groups) == 1 and len(groups[0]) == 2)
    db.unlink()

    # Test 2: No merge: different names
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event Y", future2, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("different names → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 3: No merge: different run_dates
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future2, "Venue A", "2026-06-21", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("different run_dates → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 4: No merge: Meetup source
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Tennis for All", future, "Park", "2026-06-14", "Meetup"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Tennis for All", future2, "Park", "2026-06-14", "Meetup"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("Meetup source → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 5: No merge: different venues
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future2, "Venue B", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("different venues → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 6: Merge OK: one has venue, other doesn't
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future2, None, "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("one venue + one null → 1 group", len(groups) == 1)
    db.unlink()

    # Test 7: No merge: gap too large (3 days between consecutive)
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future_gap, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("3-day gap → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 8: Merge OK: 2-day gap (matches Architecture on Stage pattern)
    conn, db = make_db()
    d1 = (today + timedelta(days=10)).isoformat()
    d2 = (today + timedelta(days=12)).isoformat()  # 2-day gap
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Architecture Talks Series", d1, "City Arts Centre", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Architecture Talks Series", d2, "City Arts Centre", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("2-day gap → 1 group", len(groups) == 1)
    db.unlink()

    # Test 9: No merge: total span > 7 days
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future_far, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("10-day span → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 10: No merge: already-written candidates
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source, pipeline_state) VALUES (?, ?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter", "written"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source, pipeline_state) VALUES (?, ?, ?, ?, ?, ?)",
                 ("Event X", future2, "Venue A", "2026-06-14", "Newsletter", "written"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("written state → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 11: Apply: verify DB state after merge
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future2, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future3, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    cmd_apply(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    kept = conn.execute("SELECT * FROM candidates WHERE id = 1").fetchone()
    check("apply: kept has end_date", kept["end_date"] == future3)
    check("apply: kept still pending_llm", kept["pipeline_state"] == "pending_llm")
    discarded = conn.execute("SELECT * FROM candidates WHERE id IN (2, 3)").fetchall()
    check("apply: discarded are duplicate", all(d["pipeline_state"] == "duplicate" for d in discarded))
    conn.close()
    db.unlink()

    # Test 12: Apply: best description kept
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source, description) VALUES (?, ?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter", "Short"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source, description) VALUES (?, ?, ?, ?, ?, ?)",
                 ("Event X", future2, "Venue A", "2026-06-14", "Newsletter", "A much longer and more detailed description of the event"))
    conn.commit()
    conn.close()
    cmd_apply(db)
    conn = sqlite3.connect(db)
    kept = conn.execute("SELECT description FROM candidates WHERE id = 1").fetchone()
    check("apply: longer description kept", "much longer" in kept[0])
    conn.close()
    db.unlink()

    # Test 13: Case-insensitive name matching
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("event x", future2, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("case-insensitive names → 1 group", len(groups) == 1)
    db.unlink()

    # Test 14: No merge: single candidate (no group)
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("single candidate → 0 groups", len(groups) == 0)
    db.unlink()

    # Test 15: Candidate with existing end_date + stray: merge keeps later end_date
    conn, db = make_db()
    conn.execute("INSERT INTO candidates (name, date, end_date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?, ?)",
                 ("Event X", future, future3, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future2, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    groups = find_merge_groups(db)
    check("one with end_date + stray → 1 group", len(groups) == 1)
    cmd_apply(db)
    conn = sqlite3.connect(db)
    kept = conn.execute("SELECT end_date FROM candidates WHERE id = 1").fetchone()
    check("apply: existing end_date preserved (not shrunk)", kept[0] == future3)
    conn.close()
    db.unlink()

    # Test 16: Stray date is later than kept's end_date: end_date extended
    conn, db = make_db()
    (today + timedelta(days=14)).isoformat()
    conn.execute("INSERT INTO candidates (name, date, end_date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?, ?)",
                 ("Event X", future, future2, "Venue A", "2026-06-14", "Newsletter"))
    conn.execute("INSERT INTO candidates (name, date, venue_name, run_date, source) VALUES (?, ?, ?, ?, ?)",
                 ("Event X", future3, "Venue A", "2026-06-14", "Newsletter"))
    conn.commit()
    conn.close()
    cmd_apply(db)
    conn = sqlite3.connect(db)
    kept = conn.execute("SELECT end_date FROM candidates WHERE id = 1").fetchone()
    check("apply: end_date extended to stray's date", kept[0] == future3)
    conn.close()
    db.unlink()

    print(f"\n{passed} passed, {failed} failed out of {passed + failed} tests.")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Merge multi-day event splits")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Show what would be merged (default)")
    group.add_argument("--apply", action="store_true",
                       help="Merge candidates in SQLite")
    group.add_argument("--smoke-test", action="store_true",
                       help="Run built-in tests")
    parser.add_argument("db", nargs="?", default=str(DEFAULT_DB),
                        help="Path to SQLite database")
    args = parser.parse_args()

    if args.smoke_test:
        return _smoke_test()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    if args.apply:
        return cmd_apply(db_path)
    else:
        return cmd_dry_run(db_path)


if __name__ == "__main__":
    sys.exit(main())
