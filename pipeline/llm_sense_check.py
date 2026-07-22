#!/usr/bin/env python3
"""
LLM sense-check: reviews non-vetoed candidates and assigns final tiers.

Deterministic Python scoring catches obvious mismatches; this step applies
contextual judgment (identity-group filtering, vibe reads, price-vs-value)
before candidates reach Notion.

This script handles the plumbing. The session (Claude Code scheduled task)
provides the LLM judgment: no external API call, no API key.

Usage:
    python3 llm_sense_check.py --prepare [--limit N]
    python3 llm_sense_check.py --apply results.json

--prepare: queries SQLite for unreviewed candidates, formats batches,
           writes .llm_batches.json plus one self-contained prompt file per
           batch (.llm_batch_prompts/batch_N.txt) for sense-check subagents.
--apply:   reads the session's tier assignments from a JSON file,
           validates, writes to SQLite, generates .last_llm_summary.json.
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
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from erlib import db as erdb
from erlib.config import DB_PATH
from erlib.constants import BORDERLINE_CAP, MIN_FOR_CAP
from erlib.freshness import stamp

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
# Your personal profile (gitignored) wins; the shipped example-persona
# template keeps the pipeline runnable out of the box.
_PERSONAL_PROFILE = SCRIPT_DIR / "references" / "taste-profile.md"
_TEMPLATE_PROFILE = SCRIPT_DIR / "references" / "taste-profile-template.md"
TASTE_PROFILE = _PERSONAL_PROFILE if _PERSONAL_PROFILE.exists() else _TEMPLATE_PROFILE

BATCH_SIZE = 20

VALID_TIERS = {"Top Picks", "Recommended", "Borderline", "Not Recommended"}

# Volume sanity caps, rebased 2026-07-12 for post-refactor volumes (the
# 42-day fetch window and no 28-day LLM ceiling make big batches normal:
# 201 LLM-bound on 2026-07-05, 409 fetched on 2026-06-24). FLAG marks a
# batch above anything yet observed; ABORT catches a genuinely runaway
# flood (broken dedup/fetch). Batches are isolated per-subagent files, so
# the old context-bomb rationale for a low abort no longer applies.
COUNT_FLAG = 300
COUNT_ABORT = 600

# Appended to each .llm_batch_prompts/batch_N.txt so the file is the
# subagent's complete instruction set. Mirrors Step 5b of
# references/scheduled-task-prompt.md: keep the two in sync.
SUBAGENT_CONSTRAINTS = """IMPORTANT CONSTRAINTS:
- Borderline means genuine uncertainty. Do NOT use it as a safe default.
  At most 6 out of 20 candidates (30%) should be Borderline. If you find
  yourself assigning Borderline to more than that, you are being indecisive.
  Make a call: Recommended or Not Recommended.
- Return ONLY a JSON array. No commentary, no markdown fencing, no
  explanation outside the JSON. Just the raw array:
  [{"id": <id>, "tier": "<tier>", "reasoning": "<1-2 sentences>", "corrected_type": "<type or null>"}, ...]
  Include "corrected_type" only when the Type field is wrong. Omit or set null when correct."""


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

LLM_COLUMNS = {
    "llm_tier": "TEXT",
    "llm_reasoning": "TEXT",
    "llm_reviewed": "TEXT",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    erdb.ensure_columns(conn, "candidates", LLM_COLUMNS)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS format_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            old_type TEXT NOT NULL,
            new_type TEXT NOT NULL,
            run_date TEXT NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Batch formatting
# ---------------------------------------------------------------------------

def _past_connection_line(signal_names: list[str], row: sqlite3.Row) -> str | None:
    """Build a Past connection line when Signal #31/#32 fired."""
    parts = []
    for s in signal_names:
        if "Signal #31" in s:
            parts.append("The user has attended this event series before")
        elif "Known venue-as-organiser" in s:
            venue = row["venue_name"] or "this venue"
            parts.append(f"The user has been to events at {venue} before")
        elif "Signal #32" in s:
            org = row["organiser"] or "this organiser"
            parts.append(f"The user has been to events by {org} before")
    if not parts:
        return None
    return f"   Past connection: {'; '.join(parts)}\n"


def format_candidate(idx: int, row: sqlite3.Row) -> str:
    travel = row["travel_display"] or "unknown"
    if row["travel_lookup_failed"]:
        travel = "unknown"
    cost = row["cost"] or "unknown"

    desc = row["description"] or "No description available"

    py_tier = row["tier"] or "Unscored"
    py_score = row["score"]
    signals_raw = row["signals_fired"]
    signal_names = []
    if signals_raw:
        try:
            signal_names = json.loads(signals_raw)
            signals_short = [s.split(": ", 1)[-1] for s in signal_names]
            signals_str = ", ".join(signals_short) if signals_short else "none"
        except (json.JSONDecodeError, TypeError):
            signals_str = "none"
    else:
        signals_str = "none"

    py_line = f"   Python assessment: {py_tier}"
    if py_score is not None:
        py_line += f" (score {py_score})"

    past_conn = _past_connection_line(signal_names, row) or ""

    format_type = dict(row).get("format_type") or "Social meetup"

    return (
        f"{idx}. ID: {row['id']}\n"
        f"   Name: {row['name']}\n"
        f"   Date: {row['date']}{' to ' + dict(row).get('end_date') if dict(row).get('end_date') else ''}\n"
        f"   Type: {format_type}\n"
        f"   Organiser: {row['organiser'] or 'unknown'}\n"
        f"   Cost: {cost}\n"
        f"   Travel: {travel}\n"
        f"   Description: {desc}\n"
        f"{py_line}\n"
        f"   Matched signals: {signals_str}\n"
        f"{past_conn}"
    )


SENSE_CHECK_INSTRUCTION = """\
You are the last check before these events reach the user's recommendations \
board. The taste profile you have been given describes who the user is: \
the letters below are its hard-veto checklist. (The shipped defaults \
describe "Alex", the example persona from the taste-profile template.)

Each candidate shows a "Python assessment" (tier + score) and "Matched signals" \
from the algorithmic scorer. Use this as your starting point: it tells you what \
keywords and patterns the algorithm detected. Your job is contextual judgment the \
algorithm cannot do: crowd/vibe reads, identity-group checks, price-vs-value, \
and quality assessment beyond keywords.

Process each candidate in two passes.

PASS 1: Hard veto check. If ANY item matches, the answer is "Not Recommended". \
Do NOT hedge into Borderline. Generic positives (free, social, beginner-friendly, \
"all welcome") never override a hard veto.

  A. Wrong sport. Climbing and bouldering are positive. Anything else is a \
hard no: badminton, football, volleyball, cricket, rugby, netball, basketball, \
tennis, padel, squash, ping pong, table tennis, hockey, darts. Also F1/Grand \
Prix viewing parties, and cardio fitness classes (Zumba, aerobics, dance \
fitness).

  B. Dating/singles. Speed dating, singles nights, matchmaking, "meet your \
match", "find love". Including identity-specific versions.

  C. Book, film, or discussion groups. Silent-writing sprints, write clubs, \
book clubs, film clubs, philosophy circles, debate nights, political \
discussion groups. Exception: a one-off book launch with a genuinely \
interesting author IS fine; repeating discussion meetups are not.

  D. Classical concerts. Symphony, recital, chamber music, opera, philharmonic, \
oratorio, string quartet, concerto. Exception: classical crossover with folk, \
jazz, electronic, or film score IS fine.

  E. Hardcore technical training. Node.js, React, Vue, Kubernetes, Docker, \
microservices, system design interview, database/platform user groups. Vibe \
coding, AI builders meetups, hackdays are DIFFERENT: social/playful and wanted.

  F. Startup/VC/pitch/tech startup networking. Pitch nights, demo days, investor \
meetups, VC networking, entrepreneur networking. The user is not a founder. \
Events geared at founders, startup professionals, or fundraising are wrong crowd. \
Exception: indie maker/solopreneur community events = builder community. \
AI/tech community events with hands-on demos or building content are not startup \
networking: assess on content, not audience labels. The veto targets fundraising \
energy, not every event where founders happen to be present.

  G. Tabletop/board games. Warhammer, D&D, RPG nights, board game cafes. \
Events that list board games as ONE activity among several (e.g. a park \
social with volleyball, team challenges, and board games) are NOT board game \
events: evaluate the overall event character in Pass 2.

  H. Language/culture exchange. Language exchange, learn Mandarin/Japanese/etc.

  I. International/expat meetups. "International professionals", "expat drinks", \
"world citizens" framing. Wrong crowd. Exception: 20s-30s events that use \
"global" are OK.

  J. Identity-specific events. Events FOR a religious, ethnic, LGBTQ+, or \
lifestyle-identity community the user isn't part of. CRITICAL EXCEPTION: check \
the taste profile's "My Communities" section: events for communities the user \
IS part of are a POSITIVE signal, not a veto (the example persona: the indoor \
climbing community and maker/creative-tech communities). "LGBT-friendly" or \
"inclusive" wording is welcoming, not targeting: that alone is never a veto.

  K. Public speaking clubs. Speaking-drill formats, speakers clubs, oratory \
practice groups.

  L. Corporate-hosted events. Big-4, banks, enterprise software. \
EXCEPTION: events about music technology, audio engineering, or creative \
tools are the user's professional field: assess on content, don't auto-veto.

  M. Gimmicky walk-through exhibitions. Instagrammable set-dressing with no \
participation.

  N. Token craft workshops. 90% talk, 10-minute activity at the end.

  O. Pub quizzes, karaoke, escape rooms, pub crawls.

  P. DJ practice events. Open decks, DJ workshops, "learn to DJ". NOT the \
same as listening bars or curated vinyl events (those are positive).

  Q. Walks inside London. Thames paths, canal walks, guided London walks, \
historical walking tours, evening strolls. An activity at multiple locations \
(thrifting, shopping) is not a walk. NOT country hikes outside London (those \
are still OK if easy/scenic).

  R. Group exhibition visits. Meetup-organised group trips to galleries or \
museums. Going to an exhibition independently is assessed on its own merits.

  S. 30s-40s age framing. Events pitched at "30s & 40s" crowd. 20s-30s is fine.

  T. "Young professionals" framing. Corporate/career crowd.

  U. Events for professional communities the user isn't part of. Data \
professionals, game developers, journalism tech. The crowd is wrong even if \
the topic sounds adjacent. Test: is the audience working professionals in that \
field, or curious people from different backgrounds? EXCEPTION: music \
technology, music production, audio engineering, and creative technology \
communities are the user's own field and hobby. Assess those on their merits.

  V. Blocked organisers: "The Hype Collective", "Megamix Socials", \
"Gallery Wander Club". Always Not Recommended. (Fictional examples: the \
taste profile carries the user's real list.)

  W. Park hangouts with no structured activity. Just "hang out in the park" \
with no creative/physical element. A park on the far side of the city from \
home is too far for a casual outdoor social.

If any of A–W matches: output "Not Recommended" with the letter in the \
reasoning. Don't reason further about that candidate.

PASS 2: Only for candidates that passed Pass 1. Apply judgement using the \
taste profile. Would the user actually enjoy this? Is the core activity \
aligned? Is the crowd/vibe right? Is the price reasonable for the value?

TRAVEL RULE: travel time alone must NEVER result in "Not Recommended". \
The comfortable range is ~45 min by transit, but the user will stretch for \
the right thing. Apply a sliding penalty:
- Up to 45 min: no penalty.
- 45-60 min: no penalty if event is well-aligned and multi-hour.
- 60-90 min: drop one tier (Top Picks → Recommended, Recommended → Borderline). \
A routine weekly meetup at this distance drops further.
- 90+ min: Borderline at best. Only genuinely exceptional events survive.
Unique, seasonal, or one-off experiences (lavender fields, outdoor festivals, \
limited-run exhibitions, special tours) justify longer travel than recurring \
weekly meetups. When penalising for travel, state the trade-off in your \
reasoning: "80 min transit for a 3-hour workshop": not just "too far."

No events before 10am. Otherwise, time of day is not a factor.

Book launches and ideas talks require genuine interest in the specific topic: \
don't give credit just because the format is "ideas."

Events with no clear agenda: recurring events that never specify what they'll \
cover: should be scored lower.

OVERRIDE EXPLANATION RULE: When you assign a different tier than the Python \
assessment, your reasoning must explain what you saw that the algorithm missed \
or got wrong. One sentence, lead with the key factor. This reasoning appears \
in the pipeline report: it must be self-explanatory to a human reader.

PAST CONNECTION SIGNALS: "Known series" (Signal #31), "Known organiser" \
(Signal #32), and "Known venue-as-organiser" mean the user has attended events \
in this series, by this organiser, or at this venue before. When a candidate \
shows a "Past connection" line, mention it in your reasoning: it is a \
genuine positive signal from real attendance history.

ANTI-HEDGING RULE: If you downgrade a Python "Recommended" or "Top Picks" to \
Borderline, you MUST name the specific concern the algorithm missed. If you \
cannot articulate what is wrong, the answer is Recommended. Borderline means \
you have a specific, articulable concern that creates genuine uncertainty.

OVERRIDE CAUTION: If the Python assessment is "Recommended" or "Top Picks" \
with a high score (6+) and multiple matched signals, think twice before \
overriding to Not Recommended. The algorithm detected genuine alignment. \
A high Python score with content signals (AI/tech community, Creative activity, \
Social through activity, Known series) means the event matches the user's \
documented interests. Only override if you have a CLEAR veto match (A-W), not \
a vague concern about crowd or format.

INFERENCE RULE: If you can reasonably infer from the event name or context \
that it aligns with a listed interest: even when the description is thin: \
lean into that inference, not away from it. A music gear expo is a music \
production event. A creative-technology showcase is the user's field. Name an \
event's likely alignment and promote accordingly, rather than dismissing \
for lack of explicit description.

FORMAT TYPE VERIFICATION: Each candidate shows a "Type" field assigned by \
keyword matching. Verify it makes sense. If the type is clearly wrong, \
include a "corrected_type" field in your output for that candidate. \
Canonical types: Social meetup, Workshop, Talk, Outdoor, Creative social, \
Concert, Exhibition, Wellness, Immersive experience, Film, Book event, Other. \
Common errors to watch for: a picnic labelled as Talk, a day trip labelled \
as Social meetup, a social night labelled as Concert. Trust the event name \
and description over the keyword-assigned type.
"""


def format_batch_prompt(rows: list[sqlite3.Row], today: str | None = None) -> str:
    header = SENSE_CHECK_INSTRUCTION + "\n"
    header += f"Review these {len(rows)} candidates. For each, assign a tier and brief reasoning.\n"
    if today:
        header += f"\nToday's date: {today}\n"
    parts = [header]
    for i, row in enumerate(rows, 1):
        parts.append(format_candidate(i, row))
    parts.append(
        "Output JSON:\n"
        "[\n"
        '  {"id": <id>, "tier": "<tier>", "reasoning": "<1-2 sentences>", "corrected_type": "<type or null>"},\n'
        "  ...\n"
        "]\n"
        'Include "corrected_type" only when the Type is wrong. Omit it or set null when correct.\n'
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_response_text(text: str, expected_ids: set[int]) -> list[dict]:
    """Parse a JSON array of tier assignments from raw text."""
    import re

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON array in response: {text[:200]}")

    results = json.loads(match.group())
    if not isinstance(results, list):
        raise ValueError(f"Expected list, got {type(results).__name__}")

    parsed = []
    for item in results:
        cid = item.get("id")
        tier = item.get("tier", "").strip()
        reasoning = item.get("reasoning", "").strip()

        if cid not in expected_ids:
            print(f"  WARNING: unexpected ID {cid} in response, skipping")
            continue
        if tier not in VALID_TIERS:
            print(f"  WARNING: invalid tier {tier!r} for ID {cid}, skipping")
            continue

        parsed.append({"id": cid, "tier": tier, "reasoning": reasoning})

    return parsed


# ---------------------------------------------------------------------------
# Prepare: format batches for the session to review
# ---------------------------------------------------------------------------

SELECT_CANDIDATES = """
SELECT *
FROM candidates
WHERE pipeline_state = 'pending_llm'
  AND llm_reviewed IS NULL
  AND COALESCE(end_date, date) >= date('now')
ORDER BY id ASC
"""


def cmd_prepare(db_path: Path, *, limit: int | None) -> int:
    if not TASTE_PROFILE.exists():
        print(f"Taste profile not found: {TASTE_PROFILE}", file=sys.stderr)
        return 2

    taste_text = TASTE_PROFILE.read_text()

    conn = erdb.connect(db_path)
    ensure_schema(conn)

    # Recover candidates stuck in pending_llm despite having been reviewed
    stuck = conn.execute(
        """SELECT id, llm_tier, travel_display, travel_lookup_failed
           FROM candidates
           WHERE pipeline_state = 'pending_llm'
             AND llm_reviewed IS NOT NULL
             AND llm_tier IS NOT NULL"""
    ).fetchall()
    if stuck:
        for row in stuck:
            if row["llm_tier"] == "Not Recommended":
                new_state = "llm_rejected"
            elif row["travel_display"] is not None or row["travel_lookup_failed"] == 1:
                new_state = "ready_to_write"
            else:
                new_state = "pending_travel"
            conn.execute(
                "UPDATE candidates SET pipeline_state = ? WHERE id = ?",
                (new_state, row["id"]),
            )
        conn.commit()
        print(f"PREPARE_RECOVERY: {len(stuck)} candidates recovered from stuck pending_llm state")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = list(conn.execute(SELECT_CANDIDATES))
    if limit is not None:
        rows = rows[:limit]

    print(f"Candidates to review: {len(rows)}")
    if not rows:
        print("Nothing to review.")
        return 0

    all_unreviewed = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE llm_reviewed IS NOT NULL"
    ).fetchone()[0] == 0
    if not all_unreviewed:
        if len(rows) > COUNT_ABORT:
            print(f"ABORT: {len(rows)} candidates exceeds limit of {COUNT_ABORT}",
                  file=sys.stderr)
            return 1
        if len(rows) > COUNT_FLAG:
            print(f"WARNING: {len(rows)} candidates exceeds expected range (>{COUNT_FLAG})")

    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    print(f"Batches: {len(batches)} (size {BATCH_SIZE})")

    output = []
    for batch_idx, batch in enumerate(batches):
        batch_num = batch_idx + 1
        candidate_ids = [row["id"] for row in batch]
        user_prompt = format_batch_prompt(batch, today=today)
        output.append({
            "batch_num": batch_num,
            "candidate_ids": candidate_ids,
            "system_prompt": taste_text,
            "user_prompt": user_prompt,
        })

    out_path = SCRIPT_DIR / ".llm_batches.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    # Self-contained per-batch prompt files so sense-check subagents can read
    # ONLY their own batch. The orchestrator must never load candidate text:
    # reading .llm_batches.json into the orchestrator context is what
    # exhausted the 2026-07-07 scheduled run mid-pipeline.
    prompts_dir = SCRIPT_DIR / ".llm_batch_prompts"
    prompts_dir.mkdir(exist_ok=True)
    for stale in prompts_dir.glob("batch_*.txt"):
        stale.unlink()
    for batch in output:
        (prompts_dir / f"batch_{batch['batch_num']}.txt").write_text(
            f"{batch['system_prompt']}\n\n───\n\n{batch['user_prompt']}"
            f"\n\n───\n\n{SUBAGENT_CONSTRAINTS}\n"
        )
    print(f"Wrote {len(output)} batch prompt files to {prompts_dir.name}/")

    all_ids = [cid for batch in output for cid in batch["candidate_ids"]]
    manifest = {"selected_count": len(all_ids), "selected_ids": all_ids}
    manifest_path = SCRIPT_DIR / ".llm_prepare_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)

    print(f"Wrote {len(output)} batches to {out_path}")
    print(f"PREPARE_MANIFEST: {len(all_ids)} candidates selected")
    return 0


# ---------------------------------------------------------------------------
# Apply: write session's tier assignments back to SQLite
# ---------------------------------------------------------------------------

def cmd_apply(db_path: Path, results_file: Path) -> int:
    if not results_file.exists():
        print(f"Results file not found: {results_file}", file=sys.stderr)
        return 2

    results = json.loads(results_file.read_text())
    if not isinstance(results, list):
        print("Results file must contain a JSON array.", file=sys.stderr)
        return 2

    conn = erdb.connect(db_path)
    ensure_schema(conn)

    now = datetime.now(timezone.utc).isoformat()

    tier_counts = {"Top Picks": 0, "Recommended": 0, "Borderline": 0, "Not Recommended": 0}
    reviewed = 0
    skipped = 0
    format_corrections = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for item in results:
        cid = item.get("id")
        tier = item.get("tier", "").strip()
        reasoning = item.get("reasoning", "").strip()

        if tier not in VALID_TIERS:
            print(f"  WARNING: invalid tier {tier!r} for ID {cid}, skipping")
            skipped += 1
            continue

        tier_counts[tier] += 1
        row = conn.execute(
            "SELECT travel_display, travel_lookup_failed, format_type FROM candidates WHERE id = ?",
            (cid,),
        ).fetchone()
        if tier == 'Not Recommended':
            new_state = 'llm_rejected'
        else:
            has_travel = row and (row["travel_display"] is not None or row["travel_lookup_failed"] == 1)
            new_state = 'ready_to_write' if has_travel else 'pending_travel'
        conn.execute(
            """UPDATE candidates
               SET llm_tier = ?, llm_reasoning = ?, llm_reviewed = ?, pipeline_state = ?
               WHERE id = ? AND pipeline_state = 'pending_llm'""",
            (tier, reasoning, now, new_state, cid),
        )

        corrected_type = item.get("corrected_type")
        if corrected_type and isinstance(corrected_type, str) and corrected_type.strip():
            old_type = (row["format_type"] if row else None) or "unknown"
            new_type = corrected_type.strip()
            if old_type != new_type:
                conn.execute(
                    "UPDATE candidates SET format_type = ? WHERE id = ?",
                    (new_type, cid),
                )
                conn.execute(
                    "INSERT INTO format_corrections (candidate_id, old_type, new_type, run_date) VALUES (?, ?, ?, ?)",
                    (cid, old_type, new_type, today),
                )
                format_corrections.append((cid, old_type, new_type))
                print(f"  [{cid}] {tier} (type: {old_type} -> {new_type}): {reasoning[:80]}")
            else:
                print(f"  [{cid}] {tier}: {reasoning[:80]}")
        else:
            print(f"  [{cid}] {tier}: {reasoning[:80]}")

        reviewed += 1

    total_accepted = sum(v for k, v in tier_counts.items() if k != "Not Recommended")
    borderline_count = tier_counts.get("Borderline", 0)
    if total_accepted > MIN_FOR_CAP and borderline_count / total_accepted > BORDERLINE_CAP:
        print(
            f"\nBORDERLINE_WARN: {borderline_count}/{total_accepted} accepted "
            f"candidates ({borderline_count/total_accepted:.0%}) are Borderline: "
            f"exceeds 30% cap. Validator will enforce.",
            file=sys.stderr,
        )

    if format_corrections:
        print(f"\nFORMAT_CORRECTIONS: {len(format_corrections)} type(s) corrected by LLM:")
        patterns = {}
        for cid, old, new in format_corrections:
            key = f"{old} -> {new}"
            patterns.setdefault(key, []).append(cid)
        for pattern, ids in sorted(patterns.items(), key=lambda x: -len(x[1])):
            print(f"  {pattern}: {len(ids)}x (IDs: {', '.join(str(i) for i in ids)})")
        print("  Review patterns to tighten classify_format() keywords.")

    conn.commit()

    expired_cp = conn.execute(
        """UPDATE candidates SET pipeline_state = 'llm_rejected', llm_reviewed = ?
           WHERE tier = 'Couldn''t Process' AND pipeline_state = 'pending_llm'""",
        (now,),
    ).rowcount
    if expired_cp:
        conn.commit()
        print(f"  Cleaned up {expired_cp} 'Couldn't Process' candidates (no description)")

    stragglers = conn.execute(SELECT_CANDIDATES).fetchall()
    if stragglers:
        ids = [s["id"] for s in stragglers]
        print(f"APPLY_WARN: {len(stragglers)} candidates still eligible but not reviewed: {ids}")

    print(f"\n{'=' * 60}")
    print("SENSE-CHECK RESULTS")
    print(f"{'=' * 60}")
    print(f"Reviewed: {reviewed}")
    if skipped:
        print(f"Skipped (invalid tier): {skipped}")
    for tier, count in tier_counts.items():
        if count:
            print(f"  {tier}: {count}")

    # Disagreement tracking vs Python scorer
    tier_order = {"Top Picks": 4, "Recommended": 3, "Borderline": 2, "Not Recommended": 1}
    disagreements = []
    dis_rows = conn.execute(
        """SELECT id, name, tier, llm_tier, llm_reasoning
           FROM candidates
           WHERE llm_reviewed = ?
             AND tier IS NOT NULL AND llm_tier IS NOT NULL
             AND tier != llm_tier""",
        (now,),
    ).fetchall()
    for r in dis_rows:
        python_rank = tier_order.get(r["tier"], 0)
        llm_rank = tier_order.get(r["llm_tier"], 0)
        direction = "upgrade" if llm_rank > python_rank else "downgrade"
        disagreements.append({
            "id": r["id"],
            "name": r["name"],
            "python_tier": r["tier"],
            "llm_tier": r["llm_tier"],
            "llm_reasoning": r["llm_reasoning"],
            "direction": direction,
        })

    upgrades = sum(1 for d in disagreements if d["direction"] == "upgrade")
    downgrades = sum(1 for d in disagreements if d["direction"] == "downgrade")

    summary = {
        "reviewed": reviewed,
        "tier_breakdown": {k: v for k, v in tier_counts.items() if v > 0},
        "couldn_t_process": expired_cp,
        "skipped": skipped,
        "disagreements": disagreements,
        "disagreement_counts": {
            "total": len(disagreements),
            "upgrades": upgrades,
            "downgrades": downgrades,
        },
    }

    summary_path = SCRIPT_DIR / ".last_llm_summary.json"
    with open(summary_path, "w") as f:
        json.dump(stamp(summary), f, indent=2)

    if disagreements:
        print(f"\nDisagreements with Python scorer: {len(disagreements)} "
              f"({upgrades} upgrades, {downgrades} downgrades)")
        for d in disagreements:
            print(f"  [{d['id']}] {d['name']}: {d['python_tier']} -> {d['llm_tier']} "
                  f"({d['llm_reasoning'][:60]})")

    return 0


# ---------------------------------------------------------------------------
# Smoke tests (offline: no network, no DB)
# ---------------------------------------------------------------------------

def _smoke_tests() -> int:
    ok = True

    def check(label: str, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    class FakeRow(dict):
        def __getitem__(self, key):
            return self.get(key)

    row = FakeRow(id=42, name="Test Event", organiser="Test Org", cost="£10",
                  travel_display="35 min (transit)", travel_lookup_failed=0,
                  description="A cool event", date="2026-05-01",
                  format_type="Workshop",
                  tier="Recommended", score=5,
                  signals_fired='["Signal #1: AI/tech community", "Signal #4: Hands-on"]')
    text = format_candidate(1, row)
    check("format has ID", "ID: 42" in text, True)
    check("format has name", "Name: Test Event" in text, True)
    check("format has type", "Type: Workshop" in text, True)
    check("format has travel", "35 min (transit)" in text, True)
    check("format has python tier", "Recommended (score 5)" in text, True)
    check("format has signal name", "AI/tech community" in text, True)
    check("format has hands-on signal", "Hands-on" in text, True)

    row2 = FakeRow(id=43, name="Test", organiser=None, cost=None,
                   travel_display="35 min (transit)", travel_lookup_failed=1,
                   description="Desc", date="2026-05-01",
                   tier=None, score=None, signals_fired=None)
    text2 = format_candidate(1, row2)
    check("failed travel shows unknown", "Travel: unknown" in text2, True)
    check("null organiser shows unknown", "Organiser: unknown" in text2, True)
    check("null tier shows Unscored", "Unscored" in text2, True)
    check("null signals shows none", "Matched signals: none" in text2, True)
    check("no past connection without signals", "Past connection" not in text2, True)

    # Past connection lines
    row_s31 = FakeRow(id=44, name="Claude Code Curious #5", organiser="Compiler",
                      cost=None, travel_display=None, travel_lookup_failed=0,
                      description="AI meetup", date="2026-06-01",
                      tier="Recommended", score=6, venue_name=None,
                      signals_fired='["Signal #1: AI/tech community", "Signal #31: Known series"]')
    text_s31 = format_candidate(1, row_s31)
    check("signal 31 has past connection", "Past connection:" in text_s31, True)
    check("signal 31 mentions series", "attended this event series" in text_s31, True)

    row_s32 = FakeRow(id=45, name="Vibe Coding Workshop", organiser="code-and-chill",
                      cost=None, travel_display=None, travel_lookup_failed=0,
                      description="Hands-on", date="2026-06-01",
                      tier="Recommended", score=5, venue_name=None,
                      signals_fired='["Signal #32: Known organiser"]')
    text_s32 = format_candidate(1, row_s32)
    check("signal 32 has past connection", "Past connection:" in text_s32, True)
    check("signal 32 names organiser", "code-and-chill" in text_s32.split("Past connection:")[1], True)

    row_venue = FakeRow(id=46, name="Hacknight", organiser="unknown",
                        cost=None, travel_display=None, travel_lookup_failed=0,
                        description="Hack", date="2026-06-01",
                        tier="Recommended", score=5, venue_name="Maker House",
                        signals_fired='["Signal #32: Known venue-as-organiser"]')
    text_venue = format_candidate(1, row_venue)
    check("venue-as-org has past connection", "Past connection:" in text_venue, True)
    check("venue-as-org names venue", "Maker House" in text_venue.split("Past connection:")[1], True)

    row_no_past = FakeRow(id=47, name="Random Event", organiser="Someone",
                          cost=None, travel_display=None, travel_lookup_failed=0,
                          description="Fun", date="2026-06-01",
                          tier="Borderline", score=3, venue_name=None,
                          signals_fired='["Signal #1: AI/tech community"]')
    text_no_past = format_candidate(1, row_no_past)
    check("no signal 31/32 means no past connection", "Past connection" not in text_no_past, True)

    # parse_response_text extracts valid tiers from raw text
    fake_text = json.dumps([
        {"id": 1, "tier": "Top Picks", "reasoning": "Great fit"},
        {"id": 2, "tier": "Not Recommended", "reasoning": "Wrong crowd"},
        {"id": 3, "tier": "Invalid Tier", "reasoning": "Bad"},
    ])
    parsed = parse_response_text(fake_text, {1, 2, 3})
    check("parse valid count", len(parsed), 2)
    check("parse tier 1", parsed[0]["tier"], "Top Picks")
    check("parse tier 2", parsed[1]["tier"], "Not Recommended")

    check("valid tiers count", len(VALID_TIERS), 4)
    check("Top Picks valid", "Top Picks" in VALID_TIERS, True)
    check("Borderline valid", "Borderline" in VALID_TIERS, True)

    # Recovery: test through cmd_prepare (not duplicated logic)
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        tconn = sqlite3.connect(tmp_path)
        tconn.execute("""CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, name TEXT, pipeline_state TEXT,
            llm_reviewed TEXT, llm_tier TEXT, llm_reasoning TEXT,
            travel_display TEXT, travel_lookup_failed INTEGER DEFAULT 0,
            date TEXT, end_date TEXT, tier TEXT, score INTEGER,
            description TEXT, organiser TEXT, cost TEXT,
            format_type TEXT, signals_fired TEXT, venue_name TEXT,
            source TEXT, source_snapshot TEXT
        )""")
        tconn.execute("INSERT INTO candidates (id,name,pipeline_state,llm_reviewed,llm_tier,travel_display,travel_lookup_failed,date) VALUES (1,'Stuck NR','pending_llm','2026-06-24','Not Recommended',NULL,0,'2099-01-01')")
        tconn.execute("INSERT INTO candidates (id,name,pipeline_state,llm_reviewed,llm_tier,travel_display,travel_lookup_failed,date) VALUES (2,'Stuck TP','pending_llm','2026-06-24','Top Picks','30 min',0,'2099-01-01')")
        tconn.execute("INSERT INTO candidates (id,name,pipeline_state,llm_reviewed,llm_tier,travel_display,travel_lookup_failed,date) VALUES (3,'Stuck BL','pending_llm','2026-06-24','Borderline',NULL,0,'2099-01-01')")
        tconn.execute("INSERT INTO candidates (id,name,pipeline_state,llm_reviewed,llm_tier,travel_display,travel_lookup_failed,date) VALUES (4,'Normal','pending_llm',NULL,NULL,NULL,0,'2099-01-01')")
        tconn.execute("INSERT INTO candidates (id,name,pipeline_state,llm_reviewed,llm_tier,travel_display,travel_lookup_failed,date) VALUES (5,'Already OK','ready_to_write','2026-06-24','Recommended','20 min',0,'2099-01-01')")
        tconn.commit()
        tconn.close()

        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115  devnull redirect, restored in finally
        try:
            ret = cmd_prepare(Path(tmp_path), limit=0)
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout
        check("recovery: cmd_prepare returns 0", ret, 0)

        tconn = sqlite3.connect(tmp_path)
        states = {r[0]: r[1] for r in tconn.execute("SELECT id, pipeline_state FROM candidates")}
        tconn.close()
        check("recovery: NR -> llm_rejected", states[1], "llm_rejected")
        check("recovery: TP with travel -> ready_to_write", states[2], "ready_to_write")
        check("recovery: BL no travel -> pending_travel", states[3], "pending_travel")
        check("recovery: unreviewed untouched", states[4], "pending_llm")
        check("recovery: already correct untouched", states[5], "ready_to_write")
    finally:
        os.unlink(tmp_path)

    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DB_PATH), type=Path)
    p.add_argument("--prepare", action="store_true",
                   help="Query candidates and write .llm_batches.json for session review")
    p.add_argument("--apply", type=Path, metavar="RESULTS_FILE",
                   help="Read tier assignments from JSON file and write to SQLite")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N candidates (with --prepare)")
    p.add_argument("--smoke-test", action="store_true",
                   help="Run offline unit tests")
    args = p.parse_args()

    if args.smoke_test:
        return _smoke_tests()

    if args.prepare:
        return cmd_prepare(args.db, limit=args.limit)

    if args.apply:
        return cmd_apply(args.db, args.apply)

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
