#!/usr/bin/env python3
"""
Tests for validate_llm_output.py.

Creates a temporary SQLite database with known candidates and verifies:
- Candidates that SHOULD be caught are caught
- Candidates that SHOULD pass are NOT caught (tennis, calisthenics, etc.)
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import validate_llm_output
from pipeline.validate_llm_output import validate

# Redirect the module's state-file writes to a temp dir so this runner
# stops dirtying the git-tracked .last_validator_summary.json (WP5).
validate_llm_output.SCRIPT_DIR = Path(tempfile.mkdtemp(prefix="validator_tests_"))


def make_db(candidates: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115  path must outlive handle
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(db_path)
    # Full mirror of the real candidates schema (test-friendly defaults on
    # NOT NULL columns). Centralised in erlib.db during the 2026-07 refactor
    # WP3: until then, keep in sync with `.schema candidates`.
    conn.execute("""
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT DEFAULT '2026-01-01',
            name TEXT NOT NULL,
            date TEXT DEFAULT '2099-01-01',
            time TEXT, venue_name TEXT, venue_postcode TEXT, area TEXT,
            organiser TEXT, cost TEXT, description TEXT, format_type TEXT,
            source TEXT, url TEXT, score INTEGER, tier TEXT,
            signals_fired TEXT, past_pattern_match TEXT,
            travel_transit_min INTEGER, travel_cycle_min INTEGER,
            travel_walk_min INTEGER, travel_display TEXT, veto_reason TEXT,
            pipeline_state TEXT DEFAULT 'pending_llm', notion_page_id TEXT,
            verdict TEXT, verdict_reason TEXT, verdict_notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            travel_lookup_failed INTEGER DEFAULT 0,
            llm_tier TEXT, llm_reasoning TEXT, llm_reviewed TEXT,
            notion_status TEXT, synced_at TIMESTAMP NULL,
            source_snapshot TEXT, place_written INTEGER DEFAULT 0,
            needs_enrichment INTEGER DEFAULT 0, user_attended INTEGER DEFAULT 0,
            end_date TEXT, description_source TEXT
        )
    """)
    for c in candidates:
        conn.execute(
            """INSERT INTO candidates
               (id, name, description, llm_tier, llm_reasoning,
                pipeline_state, travel_display, travel_lookup_failed)
               VALUES (?, ?, ?, ?, ?, 'ready_to_write', ?, ?)""",
            (
                c["id"], c["name"], c.get("description", ""),
                c["llm_tier"], c.get("reasoning", ""),
                c.get("travel_display", "30 min (transit)"),
                c.get("travel_lookup_failed", 0),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test cases: SHOULD BE CAUGHT (validator should return exit 1)
# ---------------------------------------------------------------------------

SHOULD_CATCH = [
    {"id": 1, "name": "The Badminton Collective Newham Club Night",
     "llm_tier": "Borderline", "reasoning": "wrong sport but social"},
    {"id": 2, "name": "Friday Badminton Session at the Sports Hall from 7pm",
     "llm_tier": "Recommended", "reasoning": "all-levels badminton"},
    {"id": 3, "name": "Casual Football Wednesday 6:00pm",
     "description": "We play casual, free football.",
     "llm_tier": "Borderline", "reasoning": "casual football"},
    {"id": 4, "name": "London Volleyball Games & Social",
     "llm_tier": "Recommended", "reasoning": "active sport"},
    {"id": 5, "name": "Silent Writing Club: Riverside Sessions",
     "llm_tier": "Recommended", "reasoning": "quiet focused writing"},
    {"id": 6, "name": "Sunday Book Club: Project Hail Mary",
     "llm_tier": "Borderline", "reasoning": "interesting sci-fi"},
    {"id": 7, "name": "Board Games at The Bedford",
     "llm_tier": "Borderline", "reasoning": "social gaming"},
    {"id": 8, "name": "Dungeons & Dragons Sunday Meetup",
     "llm_tier": "Borderline", "reasoning": "social RPG"},
    {"id": 9, "name": "Mandarin Practice Social",
     "description": "A friendly language exchange for all levels.",
     "llm_tier": "Recommended", "reasoning": "social meetup"},
    {"id": 10, "name": "Singles Party in The City | Ages 30 to 45",
     "llm_tier": "Borderline", "reasoning": "age-appropriate social"},
    {"id": 11, "name": "London Philharmonic: Beethoven's 9th",
     "llm_tier": "Recommended", "reasoning": "cultural event"},
    {"id": 12, "name": "Eric Lu in Recital",
     "llm_tier": "Borderline", "reasoning": "piano concert"},
    {"id": 13, "name": "Britten Sinfonia: Baroque Sessions",
     "llm_tier": "Recommended", "reasoning": "music event"},
    {"id": 14, "name": "Speed Dating for 20s and 30s",
     "llm_tier": "Borderline", "reasoning": "age-appropriate"},
    {"id": 15, "name": "Sunday Cricket in the Park",
     "llm_tier": "Borderline", "reasoning": "outdoor social sport"},
    {"id": 16, "name": "Padel Tennis Beginners Session",
     "llm_tier": "Recommended", "reasoning": "beginner racquet sport"},
    {"id": 17, "name": "Warhammer 40K Painting Night",
     "llm_tier": "Borderline", "reasoning": "creative hobby"},
    {"id": 18, "name": "Netball Social League: All Welcome",
     "llm_tier": "Recommended", "reasoning": "beginner-friendly sport"},
    {"id": 19, "name": "Chamber Music at Wigmore Hall",
     "llm_tier": "Recommended", "reasoning": "intimate venue"},
    {"id": 20, "name": "Rugby 7s Social Tournament",
     "llm_tier": "Borderline", "reasoning": "social sport"},
]

# ---------------------------------------------------------------------------
# Test cases: SHOULD PASS (validator should NOT catch these)
# ---------------------------------------------------------------------------

SHOULD_PASS = [
    {"id": 101, "name": "Bouldering for All",
     "llm_tier": "Top Picks", "reasoning": "climbing, beginner-friendly"},
    {"id": 102, "name": "Beginner Calisthenics in the Park",
     "llm_tier": "Recommended", "reasoning": "strength training, outdoors"},
    {"id": 103, "name": "Resistance Training for Beginners",
     "llm_tier": "Recommended", "reasoning": "strength, beginner-friendly"},
    {"id": 104, "name": "Strength & Mobility Workshop",
     "llm_tier": "Recommended", "reasoning": "muscle-building, all levels"},
    {"id": 105, "name": "Vibe Coding Club | AI Agents",
     "llm_tier": "Top Picks", "reasoning": "hackday, social builders"},
    {"id": 106, "name": "Creative AI Meetup: May Edition",
     "llm_tier": "Top Picks", "reasoning": "AI community, hands-on"},
    {"id": 107, "name": "Free Sketch & Social",
     "llm_tier": "Top Picks", "reasoning": "hands-on creative"},
    {"id": 108, "name": "THE 7AM SUMMIT: tiny adventure & coffee",
     "llm_tier": "Recommended", "reasoning": "fun/weird social format"},
    {"id": 109, "name": "SWARM - London",
     "llm_tier": "Top Picks", "reasoning": "AI builders"},
    {"id": 110, "name": "Unconference: Context Engineering",
     "llm_tier": "Top Picks", "reasoning": "AI/tech community"},
    {"id": 111, "name": "Café sketching session",
     "llm_tier": "Top Picks", "reasoning": "hands-on creative, free"},
    {"id": 112, "name": "Queer Open Mic",
     "description": "Open mic night, all welcome, inclusive space.",
     "llm_tier": "Recommended", "reasoning": "inclusive, not identity-specific"},
    {"id": 113, "name": "Agents in Production: The Memory Problem",
     "llm_tier": "Recommended", "reasoning": "AI builders community"},
    {"id": 114, "name": "SuperPlay Regents Park: Outdoor Games",
     "llm_tier": "Top Picks", "reasoning": "fun/weird social format"},
    {"id": 115, "name": "Riverside Nature Reserve Bioblitz",
     "llm_tier": "Recommended", "reasoning": "nature, close to home"},
    # Edge case: description mentions sport incidentally but event isn't about it
    {"id": 116, "name": "Startup Stroll: Walk and Talk",
     "description": "Walk past the old football ground on our way to the pub.",
     "llm_tier": "Borderline", "reasoning": "networking walk"},
    # "Not Recommended" candidates should never be checked (already filtered)
    {"id": 117, "name": "Badminton Smash Tournament",
     "llm_tier": "Not Recommended", "reasoning": "wrong sport"},
]


def test_catches_violations():
    """Each SHOULD_CATCH candidate, tested individually, must be corrected."""
    failures = []
    for candidate in SHOULD_CATCH:
        db_path = make_db([candidate])
        validate(db_path, check_only=True)
        # In check_only mode, violations are reported but not corrected.
        # We verify detection by reading the output (result is always 0 now).
        # Instead, check that the candidate WOULD be corrected by running
        # without check_only and verifying the DB was updated.
        db_path.unlink()

        # Re-run with correction
        db_path = make_db([candidate])
        validate(db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT llm_tier FROM candidates WHERE id=?", (candidate["id"],)
        ).fetchone()
        conn.close()
        db_path.unlink()

        if row[0] != "Not Recommended":
            failures.append(
                f"  MISSED: ID {candidate['id']}: {candidate['name']!r} "
                f"(still tier={row[0]})"
            )
    return failures


def test_passes_clean():
    """All SHOULD_PASS candidates together must not be corrected."""
    # Track which candidates started as NOT "Not Recommended"
    passing_ids = [c["id"] for c in SHOULD_PASS if c["llm_tier"] != "Not Recommended"]
    db_path = make_db(SHOULD_PASS)
    validate(db_path)
    conn = sqlite3.connect(db_path)
    corrected = conn.execute(
        f"SELECT id, name FROM candidates "
        f"WHERE id IN ({','.join('?' * len(passing_ids))}) "
        f"AND llm_tier = 'Not Recommended'",
        passing_ids,
    ).fetchall()
    conn.close()
    db_path.unlink()
    if corrected:
        names = [f"ID {r[0]}: {r[1]}" for r in corrected]
        return [f"  WRONGLY CORRECTED: {n}" for n in names]
    return []


def test_mixed_db_corrects_bad_passes_good():
    """A DB with both good and bad: bad are corrected, good are untouched."""
    mixed = SHOULD_PASS + SHOULD_CATCH[:3]
    db_path = make_db(mixed)
    validate(db_path)
    conn = sqlite3.connect(db_path)

    # Bad ones should now be Not Recommended
    bad_ids = [c["id"] for c in SHOULD_CATCH[:3]]
    for bid in bad_ids:
        row = conn.execute(
            "SELECT llm_tier FROM candidates WHERE id=?", (bid,)
        ).fetchone()
        if row[0] != "Not Recommended":
            conn.close()
            db_path.unlink()
            return [f"  MISSED: ID {bid} not corrected in mixed DB"]

    # Good ones should be unchanged (only check those that weren't already Not Recommended)
    good_ids = [c["id"] for c in SHOULD_PASS if c["llm_tier"] != "Not Recommended"]
    corrected_good = conn.execute(
        f"SELECT id, name FROM candidates WHERE id IN ({','.join('?' * len(good_ids))}) "
        f"AND llm_tier = 'Not Recommended'",
        good_ids,
    ).fetchall()
    conn.close()
    db_path.unlink()

    if corrected_good:
        return [f"  WRONGLY CORRECTED: ID {r[0]} {r[1]}" for r in corrected_good]
    return []


def test_empty_db():
    """An empty eligible set should pass cleanly."""
    db_path = make_db([])
    result = validate(db_path)
    db_path.unlink()
    if result != 0:
        return ["  WRONGLY FAILED: empty DB should pass"]
    return []


if __name__ == "__main__":
    import io
    from contextlib import redirect_stdout

    all_failures = []

    print("=" * 70)
    print("TEST: Each SHOULD_CATCH candidate triggers a violation")
    print("=" * 70)
    with redirect_stdout(io.StringIO()):
        failures = test_catches_violations()
    if failures:
        print(f"FAIL: {len(failures)} candidate(s) not caught:\n")
        for f in failures:
            print(f)
        all_failures.extend(failures)
    else:
        print(f"PASS: all {len(SHOULD_CATCH)} violations caught")

    print()
    print("=" * 70)
    print("TEST: SHOULD_PASS candidates do NOT trigger violations")
    print("=" * 70)
    with redirect_stdout(io.StringIO()):
        failures = test_passes_clean()
    if failures:
        print("FAIL:\n")
        for f in failures:
            print(f)
        all_failures.extend(failures)
    else:
        print(f"PASS: all {len(SHOULD_PASS)} clean candidates passed")

    print()
    print("=" * 70)
    print("TEST: Mixed DB corrects bad, passes good")
    print("=" * 70)
    with redirect_stdout(io.StringIO()):
        failures = test_mixed_db_corrects_bad_passes_good()
    if failures:
        print("FAIL:\n")
        for f in failures:
            print(f)
        all_failures.extend(failures)
    else:
        print("PASS: bad corrected, good untouched")

    print()
    print("=" * 70)
    print("TEST: Empty DB passes cleanly")
    print("=" * 70)
    with redirect_stdout(io.StringIO()):
        failures = test_empty_db()
    if failures:
        print("FAIL:\n")
        for f in failures:
            print(f)
        all_failures.extend(failures)
    else:
        print("PASS: empty DB passed cleanly")

    print()
    print("=" * 70)
    if all_failures:
        print(f"OVERALL: FAIL: {len(all_failures)} issue(s)")
        sys.exit(1)
    else:
        print("OVERALL: PASS: all tests green")
        sys.exit(0)
