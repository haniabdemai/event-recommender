#!/usr/bin/env python3
"""
Cross-batch duplicate detection for the event pipeline.

Two-stage approach:
  Stage 1 (deterministic): Find pending candidates whose names are similar
  to existing candidates with the same event date (prefix match or >70%
  word overlap).
  Stage 2 (LLM): A subagent reviews the pre-filtered pairs and confirms
  duplicates with confidence and reasoning.

Usage:
    python3 dedup_candidates.py --prepare
    python3 dedup_candidates.py --apply results.json
    python3 dedup_candidates.py --smoke-test
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
import re
import sqlite3
import sys
from pathlib import Path

from erlib import db as erdb
from erlib.config import DB_PATH

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
OUTPUT_FILE = SCRIPT_DIR / ".dedup_pairs.json"
CONFIDENCE_THRESHOLD = 0.70
DESCRIPTION_SNIPPET_LEN = 200


# Typographic → ASCII mappings for Unicode normalisation.
# Matches the table in scripts/verify_newsletter_extraction.py and reconcile_notion.py.
from erlib.normalise import normalise_name as _normalise  # noqa: E402


def _words(normalised: str) -> set[str]:
    return set(re.findall(r'\w+', normalised))


def names_similar(a: str, b: str) -> str | None:
    """Check if two names are similar enough to warrant LLM review.

    Returns the match type ('prefix' or 'word_overlap') or None.
    """
    if not a or not b:
        return None
    na, nb = _normalise(a), _normalise(b)
    if na.startswith(nb) or nb.startswith(na):
        return "prefix"
    wa, wb = _words(na), _words(nb)
    if not wa or not wb:
        return None
    overlap = len(wa & wb)
    smaller = min(len(wa), len(wb))
    if smaller > 0 and overlap / smaller >= 0.7:
        return "word_overlap"
    return None


def _snippet(text: str | None) -> str | None:
    if not text:
        return None
    if len(text) <= DESCRIPTION_SNIPPET_LEN:
        return text
    return text[:DESCRIPTION_SNIPPET_LEN] + "..."


# ---------------------------------------------------------------------------
# Prepare: find potential duplicates, write .dedup_pairs.json
# ---------------------------------------------------------------------------

def cmd_prepare(db_path: Path) -> int:
    conn = erdb.connect(db_path)

    pending = conn.execute("""
        SELECT id, name, date, venue_name, source, organiser, description
        FROM candidates
        WHERE pipeline_state = 'pending_llm'
          AND COALESCE(end_date, date) >= date('now')
        ORDER BY id ASC
    """).fetchall()

    if not pending:
        print("DEDUP_PREPARE: 0 pending candidates with future dates.")
        OUTPUT_FILE.write_text("[]")
        return 0

    pending_dates = {row["date"] for row in pending}

    placeholders = ",".join("?" for _ in pending_dates)
    all_matching = conn.execute(f"""
        SELECT id, name, date, venue_name, source, organiser, description
        FROM candidates
        WHERE date IN ({placeholders})
          AND pipeline_state NOT IN ('vetoed', 'expired', 'duplicate')
        ORDER BY id ASC
    """, tuple(sorted(pending_dates))).fetchall()

    by_date: dict[str, list[dict]] = {}
    for row in all_matching:
        d = row["date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(dict(row))

    pairs = []
    for row in pending:
        candidates_same_date = by_date.get(row["date"], [])
        matches = []
        for other in candidates_same_date:
            if other["id"] >= row["id"]:
                continue
            sim = names_similar(row["name"], other["name"])
            if sim:
                matches.append({
                    "id": other["id"],
                    "name": other["name"],
                    "venue": other["venue_name"],
                    "source": other["source"],
                    "organiser": other["organiser"],
                    "description_snippet": _snippet(other["description"]),
                    "similarity": sim,
                })
        if matches:
            pairs.append({
                "new_id": row["id"],
                "new_name": row["name"],
                "new_date": row["date"],
                "new_venue": row["venue_name"],
                "new_source": row["source"],
                "new_organiser": row["organiser"],
                "new_description_snippet": _snippet(row["description"]),
                "existing": matches,
            })

    OUTPUT_FILE.write_text(json.dumps(pairs, indent=2, ensure_ascii=False))
    total_comparisons = sum(len(p["existing"]) for p in pairs)
    print(f"DEDUP_PREPARE: {len(pending)} pending candidates, "
          f"{len(pairs)} have similar-name date matches "
          f"({total_comparisons} comparisons)")
    conn.close()
    return 0


# ---------------------------------------------------------------------------
# Apply: mark confirmed duplicates in SQLite
# ---------------------------------------------------------------------------

def cmd_apply(db_path: Path, results_path: Path) -> int:
    if not results_path.exists():
        print(f"Results file not found: {results_path}", file=sys.stderr)
        return 2

    results = json.loads(results_path.read_text())
    if not isinstance(results, list):
        print("Results must be a JSON array.", file=sys.stderr)
        return 2

    if not results:
        print("DEDUP_APPLY: 0 duplicates confirmed.")
        return 0

    conn = erdb.connect(db_path)
    marked = 0
    skipped_low = 0
    skipped_invalid = 0
    skipped_state = 0
    log = []

    for r in results:
        new_id = r.get("new_id")
        dup_of = r.get("duplicate_of")
        raw_confidence = r.get("confidence")
        reasoning = r.get("reasoning", "")

        if raw_confidence is None:
            confidence = 0.0
        elif isinstance(raw_confidence, bool):
            entry = {"new_id": new_id, "duplicate_of": dup_of,
                     "confidence": raw_confidence, "reasoning": reasoning,
                     "action": "invalid_confidence"}
            skipped_invalid += 1
            print(f"  INVALID_CONFIDENCE: id={new_id} got boolean {raw_confidence!r}, "
                  f"expected float 0.0-1.0. Keeping candidate (safe default).")
            log.append(entry)
            continue
        elif isinstance(raw_confidence, str):
            try:
                confidence = float(raw_confidence)
            except ValueError:
                entry = {"new_id": new_id, "duplicate_of": dup_of,
                         "confidence": raw_confidence, "reasoning": reasoning,
                         "action": "invalid_confidence"}
                skipped_invalid += 1
                print(f"  INVALID_CONFIDENCE: id={new_id} got unparseable string "
                      f"{raw_confidence!r}. Keeping candidate (safe default).")
                log.append(entry)
                continue
        elif isinstance(raw_confidence, (int, float)):
            confidence = float(raw_confidence)
        else:
            confidence = 0.0

        if confidence < 0.0 or confidence > 1.0:
            entry = {"new_id": new_id, "duplicate_of": dup_of,
                     "confidence": confidence, "reasoning": reasoning,
                     "action": "invalid_confidence"}
            skipped_invalid += 1
            print(f"  INVALID_CONFIDENCE: id={new_id} confidence={confidence} "
                  f"is outside 0.0-1.0 range. Keeping candidate (safe default).")
            log.append(entry)
            continue

        entry = {"new_id": new_id, "duplicate_of": dup_of,
                 "confidence": confidence, "reasoning": reasoning,
                 "action": None}

        if confidence < CONFIDENCE_THRESHOLD:
            entry["action"] = "kept_low_confidence"
            skipped_low += 1
            print(f"  KEPT: id={new_id} (confidence {confidence:.0%} "
                  f"< threshold {CONFIDENCE_THRESHOLD:.0%})")
        else:
            row = conn.execute(
                "SELECT pipeline_state FROM candidates WHERE id = ?",
                (new_id,),
            ).fetchone()
            if not row or row[0] != "pending_llm":
                state = row[0] if row else "not_found"
                entry["action"] = f"skipped_wrong_state ({state})"
                skipped_state += 1
                print(f"  SKIP: id={new_id} not in pending_llm (state: {state})")
            else:
                conn.execute(
                    "UPDATE candidates SET pipeline_state = 'duplicate' "
                    "WHERE id = ?",
                    (new_id,),
                )
                entry["action"] = "marked_duplicate"
                marked += 1
                print(f"  DUPLICATE: id={new_id} duplicate of id={dup_of} "
                      f"({confidence:.0%}: {reasoning})")

        log.append(entry)

    conn.commit()
    conn.close()

    log_path = SCRIPT_DIR / ".dedup_results.json"
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False))

    print(f"DEDUP_APPLY: {marked} marked duplicate, "
          f"{skipped_low} kept (low confidence), "
          f"{skipped_invalid} rejected (invalid confidence), "
          f"{skipped_state} skipped (wrong state)")
    return 0


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------

def _smoke_tests() -> int:
    # Redirect state-file writes to a temp dir: the smoke suite used to
    # overwrite the git-tracked .dedup_results.json on every `make test`
    # run (WP5). The process exits after the suite - no restore needed.
    import tempfile as _tempfile
    global SCRIPT_DIR, OUTPUT_FILE
    SCRIPT_DIR = Path(_tempfile.mkdtemp(prefix="dedup_smoke_"))
    OUTPUT_FILE = SCRIPT_DIR / ".dedup_pairs.json"
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    # --- names_similar: known duplicate patterns ---
    check("prefix: Spatial",
          names_similar("Spatial, No Problem",
                        "Spatial, No Problem by Lee 'Scratch' Perry & Mouse on Mars"),
          "prefix")
    check("prefix: Delcy",
          names_similar("Delcy Morelos in Conversation",
                        "Delcy Morelos in Conversation with Hans Ulrich Obrist"),
          "prefix")
    check("word_overlap: Mindstone",
          names_similar("Mindstone London AI Meetup",
                        "Mindstone London June AI Meetup"),
          "word_overlap")
    check("word_overlap: we DJ",
          names_similar("we DJ Beginners DJ Workshop",
                        "we DJ: Beginners DJ workshop") is not None,
          True)
    check("prefix: identical names",
          names_similar("Test Event", "Test Event"),
          "prefix")

    # --- names_similar: known different events ---
    check("no match: unrelated",
          names_similar("London Jazz Festival", "City Arts Centre Art Exhibition"),
          None)
    check("no match: same venue different event",
          names_similar("Riverside Gallery Late", "The Main Hall Installation"),
          None)
    check("no match: low word overlap",
          names_similar("DJ Set", "DJ Workshop"),
          None)

    # --- names_similar: pre-filter correctly flags ambiguous pairs ---
    check("prefix flags ambiguous subtitle",
          names_similar("Project a Black Planet",
                        "Project a Black Planet: Film"),
          "prefix")

    # --- names_similar: edge cases ---
    check("empty string", names_similar("", "Something"), None)
    check("none handling", names_similar(None, "Something"), None)

    # --- _snippet ---
    check("snippet short", _snippet("Short text"), "Short text")
    check("snippet long", _snippet("x" * 300),
          "x" * DESCRIPTION_SNIPPET_LEN + "...")
    check("snippet none", _snippet(None), None)
    check("snippet exact boundary",
          _snippet("x" * DESCRIPTION_SNIPPET_LEN),
          "x" * DESCRIPTION_SNIPPET_LEN)

    # --- _normalise: Unicode ---
    check("normalise basic", _normalise("Jazz Night"), "jazz night")
    check("normalise whitespace collapse", _normalise("  Jazz   Night  "), "jazz night")
    check("normalise NFKD ligature", _normalise("ﬁnale"), "finale")
    check("normalise ASCII unchanged", _normalise("plain ascii"), "plain ascii")

    # --- names_similar with Unicode variants ---
    check("unicode prefix: curly vs straight apostrophe",
          names_similar("Gay’s We Chree", "Gay's We Chree Live Session"),
          "prefix")
    check("unicode prefix: em dash matches hyphen",
          names_similar("Art—Life Workshop", "Art-Life Workshop June"),
          "prefix")
    check("unicode prefix: NBSP in name",
          names_similar("Sat\xa0Night Jazz", "Sat Night Jazz"),
          "prefix")

    # --- confidence validation in cmd_apply ---
    import tempfile
    import io
    from contextlib import redirect_stdout

    tdb_cv_path = Path(tempfile.mktemp(suffix=".db"))
    tdb_cv = sqlite3.connect(tdb_cv_path)
    tdb_cv.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY, name TEXT, date TEXT,
        pipeline_state TEXT DEFAULT 'pending_llm',
        description TEXT, venue_name TEXT)""")
    tdb_cv.execute(
        "INSERT INTO candidates (id, name, date, pipeline_state) "
        "VALUES (99901, 'Test Event', '2026-09-01', 'pending_llm')")
    tdb_cv.commit()
    tdb_cv.close()

    def _reset_cv():
        c = sqlite3.connect(tdb_cv_path)
        c.execute("UPDATE candidates SET pipeline_state='pending_llm' WHERE id=99901")
        c.commit()
        c.close()

    # boolean confidence
    p1 = Path(tempfile.mktemp(suffix=".json"))
    p1.write_text(json.dumps([
        {"new_id": 99901, "duplicate_of": 1, "confidence": True, "reasoning": "test"}]))
    f1 = io.StringIO()
    with redirect_stdout(f1):
        cmd_apply(tdb_cv_path, p1)
    check("confidence: boolean flagged as INVALID_CONFIDENCE",
          "INVALID_CONFIDENCE" in f1.getvalue(), True)

    _reset_cv()

    # out-of-range confidence (85 instead of 0.85)
    p2 = Path(tempfile.mktemp(suffix=".json"))
    p2.write_text(json.dumps([
        {"new_id": 99901, "duplicate_of": 1, "confidence": 85, "reasoning": "test"}]))
    f2 = io.StringIO()
    with redirect_stdout(f2):
        cmd_apply(tdb_cv_path, p2)
    check("confidence: out-of-range (85) flagged as INVALID_CONFIDENCE",
          "INVALID_CONFIDENCE" in f2.getvalue(), True)

    _reset_cv()

    # string confidence "0.85" silently converted (not crashed)
    p3 = Path(tempfile.mktemp(suffix=".json"))
    p3.write_text(json.dumps([
        {"new_id": 99901, "duplicate_of": 1, "confidence": "0.85", "reasoning": "test"}]))
    f3 = io.StringIO()
    with redirect_stdout(f3):
        cmd_apply(tdb_cv_path, p3)
    check("confidence: string '0.85' accepted without crash",
          "INVALID_CONFIDENCE" not in f3.getvalue(), True)

    _reset_cv()

    # negative confidence should be flagged
    p4 = Path(tempfile.mktemp(suffix=".json"))
    p4.write_text(json.dumps([
        {"new_id": 99901, "duplicate_of": 1, "confidence": -0.5, "reasoning": "test"}]))
    f4 = io.StringIO()
    with redirect_stdout(f4):
        cmd_apply(tdb_cv_path, p4)
    check("confidence: negative (-0.5) flagged as INVALID_CONFIDENCE",
          "INVALID_CONFIDENCE" in f4.getvalue(), True)

    # check that invalid confidence is reported separately from low confidence
    check("confidence: invalid counter separate from low-confidence",
          "rejected (invalid confidence)" in f4.getvalue(), True)

    tdb_cv_path.unlink(missing_ok=True)
    p1.unlink(missing_ok=True)
    p2.unlink(missing_ok=True)
    p3.unlink(missing_ok=True)
    p4.unlink(missing_ok=True)

    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DB_PATH), type=Path)
    p.add_argument("--prepare", action="store_true",
                   help="Find potential duplicates, write .dedup_pairs.json")
    p.add_argument("--apply", type=Path, metavar="RESULTS_FILE",
                   help="Read confirmed duplicates and mark in SQLite")
    p.add_argument("--smoke-test", action="store_true",
                   help="Run offline unit tests")
    args = p.parse_args()

    if args.smoke_test:
        return _smoke_tests()
    if args.prepare:
        return cmd_prepare(args.db)
    if args.apply:
        return cmd_apply(args.db, args.apply)

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
