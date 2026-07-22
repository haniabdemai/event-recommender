#!/usr/bin/env python3
"""Seed the database with synthetic demo events.

Gives a fresh clone something to score so you can watch the pipeline work
before wiring up any API:

    python3 scripts/init_db.py
    python3 seed_demo_data.py
    python3 pipeline/score_candidates.py
    python3 pipeline/llm_sense_check.py --prepare

The events are invented (fictional organisers matching the example persona
in references/taste-profile-template.md) and dated relative to today, so
the funnel always has future events to process. The mix is deliberate:
some hard-vetoed, some high scorers, a borderline, and one with no
description ("Couldn't Process"): one of each pipeline outcome.

Usage:
    python3 seed_demo_data.py [--db PATH] [--fresh]

--fresh deletes previously seeded demo rows (matched by the demo URL
domain) before inserting, so re-runs don't pile up duplicates.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO))
from erlib import db as erdb  # noqa: E402
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402

DEMO_DOMAIN = "events.example"


def _demo_events(today: date) -> list[dict]:
    def on(days: int) -> str:
        return (today + timedelta(days=days)).isoformat()

    return [
        # Expected: Top Picks territory: AI/tech + hands-on + known organiser.
        {"name": "Vibe Coding Club: Build a Tiny AI Agent",
         "date": on(5), "time": "18:30", "venue_name": "The Old Print Works",
         "venue_postcode": "E2 8AA", "organiser": "Vibe Coding Club",
         "cost": "Free", "format_type": "Workshop",
         "description": ("Hands-on session: bring your laptop and build a "
                         "small AI agent together. Beginners welcome, small "
                         "groups, demos at the end.")},
        # Expected: Recommended: creative activity + cheap.
        {"name": "Drop-in Pottery Evening",
         "date": on(7), "time": "19:00", "venue_name": "Clay Corner Studio",
         "venue_postcode": "SE15 4QL", "organiser": "The Print Room Studio",
         "cost": "£12", "format_type": "Creative social",
         "description": ("Throw a pot, meet people, take your piece home. "
                         "All materials provided, no experience needed.")},
        # Expected: Recommended: the persona's sport, known organiser.
        {"name": "Beginner Bouldering Social",
         "date": on(9), "time": "19:30", "venue_name": "The Boulder Room",
         "venue_postcode": "E9 6PP", "organiser": "The Boulder Room",
         "cost": "£15", "format_type": "Social meetup",
         "description": ("Coached intro to bouldering followed by pizza. "
                         "All levels, shoes included.")},
        # Expected: known series fires (Art Jam).
        {"name": "Art Jam: Collage Edition",
         "date": on(12), "time": "14:00", "venue_name": "Corner Gallery Cafe",
         "venue_postcode": "N1 7AA", "organiser": "Art Jam Studio",
         "cost": "£10", "format_type": "Creative social",
         "description": "Drop-in collage jam, materials provided."},
        # Expected: VETOED: wrong sport (tennis).
        {"name": "Tennis for All: Beginners Welcome",
         "date": on(6), "time": "10:00", "venue_name": "Community Courts",
         "venue_postcode": "SW4 9DE", "organiser": "City Tennis Club",
         "cost": "£15", "format_type": "Social meetup",
         "description": "Coaching-led tennis session for all levels."},
        # Expected: VETOED: pub quiz.
        {"name": "Big Thursday Pub Quiz",
         "date": on(8), "time": "20:00", "venue_name": "The Anchor",
         "venue_postcode": "SE1 9PP", "organiser": "The Quiz Masters",
         "cost": "Free", "format_type": "Social meetup",
         "description": "Weekly pub quiz, teams of 4-6, cash prizes."},
        # Expected: VETOED: blocked organiser.
        {"name": "Live Music Night",
         "date": on(10), "time": "20:00", "venue_name": "The Castle",
         "venue_postcode": "EC1V 8AB", "organiser": "The Hype Collective",
         "cost": "Free", "format_type": "Concert",
         "description": "Live band night with social drinks."},
        # Expected: PASSES veto: classical crossover exemption.
        {"name": "Orchestra Meets Jazz: Crossover Night",
         "date": on(15), "time": "19:30", "venue_name": "Riverside Hall",
         "venue_postcode": "SE1 8XX", "organiser": "Riverside Hall",
         "cost": "£18", "format_type": "Concert",
         "description": ("A symphony orchestra joins a live jazz quartet "
                         "for an improvised crossover set.")},
        # Expected: Borderline: passive format, nothing aligned.
        {"name": "An Evening Lecture on Bridges",
         "date": on(11), "time": "18:30", "venue_name": "Civic Hall",
         "venue_postcode": "WC1A 1AA", "organiser": "Civic Society",
         "cost": "Free", "format_type": "Talk",
         "description": "A lecture about the city's bridges."},
        # Expected: Couldn't Process: no description to judge.
        {"name": "Monthly Community Gathering",
         "date": on(13), "time": "14:00", "venue_name": "Community Hall",
         "venue_postcode": "N4 2BB", "organiser": "Local Community Group",
         "cost": "£5", "format_type": "",
         "description": ""},
    ]


def seed(conn, today: date | None = None, fresh: bool = False) -> int:
    today = today or date.today()
    if fresh:
        conn.execute("DELETE FROM candidates WHERE url LIKE ?",
                     (f"https://{DEMO_DOMAIN}/%",))
    inserted = 0
    for i, ev in enumerate(_demo_events(today), start=1):
        url = f"https://{DEMO_DOMAIN}/{i}"
        dup = conn.execute("SELECT 1 FROM candidates WHERE url = ?",
                           (url,)).fetchone()
        if dup:
            continue
        conn.execute(
            """INSERT INTO candidates
               (run_date, name, date, time, venue_name, venue_postcode,
                organiser, cost, description, format_type, source, url,
                pipeline_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Meetup', ?,
                       'pending_llm')""",
            (today.isoformat(), ev["name"], ev["date"], ev["time"],
             ev["venue_name"], ev["venue_postcode"], ev["organiser"],
             ev["cost"], ev["description"], ev["format_type"], url))
        inserted += 1
    conn.commit()
    return inserted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    ap.add_argument("--fresh", action="store_true",
                    help="Delete previously seeded demo rows first")
    args = ap.parse_args()

    conn = erdb.connect(args.db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "candidates" not in tables:
        print("No schema yet: run scripts/init_db.py first.", file=sys.stderr)
        return 1
    n = seed(conn, fresh=args.fresh)
    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    print(f"Seeded {n} demo events ({total} candidates total).")
    print("Next: python3 pipeline/score_candidates.py")
    conn.close()
    return 0


def _self_test() -> int:
    """Seed a temp DB, score it, and pin the expected funnel outcomes."""
    import sqlite3

    from pipeline import score_candidates as sc  # noqa: PLC0415

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
    erdb.init_schema(conn)
    n = seed(conn)
    check("all demo events inserted", n, len(_demo_events(date.today())))
    check("idempotent re-seed inserts nothing", seed(conn), 0)

    outcomes = {}
    for row in conn.execute("SELECT * FROM candidates").fetchall():
        c = dict(row)
        vetoed, reason = sc.check_veto(c)
        if vetoed:
            outcomes[c["name"]] = f"veto:{reason}"
        else:
            score, _, _ = sc.score_candidate(c)
            outcomes[c["name"]] = sc.assign_tier(score)

    check("vibe coding is Top Picks",
          outcomes["Vibe Coding Club: Build a Tiny AI Agent"], "Top Picks")
    check("tennis vetoed",
          outcomes["Tennis for All: Beginners Welcome"], "veto:wrong_sports")
    check("pub quiz vetoed",
          outcomes["Big Thursday Pub Quiz"], "veto:pub_quiz")
    check("blocked organiser vetoed",
          outcomes["Live Music Night"], "veto:blocked_organiser")
    check("classical crossover passes veto",
          outcomes["Orchestra Meets Jazz: Crossover Night"].startswith("veto:"),
          False)
    check("lecture is Borderline",
          outcomes["An Evening Lecture on Bridges"], "Borderline")
    check("pottery reaches a recommended tier",
          outcomes["Drop-in Pottery Evening"] in ("Recommended", "Top Picks"),
          True)
    check("bouldering not vetoed",
          outcomes["Beginner Bouldering Social"].startswith("veto:"), False)

    print("All passed" if ok else "FAILURES", file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
