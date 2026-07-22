#!/usr/bin/env python3
"""
Post-LLM validation gate. Runs after llm_sense_check.py, before write_notion.py.

Checks that the LLM correctly applied the taste profile's hard vetoes and
explicit exclusions. Candidates that match hard-veto patterns are auto-corrected
to Not Recommended and excluded from the Notion write (pipeline_state set to
llm_rejected). The pipeline continues with the corrected data.

This is a canary, not a replacement for the LLM. It checks a small set of
patterns that are unambiguously wrong per the taste profile. Over time, add
new patterns as failures are observed.

Exit codes:
    0 = all checks passed (or violations auto-corrected), safe to proceed
    3 = travel gaps found, candidates have a venue but no travel data
"""

# Runnable as `python3 pipeline/<name>.py` or importable as a module:
# put the repo root (for erlib) and this package dir (for sibling
# modules) on sys.path before the repo imports below.
import sys as _sys
import pathlib as _pl
_r = _pl.Path(__file__).resolve().parent.parent
for _p in (str(_r / "pipeline"), str(_r)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from erlib import db as erdb
from erlib.config import DB_PATH
from erlib.constants import BORDERLINE_CAP, MIN_FOR_CAP
from erlib.freshness import stamp

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)

# ---------------------------------------------------------------------------
# Validation rules
#
# Each rule: (compiled regex, scope, reason)
# scope: "name" = match against name only, "all" = match against name + description
#
# IMPORTANT: These are patterns that the LLM should ALWAYS veto per the taste
# profile. They are not exhaustive: the LLM handles nuance. These are the
# floor: if the LLM can't even get these right, stop the pipeline.
# ---------------------------------------------------------------------------

MUST_BE_NOT_RECOMMENDED = [
    # --- Wrong sports (taste profile: climbing/bouldering are the only
    # positive sports for the example persona) ---
    # NOTE: "climbing", "bouldering" are WANTED: never add those here.
    (re.compile(r"\bbadminton\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bfootball\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bsoccer\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bvolleyball\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bcricket\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\brugby\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bnetball\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bbasketball\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bpadel\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bping\s*pong\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\btable\s*tennis\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\bsquash\b", re.I), "name", "wrong sport: not climbing"),
    (re.compile(r"\btennis\b", re.I), "name", "wrong sport: not climbing"),

    # --- Hard vetoes explicitly named in taste profile ---
    (re.compile(r"\bsilent\s*writing\b", re.I), "name", "writing group: hard veto"),
    (re.compile(r"\bwriting\s*sprint\b", re.I), "name", "writing group: hard veto"),
    (re.compile(r"\bbook\s*club\b", re.I), "name", "book club: hard veto"),
    (re.compile(r"\bboard\s*games?\b", re.I), "name", "tabletop/board games: hard veto"),
    (re.compile(r"\bdungeons\b", re.I), "name", "D&D/tabletop: hard veto"),
    (re.compile(r"\bwarhammer\b", re.I), "name", "tabletop gaming: hard veto"),
    (re.compile(r"\blanguage\s*exchange\b", re.I), "all", "language learning: hard veto"),
    (re.compile(r"\bspeed\s*dating\b", re.I), "all", "dating event: hard veto"),
    (re.compile(r"\bsingles\s*night\b", re.I), "name", "dating event: hard veto"),
    (re.compile(r"\bsingles\s*party\b", re.I), "name", "dating event: hard veto"),

    # --- Classical music (taste profile hard veto) ---
    (re.compile(r"\bsymphony\b", re.I), "name", "classical concert: hard veto"),
    (re.compile(r"\brecital\b", re.I), "name", "classical concert: hard veto"),
    (re.compile(r"\bchamber\s*music\b", re.I), "name", "classical concert: hard veto"),
    (re.compile(r"\bphilharmonic\b", re.I), "name", "classical concert: hard veto"),
    (re.compile(r"\bsinfonia\b", re.I), "name", "classical concert: hard veto"),
    (re.compile(r"\bopera\b", re.I), "name", "opera: hard veto"),

    # --- feedback-audit additions ---
    (re.compile(r"\bpub\s*quiz\b", re.I), "name", "pub quiz: hard veto"),
    (re.compile(r"\btrivia\s*night\b", re.I), "name", "trivia night: hard veto"),
    (re.compile(r"\bkaraoke\b", re.I), "name", "karaoke: hard veto"),
    (re.compile(r"\bescape\s*room\b", re.I), "name", "escape room: hard veto"),
    (re.compile(r"\bpub\s*crawl\b", re.I), "name", "pub crawl: hard veto"),
    (re.compile(r"\bbar\s*crawl\b", re.I), "name", "bar crawl: hard veto"),
    (re.compile(r"\bdj\s*workshop\b", re.I), "name", "DJ practice: hard veto"),
    (re.compile(r"\bopen\s*decks\b", re.I), "name", "DJ practice: hard veto"),
    (re.compile(r"\bwalking\s*tour\b", re.I), "name", "London walk: hard veto"),
    (re.compile(r"\bthames\s*(walk|path)\b", re.I), "name", "London walk: hard veto"),
    (re.compile(r"\bcanal\s*walk\b", re.I), "name", "London walk: hard veto"),
    (re.compile(r"\bdarts\b", re.I), "name", "darts: hard veto"),
    # (padel already covered by the wrong-sport rule above; the duplicate
    # entry here could never fire: removed in WP5)
    (re.compile(r"\b30s\s*[&and-]+\s*40s\b", re.I), "all", "30s-40s framing: hard veto"),
    (re.compile(r"\byoung\s*professionals?\b", re.I), "name", "young professionals: hard veto"),
]

PAST_REFERENCE_RE = re.compile(
    r"attend|been to|went to|visited|previously|familiar|the user has|you've been|"
    r"series.{0,20}before|organiser.{0,20}before|venue.{0,20}before",
    re.IGNORECASE,
)


def _past_connection_note(signals_fired: str, organiser: str, venue_name: str) -> str | None:
    """Build a validator note for past-pattern signals 31/32."""
    if not signals_fired:
        return None
    try:
        parsed = json.loads(signals_fired)
    except (json.JSONDecodeError, TypeError):
        return None
    parts = []
    for s in parsed:
        if "Signal #31" in s:
            parts.append("The user has attended this event series before")
        elif "Known venue-as-organiser" in s:
            v = venue_name or "this venue"
            parts.append(f"The user has been to events at {v} before")
        elif "Signal #32" in s:
            o = organiser or "this organiser"
            parts.append(f"The user has been to events by {o} before")
    if not parts:
        return None
    return f" [Validator: {'; '.join(parts)}]"


MUST_BE_NOT_RECOMMENDED_ORGANISERS = [
    # Fictional examples matching VETO_PATTERNS["blocked_organiser"]: keep
    # this list in sync with yours.
    (re.compile(r"(?:the\s*)?hype\s*collective", re.I), "blocked organiser: The Hype Collective"),
    (re.compile(r"megamix\s*socials?", re.I), "blocked organiser: Megamix Socials"),
    (re.compile(r"gallery\s*wander\s*club", re.I), "blocked organiser: Gallery Wander Club"),
]


SEASONAL_UNIQUE_KEYWORDS = re.compile(
    r"\b(?:lavender|strawberry|cherry blossom|fireworks|bonfire|outdoor cinema|"
    r"festival|open[- ]air|harvest|pumpkin|christmas market|winter wonderland|"
    r"summer fair|spring fair|sculpture trail|light show|lantern|"
    r"one[- ]off|inaugural|limited[- ]run|special edition|first ever|farewell)\b",
    re.IGNORECASE,
)

TRAVEL_REJECTION_RE = re.compile(
    r"\b(?:travel|transit|journey|commute|too far|distance|"
    r"\d+\s*min(?:ute)?s?\s*(?:away|each way|travel|transit))\b",
    re.IGNORECASE,
)

# Independent non-travel rejection signals: if the reasoning contains these,
# the event was rejected for reasons beyond just travel distance.
OTHER_CONCERN_RE = re.compile(
    r"\b(?:wrong crowd|wrong vibe|not (?:in|among) .{0,30}interest|"
    r"passive .{0,20}format|veto [A-W]|hard no|"
    r"wrong (?:age|sport|community))\b",
    re.IGNORECASE,
)


def validate(db_path: Path, *, check_only: bool = False) -> int:
    conn = erdb.connect(db_path)

    rows = conn.execute("""
        SELECT id, name, description, organiser, llm_tier, llm_reasoning,
               signals_fired, venue_name, COALESCE(score, 0) AS py_score
        FROM candidates
        WHERE pipeline_state IN ('ready_to_write', 'pending_travel')
          AND llm_tier IN ('Top Picks', 'Recommended', 'Borderline')
    """).fetchall()

    violations = []

    for row in rows:
        name = row["name"] or ""
        desc = row["description"] or ""
        org = row["organiser"] or ""
        found = False

        for pattern, scope, reason in MUST_BE_NOT_RECOMMENDED:
            text = name if scope == "name" else f"{name} {desc}"
            if pattern.search(text):
                violations.append({
                    "id": row["id"],
                    "name": name,
                    "llm_tier": row["llm_tier"],
                    "llm_reasoning": row["llm_reasoning"],
                    "rule": reason,
                })
                found = True
                break

        if not found:
            for pattern, reason in MUST_BE_NOT_RECOMMENDED_ORGANISERS:
                if pattern.search(org):
                    violations.append({
                        "id": row["id"],
                        "name": name,
                        "llm_tier": row["llm_tier"],
                        "llm_reasoning": row["llm_reasoning"],
                        "rule": reason,
                    })
                    break

    summary = {
        "checked": len(rows),
        "corrections": len(violations),
        "hard_veto_count": len(violations),
        "cap_demotion_count": 0,
        "details": [{"id": v["id"], "name": v["name"], "was": v["llm_tier"], "rule": v["rule"]} for v in violations],
    }

    if violations:
        print(f"VALIDATE_CORRECTED: {len(violations)} candidate(s) the LLM got wrong.\n")
        for v in violations:
            print(f"  ID {v['id']}: {v['name']}")
            print(f"    LLM gave: {v['llm_tier']}: \"{v['llm_reasoning']}\"")
            print(f"    Rule: {v['rule']}")
            if not check_only:
                conn.execute(
                    """UPDATE candidates
                       SET llm_tier = 'Not Recommended',
                           llm_reasoning = ?,
                           pipeline_state = 'llm_rejected'
                       WHERE id = ?""",
                    (
                        f"VALIDATOR OVERRIDE ({v['rule']}). "
                        f"Original LLM tier: {v['llm_tier']}. "
                        f"Original reasoning: {v['llm_reasoning']}",
                        v["id"],
                    ),
                )
                print("    -> Corrected to 'Not Recommended'")
            print()
        if not check_only:
            conn.commit()

    violated_ids = {v["id"] for v in violations}
    tier_dist = {}
    for row in rows:
        if row["id"] in violated_ids:
            continue
        t = row["llm_tier"]
        tier_dist[t] = tier_dist.get(t, 0) + 1
    total_accepted = sum(tier_dist.values())
    borderline_count = tier_dist.get("Borderline", 0)


    if total_accepted > MIN_FOR_CAP and borderline_count / total_accepted > BORDERLINE_CAP:
        pct = round(borderline_count / total_accepted * 100, 1)
        print(f"VALIDATE_WARN: {borderline_count}/{total_accepted} accepted candidates "
              f"({pct}%) are Borderline: exceeds 30% cap.",
              file=sys.stderr)
        summary["borderline_warning"] = {
            "borderline": borderline_count,
            "total_accepted": total_accepted,
            "pct": pct,
        }

        excess = math.ceil(
            (borderline_count - BORDERLINE_CAP * total_accepted) / (1 - BORDERLINE_CAP)
        )
        excess = min(excess, borderline_count)

        borderline_rows = [r for r in rows
                           if r["id"] not in violated_ids
                           and r["llm_tier"] == "Borderline"]
        borderline_rows.sort(key=lambda r: (r["py_score"], r["id"]))

        demoted = []
        for row in borderline_rows[:excess]:
            demoted.append({
                "id": row["id"],
                "name": row["name"] or "",
                "py_score": row["py_score"],
                "reasoning": row["llm_reasoning"] or "",
            })
            if not check_only:
                conn.execute(
                    """UPDATE candidates
                       SET llm_tier = 'Not Recommended',
                           llm_reasoning = ?,
                           pipeline_state = 'llm_rejected'
                       WHERE id = ?""",
                    (
                        f"BORDERLINE CAP ({borderline_count}/{total_accepted} = "
                        f"{pct}%, cap 30%). Demoted: lowest Python score "
                        f"({row['py_score']}). Original reasoning: "
                        f"{row['llm_reasoning'] or ''}",
                        row["id"],
                    ),
                )

        if demoted:
            if not check_only:
                conn.commit()
            action = "Would demote" if check_only else "Demoted"
            print(f"\nBORDERLINE_CAP_ENFORCED: {action} {len(demoted)} candidate(s) "
                  f"({borderline_count}/{total_accepted} = {pct}%, cap 30%).\n")
            for d in demoted:
                print(f"  ID {d['id']}: {d['name'][:60]} (Python score: {d['py_score']})")
            summary["corrections"] = summary.get("corrections", 0) + len(demoted)
            summary["cap_demotion_count"] = len(demoted)
            summary["borderline_cap_enforced"] = {
                "demoted": len(demoted),
                "pre_enforcement_borderline": borderline_count,
                "total_accepted": total_accepted,
                "pct_before": pct,
                "demoted_ids": [d["id"] for d in demoted],
            }

    # Past-pattern check: when Signal #31/#32 fired but reasoning doesn't
    # mention past attendance, append a validator note so the pipeline report
    # carries the context.
    past_pattern_notes = []
    for row in rows:
        signals = row["signals_fired"] or ""
        if "Signal #31" not in signals and "Signal #32" not in signals:
            continue
        reasoning = row["llm_reasoning"] or ""
        if PAST_REFERENCE_RE.search(reasoning):
            continue
        note = _past_connection_note(signals, row["organiser"] or "", row["venue_name"] or "")
        if note:
            past_pattern_notes.append({"id": row["id"], "name": row["name"], "note": note})
            if not check_only:
                conn.execute(
                    "UPDATE candidates SET llm_reasoning = ? WHERE id = ?",
                    (reasoning + note, row["id"]),
                )

    if past_pattern_notes:
        print(f"\nPAST_PATTERN: {len(past_pattern_notes)} candidate(s) had past-pattern "
              f"signals but reasoning didn't mention past attendance: validator note appended.\n")
        for pp in past_pattern_notes:
            print(f"  ID {pp['id']}: {pp['name']}")
            print(f"    Added: {pp['note']}")
        if not check_only:
            conn.commit()
        summary["past_pattern_notes"] = [
            {"id": pp["id"], "name": pp["name"]} for pp in past_pattern_notes
        ]

    # Travel-rejection recovery: LLM said Not Recommended with travel as the
    # primary concern, but the event has seasonal/unique keywords. Promote to
    # Borderline: travel alone should never be a hard rejection.
    # run_date scoping (audit P0-4): only reconsider candidates rejected in
    # the CURRENT run. Without it, every historical travel rejection with a
    # future event date was re-promoted on every validator pass.
    travel_recoveries = conn.execute("""
        SELECT id, name, description, llm_tier, llm_reasoning,
               travel_display, COALESCE(score, 0) AS py_score
        FROM candidates
        WHERE pipeline_state = 'llm_rejected'
          AND llm_tier = 'Not Recommended'
          AND run_date = date('now')
          AND COALESCE(end_date, date) >= date('now')
    """).fetchall()

    recovered = []
    for row in travel_recoveries:
        reasoning = row["llm_reasoning"] or ""
        desc = row["description"] or ""
        name = row["name"] or ""
        full_text = f"{name} {desc}"

        if not TRAVEL_REJECTION_RE.search(reasoning):
            continue
        if OTHER_CONCERN_RE.search(reasoning):
            continue
        if not SEASONAL_UNIQUE_KEYWORDS.search(full_text):
            continue

        recovered.append({
            "id": row["id"],
            "name": name,
            "travel": row["travel_display"] or "unknown",
            "original_reasoning": reasoning,
        })
        if not check_only:
            conn.execute(
                """UPDATE candidates
                   SET llm_tier = 'Borderline',
                       llm_reasoning = ?,
                       pipeline_state = 'ready_to_write'
                   WHERE id = ?""",
                (
                    f"VALIDATOR RECOVERY (seasonal/unique event, travel-only rejection). "
                    f"Promoted to Borderline. Original: {reasoning}",
                    row["id"],
                ),
            )

    if recovered:
        if not check_only:
            conn.commit()
        action = "Would recover" if check_only else "Recovered"
        print(f"\nTRAVEL_RECOVERY: {action} {len(recovered)} seasonal/unique event(s) "
              f"from travel-only rejection:\n")
        for r in recovered:
            print(f"  ID {r['id']}: {r['name'][:60]} | {r['travel']}")
        summary["travel_recoveries"] = [
            {"id": r["id"], "name": r["name"], "travel": r["travel"]}
            for r in recovered
        ]

    travel_gaps = conn.execute("""
        SELECT id, name, venue_name, venue_postcode
        FROM candidates
        WHERE pipeline_state IN ('ready_to_write', 'pending_travel')
          AND (travel_display IS NULL OR travel_display = '')
          AND travel_lookup_failed != 1
          AND venue_name IS NOT NULL AND venue_name != ''
          AND LOWER(venue_name) NOT IN ('unknown', 'tbc', 'tba', 'n/a')
    """).fetchall()

    if travel_gaps:
        print(f"\nTRAVEL_GAPS: {len(travel_gaps)} candidate(s) have a venue but no travel data:\n")
        for g in travel_gaps:
            print(f"  ID {g['id']}: {g['name'][:50]} | venue: {g['venue_name']}")
        summary["travel_gaps"] = [
            {"id": g["id"], "name": g["name"], "venue": g["venue_name"]}
            for g in travel_gaps
        ]

    exit_code = 3 if travel_gaps else 0

    enforced = summary.get("borderline_cap_enforced")
    if not violations and not travel_gaps and not enforced:
        print(f"VALIDATE_OK: {len(rows)} candidates checked, all passed.")

    summary_path = SCRIPT_DIR / ".last_validator_summary.json"
    with open(summary_path, "w") as f:
        json.dump(stamp(summary), f, indent=2)

    conn.close()
    return exit_code


def _smoke_tests() -> int:
    # Redirect state-file writes to a temp dir: the smoke suite used to
    # overwrite the git-tracked .last_validator_summary.json on every
    # `make test` run (WP5). The process exits after the suite, so the
    # global needs no restore.
    global SCRIPT_DIR
    SCRIPT_DIR = Path(tempfile.mkdtemp(prefix="validator_smoke_"))
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    # _past_connection_note
    note_s31 = _past_connection_note(
        '["Signal #31: Known series"]', "Compiler", "")
    check("s31 note mentions series",
          note_s31 is not None and "event series" in note_s31, True)
    check("s31 note has Validator prefix",
          note_s31 is not None and "[Validator:" in note_s31, True)

    note_s32 = _past_connection_note(
        '["Signal #32: Known organiser"]', "code-and-chill", "")
    check("s32 note names organiser",
          note_s32 is not None and "code-and-chill" in note_s32, True)

    note_venue = _past_connection_note(
        '["Signal #32: Known venue-as-organiser"]', "", "Maker House")
    check("venue note names venue",
          note_venue is not None and "Maker House" in note_venue, True)

    note_none = _past_connection_note(
        '["Signal #1: AI/tech community"]', "Org", "Venue")
    check("no past signal gives None", note_none, None)

    note_empty = _past_connection_note(None, "", "")
    check("null signals gives None", note_empty, None)

    # PAST_REFERENCE_RE
    check("regex matches 'attended'",
          bool(PAST_REFERENCE_RE.search("They attended this before")), True)
    check("regex matches 'been to'",
          bool(PAST_REFERENCE_RE.search("The user has been to events by them")), True)
    check("regex matches 'previously'",
          bool(PAST_REFERENCE_RE.search("Previously attended similar")), True)
    check("regex matches 'visited'",
          bool(PAST_REFERENCE_RE.search("They visited this venue")), True)
    check("regex misses generic text",
          bool(PAST_REFERENCE_RE.search("Great hands-on AI workshop")), False)
    check("regex misses vague 'your kind'",
          bool(PAST_REFERENCE_RE.search("your kind of AI community gathering")), False)

    # --- Borderline cap enforcement (DB-backed) ---
    def _make_test_db(candidates):
        """Create temp DB with minimal candidates schema, return path."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        c = sqlite3.connect(path)
        # Full mirror of the real candidates schema (test-friendly defaults on
        # NOT NULL columns). Centralised in erlib.db during the 2026-07 refactor
        # WP3: until then, keep in sync with `.schema candidates`.
        c.execute("""CREATE TABLE candidates (
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
        )""")
        for row in candidates:
            c.execute(
                """INSERT INTO candidates
                   (id, name, llm_tier, llm_reasoning, score, pipeline_state,
                    travel_display, description, organiser, signals_fired, venue_name)
                   VALUES (?, ?, ?, ?, ?, 'ready_to_write', 'Bus 20 min',
                           '', '', '[]', '')""",
                (row["id"], row["name"], row["tier"], row.get("reasoning", "OK"),
                 row["score"]),
            )
        c.commit()
        c.close()
        return Path(path)

    # Test: >30% Borderline → enforcement demotes lowest-score candidates
    test_candidates = [
        {"id": 1, "name": "Top A", "tier": "Top Picks", "score": 10},
        {"id": 2, "name": "Rec A", "tier": "Recommended", "score": 8},
        {"id": 3, "name": "Rec B", "tier": "Recommended", "score": 7},
        {"id": 4, "name": "Rec C", "tier": "Recommended", "score": 6},
        {"id": 5, "name": "BL low", "tier": "Borderline", "score": 1},
        {"id": 6, "name": "BL mid", "tier": "Borderline", "score": 3},
        {"id": 7, "name": "BL high", "tier": "Borderline", "score": 5},
        {"id": 8, "name": "BL top", "tier": "Borderline", "score": 7},
        {"id": 9, "name": "Rec D", "tier": "Recommended", "score": 9},
        {"id": 10, "name": "BL five", "tier": "Borderline", "score": 2},
        {"id": 11, "name": "Rec E", "tier": "Recommended", "score": 5},
    ]
    # 11 accepted, 5 Borderline = 45.5%.
    # excess = ceil((5 - 0.30*11) / 0.70) = ceil(2.43) = 3.
    # Lowest scores: id=5 (1), id=10 (2), id=6 (3). Those three demoted.
    # After: T'=8, B'=2, 2/8=25% ≤ 30%.
    db_path = _make_test_db(test_candidates)
    try:
        validate(db_path, check_only=False)
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        demoted_rows = c.execute(
            "SELECT id FROM candidates WHERE pipeline_state = 'llm_rejected' ORDER BY id"
        ).fetchall()
        demoted_ids = [r["id"] for r in demoted_rows]
        check("enforcement demotes exactly 3", len(demoted_ids), 3)
        check("enforcement demotes lowest scores (id=5,6,10)",
              demoted_ids, [5, 6, 10])

        kept = c.execute(
            "SELECT id FROM candidates WHERE llm_tier = 'Borderline' "
            "AND pipeline_state = 'ready_to_write' ORDER BY id"
        ).fetchall()
        check("enforcement keeps 2 Borderlines", len(kept), 2)
        check("kept Borderlines are highest scores",
              [r["id"] for r in kept], [7, 8])

        remaining_accepted = c.execute(
            "SELECT COUNT(*) FROM candidates "
            "WHERE pipeline_state IN ('ready_to_write', 'pending_travel')"
        ).fetchone()[0]
        remaining_bl = len(kept)
        post_pct = remaining_bl / remaining_accepted if remaining_accepted else 0
        check("post-enforcement Borderline ≤ 30%", post_pct <= 0.30, True)
        c.close()
    finally:
        os.unlink(db_path)

    # Test: <30% Borderline → no enforcement
    safe_candidates = [
        {"id": 1, "name": "Top A", "tier": "Top Picks", "score": 10},
        {"id": 2, "name": "Rec A", "tier": "Recommended", "score": 8},
        {"id": 3, "name": "Rec B", "tier": "Recommended", "score": 7},
        {"id": 4, "name": "Rec C", "tier": "Recommended", "score": 6},
        {"id": 5, "name": "Rec D", "tier": "Recommended", "score": 5},
        {"id": 6, "name": "Rec E", "tier": "Recommended", "score": 4},
        {"id": 7, "name": "Rec F", "tier": "Recommended", "score": 3},
        {"id": 8, "name": "Rec G", "tier": "Recommended", "score": 2},
        {"id": 9, "name": "BL A", "tier": "Borderline", "score": 1},
        {"id": 10, "name": "BL B", "tier": "Borderline", "score": 2},
        {"id": 11, "name": "BL C", "tier": "Borderline", "score": 3},
    ]
    # 11 accepted, 3 Borderline = 27.3% → below 30%, no enforcement
    db_path = _make_test_db(safe_candidates)
    try:
        validate(db_path, check_only=False)
        c = sqlite3.connect(str(db_path))
        rejected = c.execute(
            "SELECT COUNT(*) FROM candidates WHERE pipeline_state = 'llm_rejected'"
        ).fetchone()[0]
        check("no enforcement below 30%", rejected, 0)
        c.close()
    finally:
        os.unlink(db_path)

    # Test: check_only=True → no DB modifications
    db_path = _make_test_db(test_candidates)
    try:
        validate(db_path, check_only=True)
        c = sqlite3.connect(str(db_path))
        rejected = c.execute(
            "SELECT COUNT(*) FROM candidates WHERE pipeline_state = 'llm_rejected'"
        ).fetchone()[0]
        check("check_only doesn't modify DB", rejected, 0)
        c.close()
    finally:
        os.unlink(db_path)

    # Test: tiebreaker: same score, lower id demoted first
    tie_candidates = [
        {"id": 1, "name": "Rec A", "tier": "Recommended", "score": 10},
        {"id": 2, "name": "Rec B", "tier": "Recommended", "score": 9},
        {"id": 3, "name": "Rec C", "tier": "Recommended", "score": 8},
        {"id": 4, "name": "Rec D", "tier": "Recommended", "score": 7},
        {"id": 5, "name": "Rec E", "tier": "Recommended", "score": 6},
        {"id": 6, "name": "Rec F", "tier": "Recommended", "score": 5},
        {"id": 10, "name": "BL tied-a", "tier": "Borderline", "score": 2},
        {"id": 20, "name": "BL tied-b", "tier": "Borderline", "score": 2},
        {"id": 30, "name": "BL tied-c", "tier": "Borderline", "score": 2},
        {"id": 40, "name": "BL tied-d", "tier": "Borderline", "score": 2},
        {"id": 50, "name": "BL tied-e", "tier": "Borderline", "score": 2},
    ]
    # 11 accepted, 5 Borderline = 45.5%.
    # excess = ceil((5 - 0.30*11) / 0.70) = 3. Tiebreak by id ASC: id=10, 20, 30 demoted.
    db_path = _make_test_db(tie_candidates)
    try:
        validate(db_path, check_only=False)
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        demoted_rows = c.execute(
            "SELECT id FROM candidates WHERE pipeline_state = 'llm_rejected' ORDER BY id"
        ).fetchall()
        check("tiebreaker demotes lowest IDs",
              [r["id"] for r in demoted_rows], [10, 20, 30])
        c.close()
    finally:
        os.unlink(db_path)

    # --- Travel-recovery run_date scoping (audit P0-4) ---
    # Recovery must only reconsider candidates rejected in the CURRENT run.
    # A historical rejection (run_date in the past) stays rejected, even if
    # the event date is still in the future.
    recovery_reasoning = (
        "Not Recommended: 55 min transit each way is too far for this event."
    )
    db_path = _make_test_db([])
    try:
        c = sqlite3.connect(str(db_path))
        c.execute(
            """INSERT INTO candidates
               (id, name, description, run_date, date, pipeline_state,
                llm_tier, llm_reasoning, score, travel_display, venue_name)
               VALUES (1, 'Lavender Fields Trip', 'Annual lavender festival visit.',
                       '2026-01-01', '2099-01-01', 'llm_rejected',
                       'Not Recommended', ?, 5, 'Bus 55 min', 'Mayfield Farm')""",
            (recovery_reasoning,),
        )
        c.execute(
            """INSERT INTO candidates
               (id, name, description, run_date, date, pipeline_state,
                llm_tier, llm_reasoning, score, travel_display, venue_name)
               VALUES (2, 'Strawberry Fair', 'Open-air strawberry festival.',
                       date('now'), '2099-01-01', 'llm_rejected',
                       'Not Recommended', ?, 5, 'Bus 55 min', 'Hyde Park')""",
            (recovery_reasoning,),
        )
        c.commit()
        c.close()
        validate(db_path, check_only=False)
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        st_old = c.execute(
            "SELECT pipeline_state FROM candidates WHERE id = 1"
        ).fetchone()["pipeline_state"]
        st_new = c.execute(
            "SELECT pipeline_state FROM candidates WHERE id = 2"
        ).fetchone()["pipeline_state"]
        check("stale rejection NOT resurrected by travel recovery",
              st_old, "llm_rejected")
        check("current-run rejection recovered", st_new, "ready_to_write")
        c.close()
    finally:
        os.unlink(db_path)

    print(f"\n{'ALL PASSED' if ok else 'SOME FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DB_PATH), type=Path)
    p.add_argument("--check-only", action="store_true",
                   help="Report violations without correcting them (for testing)")
    p.add_argument("--smoke-test", action="store_true",
                   help="Run offline smoke tests and exit")
    args = p.parse_args()
    if args.smoke_test:
        sys.exit(_smoke_tests())
    sys.exit(validate(args.db, check_only=args.check_only))