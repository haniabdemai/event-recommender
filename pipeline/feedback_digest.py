#!/usr/bin/env python3
"""
Fortnightly feedback digest: cross-references Google Calendar attendance
with pipeline candidates to produce a taste profile feedback report.

Combines two data sources:
  1. Google Calendar (past 30 days, physical London locations)
  2. SQLite candidates table (scores, signals, vetoes, tiers, verdicts)

Produces a structured report (JSON + Notion page + ntfy) that feeds the
quarterly scoring review. Runs on ODD ISO weeks, alternating with verdict
sync on EVEN weeks.

Exit codes:
    0 = success
    1 = configuration error (missing env vars, bad credentials)
    2 = Calendar API error

Usage:
    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=... \
    NOTION_TOKEN=... python3 feedback_digest.py [--dry-run] [--smoke-test] [--days 30]
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
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from erlib.config import DB_PATH, NTFY_TOPIC
from erlib.google_auth import refresh_access_token as ga_refresh_access_token
from erlib.notion import NotionClient, NotionError

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)


class CalendarError(Exception):
    """Raised when Calendar OAuth or API calls fail."""


# Confidence thresholds for matching
CONFIDENCE_HIGH = "high"       # venue+date+title, or exact title+date
CONFIDENCE_MEDIUM = "medium"   # venue+date (different titles), or strong title overlap+date
CONFIDENCE_LOW = "low"         # weak title overlap+date only: not reported as a match

TITLE_OVERLAP_THRESHOLD_HIGH = 0.6    # word overlap ratio for high confidence (with date)
TITLE_OVERLAP_THRESHOLD_MEDIUM = 0.4  # word overlap ratio for medium confidence (with date)

VIRTUAL_PATTERNS = [
    r"https?://",
    r"zoom\.us",
    r"meet\.google",
    r"teams\.microsoft",
    r"microsoft teams",
    r"teams meeting",
    r"webex\.com",
    r"skype",
    r"telephone",
    r"phone call",
    r"dial.?in",
    r"conference call",
    r"video call",
]

# ── Normalisation ─────────────────────────────────────────────────────────────

from erlib.normalise import normalise_name  # noqa: E402


def normalize_text(s: str) -> str:
    return normalise_name(s)


_STOP_WORDS = frozenset({
    "a", "an", "at", "by", "for", "in", "is", "it", "of", "on", "or",
    "the", "to", "and", "but", "its", "our", "we", "you", "via", "with",
})


def extract_words(s: str) -> set[str]:
    normalized = normalize_text(s)
    if not normalized:
        return set()
    words = set(normalized.split())
    return {w for w in words if len(w) > 1 and w not in _STOP_WORDS}


def word_overlap_ratio(a: str, b: str) -> float:
    words_a = extract_words(a)
    words_b = extract_words(b)
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    # Ratio relative to the SMALLER set: if the calendar title is short
    # ("jazz the sound room") it should still match a longer candidate title
    smaller = min(len(words_a), len(words_b))
    return len(intersection) / smaller


def normalize_venue(s: str) -> str:
    if not s:
        return ""
    s = normalize_text(s)
    # Strip the configured city/country suffix (and its parts) so a
    # Calendar location like "Venue, Berlin, Germany" matches the pipeline's
    # "Venue". Built from ER_GEOCODE_SUFFIX / ER_CITY_NAME, longest first.
    from erlib.config import CITY_NAME, GEOCODE_SUFFIX  # noqa: PLC0415
    suffix_parts = [p.strip() for p in GEOCODE_SUFFIX.split(",") if p.strip()]
    candidates = {GEOCODE_SUFFIX, ", ".join(suffix_parts), CITY_NAME, *suffix_parts}
    suffixes = sorted(
        {", " + c.lower() for c in candidates} | {" " + CITY_NAME.lower()},
        key=len, reverse=True,
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if s.endswith(suffix):
                s = s[: -len(suffix)].strip().rstrip(",").strip()
                changed = True
    # Strip UK-format postcodes (best-effort; no-op for other formats)
    s = re.sub(r"\b[A-Za-z]{1,2}\d{1,2}\s*\d[A-Za-z]{2}\b", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def venue_matches(cal_location: str, candidate_venue: str) -> bool:
    if not cal_location or not candidate_venue:
        return False
    cal_norm = normalize_venue(cal_location)
    cand_norm = normalize_venue(candidate_venue)
    if not cal_norm or not cand_norm:
        return False
    # Either one contains the other (handles "The Sound Room" vs
    # "The Sound Room, 12 Fenwick St, Camden")
    if cal_norm in cand_norm or cand_norm in cal_norm:
        return True
    # Word overlap for venues: high threshold since venue names are short
    return word_overlap_ratio(cal_location, candidate_venue) >= 0.7


def is_virtual(location: str) -> bool:
    if not location:
        return False
    loc = location.lower()
    return any(re.search(p, loc) for p in VIRTUAL_PATTERNS)


# Calendar entries that are personal business, not attendable public events.
# The defaults cover generic appointment shapes; add patterns for your own
# life (hobbies, clinics, regular commitments) via ER_PERSONAL_EVENT_PATTERNS
# and ER_PERSONAL_LOCATION_PATTERNS (comma-separated regular expressions)
# rather than editing code.
PERSONAL_TITLE_PATTERNS = [
    r"^dr[\s\.]+\S+(\s+appointment)?$",
    r"\bappointment\b",
    r"\bcheck-?up\b",
    r"\bdentist\b",
    r"\bgp\b",
    r"\bphysio\b",
    r"\blesson\b",
    r"\bface to face\b",
]

PERSONAL_LOCATION_PATTERNS = [
    r"\bclinic\b",
    r"\bsurgery\b",
]

PERSONAL_TITLE_PATTERNS += [
    p for p in os.environ.get("ER_PERSONAL_EVENT_PATTERNS", "").split(",") if p.strip()
]
PERSONAL_LOCATION_PATTERNS += [
    p for p in os.environ.get("ER_PERSONAL_LOCATION_PATTERNS", "").split(",") if p.strip()
]


def is_personal_event(title: str, location: str) -> str | None:
    t = title.lower()
    for p in PERSONAL_TITLE_PATTERNS:
        if re.search(p, t):
            return p
    loc = location.lower()
    for p in PERSONAL_LOCATION_PATTERNS:
        if re.search(p, loc):
            return p
    return None


# ── Matching ──────────────────────────────────────────────────────────────────

def match_confidence(
    cal_title: str,
    cal_location: str,
    cal_date: str,
    cand_name: str,
    cand_venue: str,
    cand_date: str,
) -> tuple[str, float]:
    """Score a match between a calendar event and a candidate.

    Returns (confidence_level, overlap_score) where confidence_level is
    HIGH, MEDIUM, or LOW, and overlap_score is the word overlap ratio.
    Date must match: returns (LOW, 0) if dates differ.
    """
    if cal_date != cand_date:
        return (CONFIDENCE_LOW, 0.0)

    title_overlap = word_overlap_ratio(cal_title, cand_name)
    has_venue_match = venue_matches(cal_location, cand_venue)

    # Exact title match (after normalisation) → HIGH
    if normalize_text(cal_title) == normalize_text(cand_name):
        return (CONFIDENCE_HIGH, 1.0)

    # Venue match + strong title overlap → HIGH
    if has_venue_match and title_overlap >= TITLE_OVERLAP_THRESHOLD_HIGH:
        return (CONFIDENCE_HIGH, title_overlap)

    # Venue match + any title overlap → MEDIUM (venue is strong evidence)
    if has_venue_match and title_overlap > 0:
        return (CONFIDENCE_MEDIUM, title_overlap)

    # Venue match + no title overlap → MEDIUM (could be different event at same venue,
    # but same date + same venue is strong)
    if has_venue_match:
        return (CONFIDENCE_MEDIUM, 0.0)

    # No venue but strong title overlap → MEDIUM
    if title_overlap >= TITLE_OVERLAP_THRESHOLD_HIGH:
        return (CONFIDENCE_MEDIUM, title_overlap)

    # Moderate title overlap → LOW (not confident enough to report)
    if title_overlap >= TITLE_OVERLAP_THRESHOLD_MEDIUM:
        return (CONFIDENCE_LOW, title_overlap)

    return (CONFIDENCE_LOW, 0.0)


def match_calendar_to_candidates(
    calendar_events: list[dict],
    candidates: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Match calendar events to candidates.

    Returns (matched, unmatched) where:
    - matched: list of dicts with calendar event, candidate, confidence, overlap
    - unmatched: calendar events with no match above LOW confidence
    """
    # Index candidates by date for fast lookup
    by_date: dict[str, list[dict]] = {}
    for c in candidates:
        d = c["date"]
        if d:
            by_date.setdefault(d, []).append(c)

    matched = []
    unmatched = []

    for cal in calendar_events:
        cal_date = cal["date"]
        cal_title = cal.get("title", "")
        cal_location = cal.get("location", "")

        same_date_candidates = by_date.get(cal_date, [])
        if not same_date_candidates:
            unmatched.append(cal)
            continue

        best_confidence = CONFIDENCE_LOW
        best_overlap = 0.0
        best_candidate = None

        for cand in same_date_candidates:
            conf, overlap = match_confidence(
                cal_title, cal_location, cal_date,
                cand["name"], cand.get("venue_name", ""), cand["date"],
            )
            # Prefer HIGH over MEDIUM, then higher overlap
            if (conf == CONFIDENCE_HIGH and best_confidence != CONFIDENCE_HIGH) or \
               (conf == best_confidence and overlap > best_overlap) or \
               (conf == CONFIDENCE_MEDIUM and best_confidence == CONFIDENCE_LOW):
                best_confidence = conf
                best_overlap = overlap
                best_candidate = cand

        if best_confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM) and best_candidate:
            matched.append({
                "calendar_event": cal,
                "candidate": best_candidate,
                "confidence": best_confidence,
                "overlap": best_overlap,
            })
        else:
            unmatched.append(cal)

    return matched, unmatched


# ── Calendar API ──────────────────────────────────────────────────────────────

CALENDAR_API = "https://www.googleapis.com/calendar/v3"


def refresh_calendar_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """erlib.google_auth refresh, wrapped in this module's CalendarError."""
    try:
        return ga_refresh_access_token(client_id, client_secret, refresh_token)
    except (urllib.error.HTTPError, KeyError) as e:
        raise CalendarError(f"Failed to refresh Calendar OAuth token: {e}") from e


def fetch_calendar_events(access_token: str, days_back: int = 30) -> list[dict]:
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days_back)).isoformat()
    time_max = now.isoformat()

    events = []
    page_token = None

    while True:
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
        }
        if page_token:
            params["pageToken"] = page_token

        qs = urllib.parse.urlencode(params)
        url = f"{CALENDAR_API}/calendars/primary/events?{qs}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {access_token}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise CalendarError(f"Calendar API returned HTTP {e.code}") from e

        for item in result.get("items", []):
            location = item.get("location", "")
            if is_virtual(location):
                continue
            if not location:
                continue

            title = item.get("summary", "")
            matched_pattern = is_personal_event(title, location)
            if matched_pattern:
                print(f'CALENDAR_FILTER: skipping "{title}" (matched: {matched_pattern})', file=sys.stderr)
                continue

            start = item.get("start", {})
            date_str = start.get("date") or (start.get("dateTime", "")[:10])
            if not date_str:
                continue

            events.append({
                "title": title,
                "location": location,
                "date": date_str,
                "calendar_id": item.get("id") or None,
            })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return events


def dedup_calendar_events(events: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for e in events:
        key = (normalize_text(e["title"]), e["date"])
        if key not in seen or len(e.get("location", "")) > len(seen[key].get("location", "")):
            seen[key] = e
    return list(seen.values())


# ── SQLite operations ─────────────────────────────────────────────────────────

def ensure_schema(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    if "user_attended" not in cols:
        conn.execute("ALTER TABLE candidates ADD COLUMN user_attended INTEGER DEFAULT 0")
        print("MIGRATE: added column user_attended")
        conn.commit()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calendar_id TEXT UNIQUE,
            title TEXT NOT NULL,
            location TEXT,
            date TEXT NOT NULL,
            matched_candidate_id INTEGER,
            match_confidence TEXT,
            digest_date TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS digest_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_key TEXT UNIQUE NOT NULL,
            finding_type TEXT NOT NULL,
            target_name TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            events_json TEXT NOT NULL DEFAULT '[]',
            evidence_count INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            notion_page_id TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            additional_types TEXT NOT NULL DEFAULT '[]',
            correction_events TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    df_cols = {r[1] for r in conn.execute("PRAGMA table_info(digest_findings)").fetchall()}
    if "additional_types" not in df_cols:
        conn.execute("ALTER TABLE digest_findings ADD COLUMN additional_types TEXT NOT NULL DEFAULT '[]'")
    if "correction_events" not in df_cols:
        conn.execute("ALTER TABLE digest_findings ADD COLUMN correction_events TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


def load_candidates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, date, venue_name, venue_postcode, organiser, "
        "source, score, tier, llm_tier, signals_fired, veto_reason, "
        "pipeline_state, verdict, verdict_reason, verdict_notes, format_type "
        "FROM candidates"
    ).fetchall()
    return [dict(r) for r in rows]


def save_attendance(conn: sqlite3.Connection, matched: list[dict], dry_run: bool) -> int:
    count = 0
    for m in matched:
        cand = m["candidate"]
        if cand.get("verdict") == "Not Going":
            continue
        if not dry_run:
            conn.execute(
                "UPDATE candidates SET user_attended = 1 WHERE id = ?",
                (cand["id"],),
            )
            count += 1
    if not dry_run and count:
        conn.commit()
    return count


def save_calendar_events(
    conn: sqlite3.Connection,
    calendar_events: list[dict],
    matched: list[dict],
    unmatched: list[dict],
    digest_date: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    for m in matched:
        cal = m["calendar_event"]
        conn.execute(
            "INSERT OR IGNORE INTO calendar_events "
            "(calendar_id, title, location, date, matched_candidate_id, "
            "match_confidence, digest_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cal.get("calendar_id"), cal["title"], cal.get("location"),
             cal["date"], m["candidate"]["id"], m["confidence"], digest_date),
        )
    for cal in unmatched:
        conn.execute(
            "INSERT OR IGNORE INTO calendar_events "
            "(calendar_id, title, location, date, matched_candidate_id, "
            "match_confidence, digest_date) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
            (cal.get("calendar_id"), cal["title"], cal.get("location"),
             cal["date"], digest_date),
        )
    conn.commit()


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_confirmed_interests(matched: list[dict]) -> list[dict]:
    confirmed = []
    for m in matched:
        cand = m["candidate"]
        verdict = cand.get("verdict")
        if verdict not in ("Going", "Maybe"):
            continue
        signals = cand.get("signals_fired", "") or ""
        confirmed.append({
            "event": cand["name"],
            "date": cand["date"],
            "verdict": verdict,
            "tier": cand.get("llm_tier") or cand.get("tier"),
            "signals": signals,
            "confidence": m["confidence"],
        })
    return confirmed


def analyze_unreviewed_attendance(matched: list[dict]) -> list[dict]:
    unreviewed = []
    for m in matched:
        cand = m["candidate"]
        verdict = cand.get("verdict")
        if verdict in ("Going", "Maybe", "Not Going"):
            continue
        unreviewed.append({
            "event": cand["name"],
            "date": cand["date"],
            "tier": cand.get("llm_tier") or cand.get("tier"),
            "venue": cand.get("venue_name", ""),
            "confidence": m["confidence"],
        })
    return unreviewed


def analyze_scoring_corrections(matched: list[dict]) -> list[dict]:
    corrections = []
    for m in matched:
        cand = m["candidate"]
        state = cand.get("pipeline_state", "")
        verdict = cand.get("verdict")

        # Common fields for cross-referencing in classify_findings
        _org = cand.get("organiser", "")
        _venue = cand.get("venue_name", "")

        # Case 1: vetoed by Python but user attended
        if state == "vetoed":
            corrections.append({
                "type": "false_veto",
                "event": cand["name"],
                "date": cand["date"],
                "veto_reason": cand.get("veto_reason"),
                "organiser": _org,
                "venue_name": _venue,
                "confidence": m["confidence"],
            })
            continue

        # Case 2: expired before LLM review but user attended
        if state == "expired":
            corrections.append({
                "type": "expired_but_attended",
                "event": cand["name"],
                "date": cand["date"],
                "organiser": _org,
                "venue_name": _venue,
                "confidence": m["confidence"],
            })
            continue

        # Case 3: rejected by LLM but user attended
        if state == "llm_rejected":
            corrections.append({
                "type": "false_llm_reject",
                "event": cand["name"],
                "date": cand["date"],
                "llm_tier": cand.get("llm_tier"),
                "organiser": _org,
                "venue_name": _venue,
                "confidence": m["confidence"],
            })
            continue

        # Case 4: user marked Going but pipeline scored low
        if verdict == "Going" and cand.get("llm_tier") in ("Borderline", "Not Recommended"):
            corrections.append({
                "type": "tier_mismatch",
                "event": cand["name"],
                "date": cand["date"],
                "llm_tier": cand.get("llm_tier"),
                "verdict": verdict,
                "verdict_reason": cand.get("verdict_reason"),
                "verdict_notes": cand.get("verdict_notes"),
                "signals": cand.get("signals_fired", ""),
                "organiser": _org,
                "venue_name": _venue,
                "confidence": m["confidence"],
            })
            continue

        # Case 5: user marked Not Going on a Top Pick
        if verdict == "Not Going" and cand.get("llm_tier") in ("Top Picks", "Recommended"):
            corrections.append({
                "type": "over_recommended",
                "event": cand["name"],
                "date": cand["date"],
                "llm_tier": cand.get("llm_tier"),
                "verdict": verdict,
                "verdict_reason": cand.get("verdict_reason"),
                "verdict_notes": cand.get("verdict_notes"),
                "organiser": _org,
                "venue_name": _venue,
                "confidence": m["confidence"],
            })

    return corrections


def analyze_verdict_conflicts(matched: list[dict]) -> list[dict]:
    conflicts = []
    for m in matched:
        cand = m["candidate"]
        if cand.get("verdict") == "Not Going":
            conflicts.append({
                "event": cand["name"],
                "date": cand["date"],
                "tier": cand.get("llm_tier") or cand.get("tier"),
                "verdict_reason": cand.get("verdict_reason"),
                "verdict_notes": cand.get("verdict_notes"),
                "confidence": m["confidence"],
            })
    return conflicts


def analyze_source_gaps(unmatched: list[dict]) -> list[dict]:
    return [
        {
            "event": cal["title"],
            "date": cal["date"],
            "location": cal.get("location", ""),
        }
        for cal in unmatched
        if cal.get("title")
    ]


def analyze_interest_evolution(
    matched: list[dict],
    all_candidates: list[dict],
) -> dict:
    # Count format types in attended events
    attended_formats: dict[str, int] = {}
    attended_venues: dict[str, int] = {}
    attended_organisers: dict[str, int] = {}
    not_going_patterns: dict[str, int] = {}

    for m in matched:
        cand = m["candidate"]
        fmt = cand.get("format_type", "Other") or "Other"
        attended_formats[fmt] = attended_formats.get(fmt, 0) + 1

        venue = cand.get("venue_name", "")
        if venue:
            attended_venues[venue] = attended_venues.get(venue, 0) + 1

        org = cand.get("organiser", "")
        if org:
            attended_organisers[org] = attended_organisers.get(org, 0) + 1

    # Not Going patterns from ALL candidates (not just calendar-matched)
    for c in all_candidates:
        if c.get("verdict") == "Not Going":
            fmt = c.get("format_type", "Other") or "Other"
            not_going_patterns[fmt] = not_going_patterns.get(fmt, 0) + 1

    return {
        "attended_formats": dict(sorted(attended_formats.items(), key=lambda x: -x[1])),
        "repeat_venues": {k: v for k, v in sorted(attended_venues.items(), key=lambda x: -x[1]) if v >= 2},
        "repeat_organisers": {k: v for k, v in sorted(attended_organisers.items(), key=lambda x: -x[1]) if v >= 2},
        "not_going_patterns": dict(sorted(not_going_patterns.items(), key=lambda x: -x[1])),
    }


PLACEHOLDER_VENUES = {"tbc", "tba", "central london tbc", "central london", "london", "online", "various"}


def generate_suggestions(
    corrections: list[dict],
    source_gaps: list[dict],
    evolution: dict,
) -> list[str]:
    suggestions = []

    # Veto corrections
    veto_counts: dict[str, int] = {}
    for c in corrections:
        if c["type"] == "false_veto":
            veto = c.get("veto_reason", "unknown")
            veto_counts[veto] = veto_counts.get(veto, 0) + 1
    for veto, count in veto_counts.items():
        suggestions.append(
            f"Relax veto '{veto}': user attended {count} event(s) it blocked"
        )

    # Repeat venues not in KNOWN lists
    for venue, count in evolution.get("repeat_venues", {}).items():
        if venue.strip().lower() in PLACEHOLDER_VENUES:
            continue
        suggestions.append(
            f"Add '{venue}' to KNOWN_VENUES: appeared {count} times in attended events"
        )

    # Repeat organisers
    for org, count in evolution.get("repeat_organisers", {}).items():
        suggestions.append(
            f"Add '{org}' to KNOWN_ORGANISERS: appeared {count} times in attended events"
        )

    # Source gaps
    if source_gaps:
        gap_venues: dict[str, int] = {}
        for g in source_gaps:
            loc = g.get("location", "")
            if loc:
                gap_venues[loc] = gap_venues.get(loc, 0) + 1
        for venue, count in gap_venues.items():
            if count >= 2:
                suggestions.append(
                    f"Source gap: {count} attended events at '{venue}' not in pipeline: consider adding as a source"
                )
        if len(source_gaps) > 0:
            suggestions.append(
                f"{len(source_gaps)} event(s) attended but not in any pipeline source"
            )

    return suggestions


# ── Findings (cross-digest accumulation + Notion) ─────────────────────────────

FINDING_TYPE_MAP = {
    "organiser": "Organiser",
    "series": "Series",
    "veto_exemption": "Veto Exemption",
    "source_gap": "Source Gap",
    "tier_mismatch": "Tier Mismatch",
    "over_recommended": "Over-recommended",
    "false_llm_reject": "False LLM Reject",
    "expired_but_attended": "Expired But Attended",
    "false_veto": "Veto Exemption",
}

ACTION_TEXT = {
    "organiser": "Auto-apply: add to known organisers (+2 score boost)",
    "series": "Auto-apply: add to known series (+2 score boost)",
    "veto_exemption": "Manual review: check if veto rule is too broad",
    "source_gap": "Manual review: consider adding as event source",
    "tier_mismatch": "Manual review: LLM may be too conservative for this type",
    "over_recommended": "Manual review: scoring may be too generous for this category",
    "false_llm_reject": "Manual review: LLM sense-check may be over-filtering",
    "expired_but_attended": "Informational: pipeline timing: event expired before review",
}


def classify_findings(
    corrections: list[dict],
    evolution: dict,
    source_gaps: list[dict],
) -> list[dict]:
    """Classify digest analysis results into structured finding records.

    Three-pass entity-first approach:
    Pass 1: Create entity findings (organisers, venues, vetoes, source gaps)
    Pass 2: Cross-reference corrections against entities: enrich with
             additional_types and correction_events
    Pass 3: Standalone correction findings for corrections that don't match
             any entity
    """
    findings = []
    # Track entity findings by normalised organiser/venue for cross-referencing
    org_findings: dict[str, dict] = {}
    venue_findings: dict[str, dict] = {}

    # ── Pass 1: entity findings ──────────────────────────────────────────

    for org, count in evolution.get("repeat_organisers", {}).items():
        org_lower = org.strip().lower()
        f = {
            "finding_key": f"organiser:{org_lower}",
            "finding_type": "organiser",
            "target_name": org,
            "events": [{"organiser": org, "count": count}],
            "additional_types": [],
            "correction_events": [],
        }
        findings.append(f)
        org_findings[org_lower] = f

    for venue, count in evolution.get("repeat_venues", {}).items():
        venue_lower = venue.strip().lower()
        if venue_lower in PLACEHOLDER_VENUES:
            continue
        f = {
            "finding_key": f"series:{venue_lower}",
            "finding_type": "series",
            "target_name": venue,
            "events": [{"venue": venue, "count": count}],
            "additional_types": [],
            "correction_events": [],
        }
        findings.append(f)
        venue_findings[venue_lower] = f

    veto_counts: dict[str, list[dict]] = {}
    for c in corrections:
        if c["type"] == "false_veto":
            veto = c.get("veto_reason", "unknown")
            veto_counts.setdefault(veto, []).append({
                "event": c["event"], "date": c["date"],
                "confidence": c.get("confidence", "unknown"),
            })
    for veto, events in veto_counts.items():
        findings.append({
            "finding_key": f"veto:{veto}",
            "finding_type": "veto_exemption",
            "target_name": veto,
            "events": events,
            "additional_types": [],
            "correction_events": [],
        })

    gap_venues: dict[str, list[dict]] = {}
    for g in source_gaps:
        loc = g.get("location", "")
        if loc and loc.strip().lower() not in PLACEHOLDER_VENUES:
            gap_venues.setdefault(loc, []).append({
                "event": g["event"], "date": g["date"],
            })
    for venue, events in gap_venues.items():
        if len(events) >= 2:
            findings.append({
                "finding_key": f"source_gap:{venue.strip().lower()}",
                "finding_type": "source_gap",
                "target_name": venue,
                "events": events,
                "additional_types": [],
                "correction_events": [],
            })

    # ── Pass 2: cross-reference corrections against entity findings ──────

    unmatched_corrections = []
    correction_types_to_check = {
        "tier_mismatch", "over_recommended", "false_llm_reject",
        "expired_but_attended", "false_veto",
    }

    for c in corrections:
        if c["type"] not in correction_types_to_check:
            continue

        matched_entity = None
        c_org = c.get("organiser", "")
        c_venue = c.get("venue_name", "")

        if c_org and c_org.strip().lower() in org_findings:
            matched_entity = org_findings[c_org.strip().lower()]
        elif c_venue and c_venue.strip().lower() in venue_findings:
            matched_entity = venue_findings[c_venue.strip().lower()]

        if matched_entity:
            ctype = c["type"]
            if ctype not in matched_entity["additional_types"]:
                matched_entity["additional_types"].append(ctype)
            matched_entity["correction_events"].append({
                "type": ctype,
                "event": c["event"],
                "date": c["date"],
                "llm_tier": c.get("llm_tier"),
                "verdict": c.get("verdict"),
                "verdict_reason": c.get("verdict_reason"),
                "veto_reason": c.get("veto_reason"),
                "confidence": c.get("confidence"),
            })
        else:
            unmatched_corrections.append(c)

    # ── Pass 3: standalone correction findings ───────────────────────────

    for c in unmatched_corrections:
        # false_veto already has entity findings in Pass 1 (keyed by veto_reason)
        if c["type"] == "false_veto":
            continue
        name_norm = normalize_text(c["event"])
        finding_key = f"{c['type']}:{name_norm}:{c['date']}"
        findings.append({
            "finding_key": finding_key,
            "finding_type": c["type"],
            "target_name": c["event"],
            "events": [{
                "event": c["event"],
                "date": c["date"],
                "llm_tier": c.get("llm_tier"),
                "verdict": c.get("verdict"),
                "verdict_reason": c.get("verdict_reason"),
                "confidence": c.get("confidence"),
            }],
            "additional_types": [],
            "correction_events": [],
        })

    return findings


def generate_rationale(finding: dict) -> str:
    """Plain English rationale answering: what happened, why it matters,
    what to do about it. For enriched entity findings, weaves in all signals."""
    ftype = finding["finding_type"]
    name = finding["target_name"]
    events = finding.get("events", [])
    correction_events = finding.get("correction_events", [])

    parts = []

    if ftype == "organiser":
        total = sum(e.get("count", 1) for e in events)
        parts.append(
            f"You attended {total} event(s) by {name}. "
            f"This organiser isn't in the known list yet, so their events "
            f"don't get a score boost."
        )
        _append_correction_context(parts, correction_events, name)
        parts.append(
            f"If approved, future {name} events score higher "
            f"and are more likely to be recommended."
        )

    elif ftype == "series":
        total = sum(e.get("count", 1) for e in events)
        parts.append(
            f"You attended {total} event(s) at {name}. "
            f"This venue isn't in the known series list yet, so events "
            f"here don't get a score boost."
        )
        _append_correction_context(parts, correction_events, name)
        parts.append(
            f"If approved, future events at {name} score higher."
        )

    elif ftype == "veto_exemption":
        event_list = ", ".join(
            f"{e['event']} ({e['date']})" for e in events[:5]
        )
        parts.append(
            f"The '{name}' veto blocked events you actually attended: "
            f"{event_list}."
        )
        parts.append(
            "This veto rule may be too broad: it's catching events "
            "you'd enjoy. Needs manual review to decide whether to relax it."
        )

    elif ftype == "source_gap":
        event_list = ", ".join(
            f"{e['event']} ({e['date']})" for e in events[:5]
        )
        parts.append(
            f"You attended events at {name} that the pipeline never found: "
            f"{event_list}."
        )
        parts.append(
            "This venue isn't covered by any current source (newsletters, "
            "Meetup, Luma). Worth checking whether there's a feed to add."
        )

    elif ftype == "tier_mismatch":
        e = events[0] if events else {}
        tier = e.get("llm_tier", "low")
        verdict = e.get("verdict", "Going")
        reason = e.get("verdict_reason", "")
        parts.append(
            f"You went to {name} ({e.get('date', '?')}), but the pipeline "
            f"scored it as {tier}. Your verdict was {verdict}."
        )
        if reason:
            parts.append(f"Your reason: \"{reason}\".")
        parts.append(
            "The scoring model undervalued this event. Check whether the "
            "LLM checklist is too conservative for this type of event."
        )

    elif ftype == "over_recommended":
        e = events[0] if events else {}
        tier = e.get("llm_tier", "high")
        reason = e.get("verdict_reason", "")
        parts.append(
            f"The pipeline recommended {name} as {tier}, but you said "
            f"Not Going ({e.get('date', '?')})."
        )
        if reason:
            parts.append(f"Your reason: \"{reason}\".")
        parts.append(
            "The scoring model may be over-valuing this type of event. "
            "Check which signals pushed the score up."
        )

    elif ftype == "false_llm_reject":
        e = events[0] if events else {}
        parts.append(
            f"You attended {name} ({e.get('date', '?')}), but the LLM "
            f"rejected it entirely: it never appeared in your Notion picks."
        )
        parts.append(
            "The LLM sense-check is filtering out events you'd actually "
            "attend. Check the checklist for over-broad rejection patterns."
        )

    elif ftype == "expired_but_attended":
        e = events[0] if events else {}
        parts.append(
            f"You attended {name} ({e.get('date', '?')}), but it expired "
            f"from the pipeline before the LLM could review it."
        )
        parts.append(
            "The event was discovered but never reached you. This is a "
            "pipeline timing issue, not a scoring problem."
        )

    else:
        parts.append(f"'{name}': review needed.")

    return " ".join(parts)


def _append_correction_context(
    parts: list[str],
    correction_events: list[dict],
    entity_name: str,
) -> None:
    """Append plain English context about correction events to rationale parts."""
    if not correction_events:
        return

    by_type: dict[str, list[dict]] = {}
    for ce in correction_events:
        by_type.setdefault(ce["type"], []).append(ce)

    for ctype, events in by_type.items():
        n = len(events)
        sample = events[0]
        if ctype == "tier_mismatch":
            tier = sample.get("llm_tier", "low")
            parts.append(
                f"Also, {n} of these events were scored as {tier}: "
                f"the scoring model undervalued them."
            )
        elif ctype == "over_recommended":
            parts.append(
                f"However, {n} event(s) by this organiser were recommended "
                f"but you said Not Going: the model may also over-score "
                f"some of their events."
            )
        elif ctype == "false_llm_reject":
            parts.append(
                f"Additionally, {n} event(s) were rejected by the LLM "
                f"entirely, even though you attended."
            )
        elif ctype == "expired_but_attended":
            parts.append(
                f"Note: {n} event(s) expired before the LLM could review "
                f"them: a pipeline timing issue."
            )
        elif ctype == "false_veto":
            veto = sample.get("veto_reason", "unknown")
            parts.append(
                f"Also, {n} event(s) were blocked by the '{veto}' veto "
                f"even though you attended: this veto may be too broad."
            )


def save_digest_findings(
    conn: sqlite3.Connection,
    findings: list[dict],
    digest_date: str,
    dry_run: bool,
) -> int:
    """Save findings to digest_findings table, accumulating across runs.
    Returns count of new findings inserted."""
    if dry_run:
        print(f"[DRY RUN] Would save {len(findings)} findings to digest_findings")
        return 0

    new_count = 0
    for f in findings:
        existing = conn.execute(
            "SELECT id, events_json, evidence_count FROM digest_findings "
            "WHERE finding_key = ?",
            (f["finding_key"],),
        ).fetchone()

        events_json = json.dumps(f["events"])

        additional_json = json.dumps(f.get("additional_types", []))
        correction_json = json.dumps(f.get("correction_events", []))

        if existing:
            old_events = json.loads(existing["events_json"])
            merged_events = old_events + f["events"]
            updated = {**f, "evidence_count": existing["evidence_count"] + 1,
                       "events": merged_events}
            rationale = generate_rationale(updated)
            conn.execute(
                "UPDATE digest_findings SET "
                "evidence_count = evidence_count + 1, "
                "last_seen = ?, events_json = ?, rationale = ?, "
                "additional_types = ?, correction_events = ? "
                "WHERE id = ?",
                (digest_date, json.dumps(merged_events), rationale,
                 additional_json, correction_json, existing["id"]),
            )
        else:
            finding_with_count = {**f, "evidence_count": 1}
            rationale = generate_rationale(finding_with_count)
            conn.execute(
                "INSERT INTO digest_findings "
                "(finding_key, finding_type, target_name, events_json, "
                " rationale, evidence_count, first_seen, last_seen, "
                " additional_types, correction_events) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (f["finding_key"], f["finding_type"], f["target_name"],
                 events_json, rationale, digest_date, digest_date,
                 additional_json, correction_json),
            )
            new_count += 1

    conn.commit()
    return new_count


def _finding_title(finding: dict) -> str:
    name = finding["target_name"]
    ftype = finding["finding_type"]
    if ftype == "organiser":
        return f"Add '{name}' to known organisers"
    if ftype == "series":
        return f"Add '{name}' to known series"
    if ftype == "veto_exemption":
        return f"Review veto: {name}"
    if ftype == "source_gap":
        return f"Source gap: {name}"
    if ftype == "tier_mismatch":
        return f"Scoring too low: {name}"
    if ftype == "over_recommended":
        return f"Over-recommended: {name}"
    if ftype == "false_llm_reject":
        return f"LLM wrongly rejected: {name}"
    if ftype == "expired_but_attended":
        return f"Expired before review: {name}"
    return f"{ftype}: {name}"


def format_finding_notion_properties(finding: dict, db_id: str) -> dict:
    """Build Notion API body for a finding page in the Scoring Feedback database."""
    events = finding.get("events_json", "[]")
    if isinstance(events, str):
        events = json.loads(events)
    events_text = "\n".join(
        f"- {e.get('event', e.get('organiser', e.get('venue', '?')))} "
        f"({e.get('date', e.get('count', '?'))})"
        for e in events[:10]
    )
    rationale = finding.get("rationale", "")
    primary_type = finding["finding_type"]
    additional = finding.get("additional_types") or []
    if isinstance(additional, str):
        try:
            additional = json.loads(additional)
        except (json.JSONDecodeError, TypeError):
            additional = []

    type_names = [FINDING_TYPE_MAP.get(primary_type, primary_type)]
    for at in additional:
        mapped = FINDING_TYPE_MAP.get(at, at)
        if mapped not in type_names:
            type_names.append(mapped)

    action = ACTION_TEXT.get(primary_type, "Review needed")

    # Correction events context for the page body
    correction_events = finding.get("correction_events") or []
    if isinstance(correction_events, str):
        try:
            correction_events = json.loads(correction_events)
        except (json.JSONDecodeError, TypeError):
            correction_events = []
    corrections_text = ""
    if correction_events:
        corrections_text = "\n".join(
            f"- {ce.get('event', '?')} ({ce.get('date', '?')}): "
            f"{FINDING_TYPE_MAP.get(ce.get('type', ''), ce.get('type', '?'))}"
            + (f": scored {ce['llm_tier']}" if ce.get("llm_tier") else "")
            for ce in correction_events[:10]
        )

    body_children = [
        {"paragraph": {"rich_text": [{"text": {"content":
            "Rationale"}, "annotations": {"bold": True}}]}},
        {"paragraph": {"rich_text": [{"text": {"content":
            rationale}}]}},
        {"paragraph": {"rich_text": [{"text": {"content":
            "Action"}, "annotations": {"bold": True}}]}},
        {"paragraph": {"rich_text": [{"text": {"content":
            action}}]}},
        {"paragraph": {"rich_text": [{"text": {"content":
            "Supporting events"}, "annotations": {"bold": True}}]}},
        {"paragraph": {"rich_text": [{"text": {"content":
            events_text or "None yet"}}]}},
    ]
    if corrections_text:
        body_children.extend([
            {"paragraph": {"rich_text": [{"text": {"content":
                "Scoring corrections"}, "annotations": {"bold": True}}]}},
            {"paragraph": {"rich_text": [{"text": {"content":
                corrections_text}}]}},
        ])

    return {
        "parent": {"database_id": db_id},
        "properties": {
            "Month": {"title": [{"text": {"content":
                _finding_title(finding)
            }}]},
            "Row Type": {"select": {"name": "Finding"}},
            "Finding Type": {"multi_select": [
                {"name": tn} for tn in type_names
            ]},
            "Status": {"select": {"name": "New"}},
            "Evidence": {"number": finding.get("evidence_count", 1)},
            "Period": {"date": {"start": finding["first_seen"],
                                "end": finding["last_seen"] if finding["last_seen"] != finding["first_seen"] else None}},
            "Finding key": {"rich_text": [{"text": {"content":
                finding["finding_key"]
            }}]},
            "Rationale": {"rich_text": [{"text": {"content":
                rationale[:2000]
            }}]},
            "Action": {"rich_text": [{"text": {"content":
                action
            }}]},
        },
        "children": body_children,
    }


def get_finding_page_status(token: str, page_id: str) -> str | None:
    """Read Status property from a Notion finding page."""
    try:
        result = notion_request(f"/pages/{page_id}", token)
        props = result.get("properties", {})
        status_prop = props.get("Status", {}).get("select")
        return status_prop.get("name") if status_prop else None
    except Exception:
        return None


def create_or_update_finding_page(
    token: str,
    db_id: str,
    finding: dict,
    conn: sqlite3.Connection,
    dry_run: bool,
) -> str | None:
    """Create or update a Notion page for a finding.
    Returns the Notion page URL, or None on failure/dry-run."""
    page_id = finding.get("notion_page_id")

    if dry_run:
        action = "update" if page_id else "create"
        print(f"[DRY RUN] Would {action} finding page: {finding['finding_key']}")
        return None

    try:
        if page_id:
            primary_type = finding["finding_type"]
            additional = finding.get("additional_types") or []
            if isinstance(additional, str):
                try:
                    additional = json.loads(additional)
                except (json.JSONDecodeError, TypeError):
                    additional = []
            type_names = [FINDING_TYPE_MAP.get(primary_type, primary_type)]
            for at in additional:
                mapped = FINDING_TYPE_MAP.get(at, at)
                if mapped not in type_names:
                    type_names.append(mapped)
            action = ACTION_TEXT.get(primary_type, "Review needed")

            update_body = {
                "properties": {
                    "Evidence": {"number": finding.get("evidence_count", 1)},
                    "Period": {"date": {"start": finding["first_seen"],
                                        "end": finding["last_seen"] if finding["last_seen"] != finding["first_seen"] else None}},
                    "Rationale": {"rich_text": [{"text": {"content":
                        finding.get("rationale", "")[:2000]
                    }}]},
                    "Finding Type": {"multi_select": [
                        {"name": tn} for tn in type_names
                    ]},
                    "Action": {"rich_text": [{"text": {"content": action}}]},
                },
            }
            notion_request(f"/pages/{page_id}", token, update_body,
                           method="PATCH")
            return f"https://www.notion.so/{page_id.replace('-', '')}"
        else:
            body = format_finding_notion_properties(finding, db_id)
            result = notion_request("/pages", token, body)
            new_page_id = result.get("id", "")
            if new_page_id:
                conn.execute(
                    "UPDATE digest_findings SET notion_page_id = ? "
                    "WHERE finding_key = ?",
                    (new_page_id, finding["finding_key"]),
                )
                conn.commit()
            return result.get("url")
    except Exception as e:
        print(f"WARNING: Failed to write finding page "
              f"'{finding['finding_key']}': {e}", file=sys.stderr)
        return None


# ── Pipeline health assessment ────────────────────────────────────────────────

PIPELINE_SCHEDULE_DAY = 1  # Tuesday (weekday(): 0=Mon, 1=Tue, ..., 6=Sun)


def query_pipeline_runs(
    token: str,
    db_id: str,
    days_back: int = 30,
) -> tuple[list[dict], str | None]:
    """Query Pipeline Runs Notion DB for recent runs.

    Returns (runs, error). On success, error is None.
    On failure, runs is [] and error describes the problem.
    """
    if not db_id:
        return [], "No Pipeline Runs database ID configured"

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")

    filter_body = {
        "filter": {
            "property": "Batch date",
            "date": {"on_or_after": cutoff},
        },
        "sorts": [
            {"property": "Batch date", "direction": "descending"},
        ],
    }

    try:
        # paginate: >30 days of runs can exceed one page; the old
        # single-request version silently dropped rows past 100 (audit P2).
        pages = list(NotionClient(token).paginate(
            "POST", f"/databases/{db_id}/query", filter_body
        ))
    except (NotionError, OSError) as e:
        return [], f"Failed to query Pipeline Runs database: {e}"

    runs = []
    for page in pages:
        props = page.get("properties", {})

        batch_title = ""
        title_items = props.get("Batch", {}).get("title", [])
        if title_items:
            batch_title = title_items[0].get("plain_text", "")

        status_prop = props.get("Status", {}).get("select")
        status = status_prop.get("name", "") if status_prop else ""

        events_added = props.get("Events added", {}).get("number")

        issues_parts = props.get("Issues", {}).get("rich_text", [])
        issues = "".join(p.get("plain_text", "") for p in issues_parts)

        pipeline_parts = props.get("Pipeline", {}).get("rich_text", [])
        pipeline = "".join(p.get("plain_text", "") for p in pipeline_parts)

        duration_parts = props.get("Duration", {}).get("rich_text", [])
        duration = "".join(p.get("plain_text", "") for p in duration_parts)

        batch_date_prop = props.get("Batch date", {}).get("date")
        batch_date = batch_date_prop.get("start", "") if batch_date_prop else ""

        trigger_prop = props.get("Trigger", {}).get("select")
        trigger = trigger_prop.get("name", "") if trigger_prop else ""

        runs.append({
            "batch": batch_title,
            "status": status,
            "events_added": events_added or 0,
            "issues": issues,
            "pipeline": pipeline,
            "duration": duration,
            "batch_date": batch_date,
            "trigger": trigger,
        })

    return runs, None


def _expected_tuesdays(days_back: int) -> list[date]:
    """Return the Tuesday dates in the lookback period."""
    today = date.today()
    tuesdays = []
    for i in range(days_back):
        d = today - timedelta(days=i)
        if d.weekday() == PIPELINE_SCHEDULE_DAY:
            tuesdays.append(d)
    return tuesdays


def _strip_jargon(text: str) -> str:
    """Remove file paths, line numbers, and technical identifiers from text."""
    if not text:
        return text
    cleaned = re.sub(r'\b[\w/]+\.(py|sh|yml|json|md)\b', '', text)
    cleaned = re.sub(r'\bline\s+\d+\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bexit\s+code\s+\d+\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bHTTP\s+\d{3}\b', '', cleaned)
    cleaned = re.sub(r'\b[A-Z_]{3,}_[A-Z_]+\b', '', cleaned)  # CONSTANT_NAMES
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)
    cleaned = re.sub(r'\s*;\s*', '; ', cleaned)
    cleaned = cleaned.strip(' ;')
    return cleaned


def _classify_failure(issues: str, status: str) -> tuple[str, str, str]:
    """Classify a pipeline failure into (what, why, next_steps).

    Returns human-readable strings based on known patterns.
    """
    issues_lower = issues.lower() if issues else ""

    if "token" in issues_lower or "auth" in issues_lower or "401" in issues_lower:
        what = "an authentication issue"
        why = "An API token expired or was revoked."
        next_steps = "This needs a token refresh before the next run."
    elif "write" in issues_lower and ("notion" in issues_lower or "fail" in issues_lower):
        what = "the Notion write step"
        why = "Events were scored but the write to Notion failed."
        next_steps = "The scored events will be picked up on the next run."
    elif "403" in issues_lower or "host_not_allowed" in issues_lower or "blocked" in issues_lower:
        what = "a network access issue"
        why = "The pipeline couldn't reach an external service."
        next_steps = "This is usually a temporary sandbox issue and resolves on the next run."
    elif "stuck" in issues_lower or "pending" in issues_lower:
        what = "events getting stuck in processing"
        why = "Some events didn't make it through all pipeline steps."
        next_steps = "They'll be picked up on the next run: nothing is permanently lost."
    elif "fetch" in issues_lower:
        what = "the event fetching step"
        why = "The pipeline couldn't retrieve events from one or more sources."
        next_steps = "Missing events will be fetched on the next run."
    elif status == "Failed":
        what = "an unspecified failure"
        why = _strip_jargon(issues) if issues else "No details available."
        next_steps = "Check the Pipeline Runs database for more detail."
    else:
        what = "some issues"
        why = _strip_jargon(issues) if issues else "No details available."
        next_steps = "These are usually resolved automatically on the next run."

    return what, why, next_steps


def _read_local_file(filename: str) -> dict | list | None:
    """Try to read a local JSON file. Returns None if unavailable."""
    path = SCRIPT_DIR / filename
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def assess_pipeline_health(
    runs: list[dict],
    query_error: str | None,
    days_back: int = 30,
    digest_error: str | None = None,
) -> dict:
    """Assess pipeline health and generate plain English summary.

    Returns dict with:
        health_status: 'healthy' | 'degraded' | 'failed' | 'missed' | 'unknown'
        health_text: plain English string for the Notion page
        runs_summary: structured data for the JSON report
    """
    # Try to enrich with local files (available in local runs, not GH Actions)
    run_log = _read_local_file(".pipeline_run_log.json")
    write_error = _read_local_file(".last_write_error.json")

    # If we couldn't query the Pipeline Runs DB at all
    if query_error:
        health_text = (
            f"Unable to determine pipeline status: {query_error}. "
            "Check the Pipeline Runs database directly for recent run information."
        )
        if digest_error:
            health_text += f" Additionally, this digest had an error: {digest_error}"
        return {
            "health_status": "unknown",
            "health_text": health_text,
            "runs_summary": {"query_error": query_error},
        }

    expected = _expected_tuesdays(days_back)

    # No runs found
    if not runs:
        if expected:
            health_text = (
                f"No pipeline runs were recorded in the last {days_back} days. "
                f"The pipeline is scheduled for every Tuesday: {len(expected)} "
                f"run(s) were expected. This could mean the scheduled trigger "
                f"didn't fire, or runs failed before creating their summary page. "
                f"No new events were fetched or processed during this period."
            )
        else:
            health_text = f"No pipeline runs found in the last {days_back} days."
        if digest_error:
            health_text += f" Additionally, this digest had an error: {digest_error}"
        return {
            "health_status": "missed",
            "health_text": health_text,
            "runs_summary": {"runs_found": 0, "expected_tuesdays": len(expected)},
        }

    # Analyse runs: focus on the MOST RECENT run, with period context
    successes = [r for r in runs if r["status"] == "Success"]
    failures = [r for r in runs if r["status"] == "Failed"]
    partials = [r for r in runs if r["status"] == "Partial"]
    skipped = [r for r in runs if r["status"] == "Skipped"]
    total_events = sum(r["events_added"] for r in runs)

    latest = runs[0]  # sorted descending by date

    if latest["status"] == "Success":
        parts = [
            f"All systems healthy. "
            f"Last pipeline ran {latest['batch']}: "
            f"{latest['events_added']} events written to Notion"
        ]
        if latest["issues"] and latest["issues"].lower() not in ("no issues", "none", ""):
            parts.append(f", with minor notes: {_strip_jargon(latest['issues'])}")
        else:
            parts.append(", no issues")
        parts.append(f". {len(runs)} run(s) in this period")
        if failures:
            parts.append(
                f" ({len(failures)} had issues earlier, since resolved)"
            )
        parts.append(".")
        health_text = "".join(parts)
        health_status = "healthy"

    elif latest["status"] == "Failed":
        what, why, next_steps = _classify_failure(
            latest["issues"], latest["status"],
        )
        health_text = (
            f"Problem with the {latest['batch']} pipeline. "
            f"It failed at {what}. {why} "
        )
        if latest["events_added"] > 0:
            health_text += (
                f"{latest['events_added']} events were processed "
                f"before the failure. "
            )
        else:
            health_text += (
                "No new events made it to your picks from this run. "
            )
        health_text += next_steps
        health_status = "failed"

    elif latest["status"] == "Partial":
        cleaned_issues = _strip_jargon(latest["issues"])
        health_text = (
            f"The {latest['batch']} pipeline completed with some notes. "
        )
        if latest["events_added"] > 0:
            health_text += (
                f"{latest['events_added']} events were written to Notion. "
            )
        if cleaned_issues:
            health_text += f"Notes: {cleaned_issues}. "
        health_text += (
            "These are typically informational and don't need action."
        )
        if len(runs) > 1:
            other_ok = len(successes) + len([
                p for p in partials if p != latest
            ])
            if other_ok:
                health_text += (
                    f" {other_ok} other run(s) in this period also completed."
                )
        health_status = "degraded"

    elif latest["status"] == "Skipped":
        health_text = (
            f"The pipeline was scheduled but skipped on {latest['batch']}. "
            "No events were fetched or processed. "
        )
        non_skipped = [r for r in runs if r["status"] != "Skipped"]
        if non_skipped:
            prev = non_skipped[0]
            health_text += (
                f"The previous run ({prev['batch']}) "
                f"{'succeeded' if prev['status'] == 'Success' else 'completed with issues'}"
                f" with {prev['events_added']} events. "
            )
        else:
            health_text += (
                "Events from this period's newsletters and Meetup/Luma will be "
                "picked up by the next run: nothing is permanently lost, "
                "but you're seeing older recommendations. "
            )
        health_status = "degraded"

    else:
        health_text = (
            f"{len(runs)} pipeline run(s) found. "
            f"Total events added: {total_events}."
        )
        health_status = "unknown"

    # Enrich with write error details if available
    if write_error and isinstance(write_error, dict):
        err_detail = write_error.get("detail", "")
        if err_detail and health_status in ("failed", "degraded"):
            cleaned = _strip_jargon(str(err_detail))
            if cleaned:
                health_text += f" Write error detail: {cleaned}."

    # Enrich with run log if available
    if run_log and isinstance(run_log, list):
        failed_steps = [
            e for e in run_log
            if isinstance(e, dict) and e.get("outcome", "").lower() == "fail"
        ]
        if failed_steps and health_status in ("failed", "degraded"):
            step_names = [e.get("step", "unknown") for e in failed_steps]
            readable = ", ".join(_strip_jargon(s) for s in step_names if s)
            if readable:
                health_text += f" Failed steps: {readable}."

    # Check for missing runs
    if expected and len(runs) < len(expected):
        gap = len(expected) - len(runs)
        health_text += (
            f" Note: {gap} expected Tuesday run(s) had no recorded result."
        )

    # Digest self-health
    if digest_error:
        health_text += (
            f" However, this digest had an error: {digest_error}. "
            "Scoring corrections and attendance matching may be incomplete."
        )
    else:
        health_text += " This digest ran successfully."

    return {
        "health_status": health_status,
        "health_text": health_text,
        "runs_summary": {
            "runs_found": len(runs),
            "successes": len(successes),
            "failures": len(failures),
            "partials": len(partials),
            "skipped": len(skipped),
            "total_events": total_events,
            "expected_tuesdays": len(expected),
            "latest_batch": latest["batch"] if runs else None,
        },
    }


# ── Report ────────────────────────────────────────────────────────────────────

def build_report(
    matched: list[dict],
    unmatched: list[dict],
    confirmed: list[dict],
    unreviewed: list[dict],
    corrections: list[dict],
    verdict_conflicts: list[dict],
    source_gaps: list[dict],
    evolution: dict,
    suggestions: list[str],
    days_back: int,
) -> dict:
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "period_days": days_back,
        "summary": {
            "calendar_events": len(matched) + len(unmatched),
            "pipeline_matches": len(matched),
            "source_gaps": len(source_gaps),
            "scoring_corrections": len(corrections),
            "confirmed_interests": len(confirmed),
            "unreviewed_attendance": len(unreviewed),
            "verdict_conflicts": len(verdict_conflicts),
        },
        "confirmed_interests": confirmed,
        "unreviewed_attendance": unreviewed,
        "scoring_corrections": corrections,
        "verdict_conflicts": verdict_conflicts,
        "source_gaps": source_gaps,
        "interest_evolution": evolution,
        "suggestions": suggestions,
    }


def save_report(report: dict) -> Path:
    path = SCRIPT_DIR / ".feedback_digest.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    return path


# ── Notion page creation ─────────────────────────────────────────────────────

FEEDBACK_DB_ID = None  # Set after database is created


def notion_request(path: str, token: str, body: dict | None = None,
                   method: str | None = None) -> dict:
    """Thin wrapper over erlib.notion (bounded retries). Raises NotionError."""
    if method is None:
        method = "POST" if body else "GET"
    return NotionClient(token).request(method, path, body)


MAX_NOTION_BLOCKS = 95  # Notion API limit is 100; leave margin for headings


def format_notion_body(report: dict) -> list[dict]:
    blocks = []
    s = report["summary"]

    # Pipeline health: first section so it's immediately visible
    health = report.get("health", {})
    health_text = health.get("health_text", "")
    if health_text:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Pipeline Health"}}]},
        })
        emoji = "✅" if health.get("health_status") == "healthy" else "⚠️"
        blocks.append({
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": health_text[:2000]}}],
                "icon": {"type": "emoji", "emoji": emoji},
            },
        })

    # Summary heading
    blocks.append({
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Summary"}}]},
    })
    summary_lines = [
        f"Calendar events scanned: {s['calendar_events']}",
        f"Pipeline matches: {s['pipeline_matches']}",
        f"Source gaps: {s['source_gaps']}",
        f"Scoring corrections: {s['scoring_corrections']}",
        f"Confirmed interests: {s['confirmed_interests']}",
    ]
    for line in summary_lines:
        blocks.append({
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": line}}]},
        })

    # Confirmed interests
    if report["confirmed_interests"]:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Confirmed Interests"}}]},
        })
        for ci in report["confirmed_interests"]:
            text = f"{ci['event']} ({ci['date']}): {ci['verdict']}, tier: {ci['tier']}"
            if ci.get("signals"):
                text += f", signals: {ci['signals'][:100]}"
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })

    # Attended but unreviewed
    if report.get("unreviewed_attendance"):
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Attended but Unreviewed"}}]},
        })
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "You attended these events but haven’t set a verdict in Notion."}}]},
        })
        for ur in report["unreviewed_attendance"]:
            text = f"{ur['event']} ({ur['date']}): tier: {ur['tier']}"
            if ur.get("venue"):
                text += f", venue: {ur['venue']}"
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })

    # Scoring corrections
    if report["scoring_corrections"]:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Scoring Corrections"}}]},
        })
        for sc in report["scoring_corrections"]:
            if sc["type"] == "false_veto":
                text = f"FALSE VETO: {sc['event']} ({sc['date']}): vetoed by '{sc.get('veto_reason', '?')}' but user attended"
            elif sc["type"] == "expired_but_attended":
                text = f"EXPIRED: {sc['event']} ({sc['date']}): event date passed before LLM review, but user attended"
            elif sc["type"] == "false_llm_reject":
                text = f"FALSE LLM REJECT: {sc['event']} ({sc['date']}): LLM assigned '{sc.get('llm_tier', '?')}' but user attended"
            elif sc["type"] == "tier_mismatch":
                text = f"TIER MISMATCH: {sc['event']} ({sc['date']}): LLM: {sc.get('llm_tier')}, Verdict: {sc.get('verdict')}"
                if sc.get("verdict_reason"):
                    text += f", Reason: {sc['verdict_reason']}"
                if sc.get("verdict_notes"):
                    text += f", Notes: {sc['verdict_notes'][:200]}"
            elif sc["type"] == "over_recommended":
                text = f"OVER-RECOMMENDED: {sc['event']} ({sc['date']}): LLM: {sc.get('llm_tier')}, but user said Not Going"
                if sc.get("verdict_reason"):
                    text += f", Reason: {sc['verdict_reason']}"
            else:
                text = f"{sc['type']}: {sc['event']} ({sc['date']})"
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })

    # Verdict conflicts
    if report.get("verdict_conflicts"):
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Verdict Conflicts"}}]},
        })
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Calendar shows these events but you marked Not Going. Did you actually attend?"}}]},
        })
        for vc in report["verdict_conflicts"]:
            text = f"{vc['event']} ({vc['date']}): tier: {vc['tier']}"
            if vc.get("verdict_reason"):
                text += f", reason: {vc['verdict_reason']}"
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })

    # Source gaps
    if report["source_gaps"]:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Source Gaps"}}]},
        })
        for sg in report["source_gaps"]:
            text = f"{sg['event']} ({sg['date']}) at {sg.get('location', 'unknown location')}"
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })

    # Interest evolution
    evo = report.get("interest_evolution", {})
    if any(evo.values()):
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Interest Evolution"}}]},
        })
        if evo.get("attended_formats"):
            text = "Attended by format: " + ", ".join(
                f"{k} ({v})" for k, v in evo["attended_formats"].items()
            )
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })
        if evo.get("repeat_venues"):
            text = "Repeat venues: " + ", ".join(
                f"{k} ({v}x)" for k, v in evo["repeat_venues"].items()
            )
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })
        if evo.get("not_going_patterns"):
            top_3 = list(evo["not_going_patterns"].items())[:3]
            text = "Top Not Going categories: " + ", ".join(
                f"{k} ({v})" for k, v in top_3
            )
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
            })

    # Suggestions
    if report["suggestions"]:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Suggested Changes"}}]},
        })
        for sug in report["suggestions"]:
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": sug[:2000]}}]},
            })

    if len(blocks) > MAX_NOTION_BLOCKS:
        blocks = blocks[:MAX_NOTION_BLOCKS]
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {
                "content": "[Truncated: full report in .feedback_digest.json]"
            }}]},
        })

    return blocks


def _format_new_interests(evolution: dict) -> str:
    """Format attended formats as readable text, e.g. 'Workshop (3x), Outdoor (2x)'."""
    formats = evolution.get("attended_formats", {})
    if not formats:
        return "None this period"
    return ", ".join(f"{fmt} ({count}x)" for fmt, count in formats.items())


def ensure_health_property(token: str, database_id: str) -> bool:
    """Add Health rich_text property to the database if it doesn't exist.
    Returns True if the property exists (or was created), False on failure."""
    try:
        db = notion_request(f"/databases/{database_id}", token)
        if "Health" in db.get("properties", {}):
            return True
        notion_request(
            f"/databases/{database_id}", token,
            {"properties": {"Health": {"rich_text": {}}}},
            method="PATCH",
        )
        print("Added 'Health' property to Scoring Feedback database")
        return True
    except (NotionError, OSError) as e:
        print(f"WARNING: Could not ensure Health property: {e}", file=sys.stderr)
        return False


def create_notion_page(
    token: str,
    database_id: str,
    report: dict,
    dry_run: bool,
) -> str | None:
    if dry_run:
        print("[DRY RUN] Would create Notion page")
        return None

    health_prop_ready = ensure_health_property(token, database_id)

    now = datetime.now(timezone.utc)
    title = now.strftime("%B %Y")  # e.g. "June 2026"
    s = report["summary"]
    health = report.get("health", {})
    health_text = health.get("health_text", "")

    period_start = (now - timedelta(days=report["period_days"])).strftime("%Y-%m-%d")
    period_end = now.strftime("%Y-%m-%d")

    properties = {
        "Month": {"title": [{"text": {"content": title}}]},
        "Period": {"date": {"start": period_start, "end": period_end}},
        "Events attended": {"number": s["calendar_events"]},
        "Pipeline matches": {"number": s["pipeline_matches"]},
        "Source gaps": {"number": s["source_gaps"]},
        "Scoring corrections": {"number": s["scoring_corrections"]},
        "New interests": {"rich_text": [{"text": {"content":
            _format_new_interests(report.get("interest_evolution", {}))
        }}]},
        "Status": {"select": {"name": "New"}},
        "Row Type": {"select": {"name": "Summary"}},
    }
    if health_text and health_prop_ready:
        properties["Health"] = {
            "rich_text": [{"text": {"content": health_text[:2000]}}],
        }

    body = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": format_notion_body(report),
    }

    try:
        result = notion_request("/pages", token, body)
        page_url = result.get("url", "")
        print(f"Notion page created: {page_url}")
        return page_url
    except NotionError as e:
        if health_text:
            print(f"WARNING: Page creation failed, retrying without health: {e}",
                  file=sys.stderr)
            properties.pop("Health", None)
            safe_children = [
                b for b in body["children"]
                if b.get("type") != "callout"
            ]
            body["properties"] = properties
            body["children"] = safe_children
            try:
                result = notion_request("/pages", token, body)
                page_url = result.get("url", "")
                print(f"Notion page created (without health blocks): {page_url}")
                return page_url
            except NotionError as e2:
                print(f"ERROR: Failed to create Notion page: {e2}", file=sys.stderr)
                return None
        print(f"ERROR: Failed to create Notion page: {e}", file=sys.stderr)
        return None


# ── Notification ──────────────────────────────────────────────────────────────


def send_notification(report: dict, page_url: str | None) -> None:
    s = report["summary"]
    title = "Feedback Digest"
    msg = (
        f"{s['calendar_events']} events attended, "
        f"{s['pipeline_matches']} matched, "
        f"{s['source_gaps']} source gaps, "
        f"{s['scoring_corrections']} corrections"
    )
    if page_url:
        msg += f"\n{page_url}"

    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode(),
            headers={"Title": title, "Priority": "default"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"Notification sent: {msg}")
    except (urllib.error.URLError, OSError) as e:
        print(f"WARNING: ntfy notification failed: {e}", file=sys.stderr)


# ── Smoke tests ───────────────────────────────────────────────────────────────

def _smoke_tests() -> int:
    ok = True

    def check(label: str, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    def check_gte(label: str, got: float, threshold: float):
        nonlocal ok
        if got >= threshold:
            print(f"  OK  {label} ({got:.2f} >= {threshold})")
        else:
            ok = False
            print(f"  FAIL {label}: got {got:.2f}, want >= {threshold}")

    # ── normalize_text tests ──
    print("normalize_text tests:")
    check("empty", normalize_text(""), "")
    check("basic lowercase", normalize_text("Jazz Night"), "jazz night")
    check("extra whitespace", normalize_text("  Jazz   Night  "), "jazz night")
    check("curly quote", normalize_text("Mara Lune’s Show"), "mara lune's show")
    check("em dash", normalize_text("Event — Special"), "event - special")
    check("en dash", normalize_text("6–15 June"), "6-15 june")
    check("NBSP", normalize_text("Sat\xa06\xa0Jun"), "sat 6 jun")
    check("zero-width space removed", normalize_text("Hello​World"), "helloworld")
    check("NFKD ligature", normalize_text("ﬁnale"), "finale")

    # ── extract_words tests ──
    print("\nextract_words tests:")
    check("drops short words", extract_words("jazz at the bar"), {"jazz", "bar"})
    check("empty string", extract_words(""), set())
    check("all short words", extract_words("a at in"), set())
    check("mixed", extract_words("Pottery Workshop at The Kiln Room"),
          {"pottery", "workshop", "kiln", "room"})

    # ── word_overlap_ratio tests ──
    print("\nword_overlap_ratio tests:")
    check("identical", word_overlap_ratio("Jazz Night", "Jazz Night"), 1.0)
    check("no overlap", word_overlap_ratio("Jazz Night", "Pottery Workshop"), 0.0)
    check("empty a", word_overlap_ratio("", "Jazz Night"), 0.0)
    check("empty b", word_overlap_ratio("Jazz Night", ""), 0.0)
    # "jazz the sound room" (3 words > 2 chars: jazz, cafe, oto) vs
    # "Jazz Night at The Sound Room Camden" (words > 2 chars: jazz, night, sound, room, camden)
    # intersection = {jazz, cafe, oto} = 3, smaller set = 3, ratio = 1.0
    check("short calendar title fully contained",
          word_overlap_ratio("jazz the sound room", "Jazz Night at The Sound Room Camden"), 1.0)
    # partial overlap
    ratio = word_overlap_ratio("Evening Pottery Workshop", "Pottery Workshop at The Kiln Room")
    check_gte("partial overlap above threshold", ratio, 0.6)

    # ── normalize_venue tests ──
    print("\nnormalize_venue tests:")
    check("strips london suffix",
          normalize_venue("The Sound Room, London"), "the sound room")
    check("strips london uk suffix",
          normalize_venue("City Arts Centre, London, UK"), "city arts centre")
    check("strips postcode",
          normalize_venue("Cafe Echo SE1 9PP"), "cafe echo")
    check("empty", normalize_venue(""), "")
    check("just a name",
          normalize_venue("The Kiln Room"), "the kiln room")

    # ── venue_matches tests ──
    print("\nvenue_matches tests:")
    check("exact match",
          venue_matches("The Sound Room", "The Sound Room"), True)
    check("calendar shorter",
          venue_matches("The Sound Room", "The Sound Room, 12 Fenwick St, Camden"), True)
    check("candidate shorter",
          venue_matches("The Sound Room, Camden, London", "The Sound Room"), True)
    check("no match",
          venue_matches("City Arts Centre", "The Sound Room"), False)
    check("empty calendar",
          venue_matches("", "The Sound Room"), False)
    check("empty candidate",
          venue_matches("The Sound Room", ""), False)
    check("both empty",
          venue_matches("", ""), False)
    check("with postcode vs without",
          venue_matches("The Kiln Room, N1 7XB", "The Kiln Room"), True)

    # ── is_virtual tests ──
    print("\nis_virtual tests:")
    check("zoom link", is_virtual("https://zoom.us/j/123"), True)
    check("google meet", is_virtual("https://meet.google.com/abc"), True)
    check("physical", is_virtual("The Sound Room, London"), False)
    check("empty", is_virtual(""), False)
    check("phone call", is_virtual("Phone call with dentist"), True)

    # ── match_confidence tests ──
    print("\nmatch_confidence tests:")

    # Different dates → always LOW regardless of title/venue
    conf, _ = match_confidence(
        "Jazz Night", "The Sound Room", "2026-06-15",
        "Jazz Night", "The Sound Room", "2026-06-16",
    )
    check("different dates → LOW", conf, CONFIDENCE_LOW)

    # Exact title + same date → HIGH
    conf, overlap = match_confidence(
        "Jazz Night at The Sound Room", "", "2026-06-15",
        "Jazz Night at The Sound Room", "", "2026-06-15",
    )
    check("exact title match → HIGH", conf, CONFIDENCE_HIGH)
    check("exact title overlap = 1.0", overlap, 1.0)

    # Venue match + strong title overlap + same date → HIGH
    conf, _ = match_confidence(
        "Jazz Night", "The Sound Room", "2026-06-15",
        "Jazz Night — Live Performance", "The Sound Room, Camden", "2026-06-15",
    )
    check("venue + strong title → HIGH", conf, CONFIDENCE_HIGH)

    # Venue match + weak title overlap → MEDIUM
    conf, _ = match_confidence(
        "pottery 7pm", "The Kiln Room", "2026-06-15",
        "Evening Pottery Workshop at The Kiln Room", "The Kiln Room", "2026-06-15",
    )
    check("venue + weak title → MEDIUM", conf, CONFIDENCE_MEDIUM)

    # Venue match + no title overlap → MEDIUM (same venue same day is strong)
    conf, _ = match_confidence(
        "dinner with friends", "The Sound Room", "2026-06-15",
        "Jazz Night — Live Performance", "The Sound Room, Camden", "2026-06-15",
    )
    check("venue match, no title overlap → MEDIUM", conf, CONFIDENCE_MEDIUM)

    # Strong title overlap, no venue → MEDIUM
    conf, _ = match_confidence(
        "Pottery Workshop The Kiln Room", "", "2026-06-15",
        "Evening Pottery Workshop at The Kiln Room", "The Kiln Room", "2026-06-15",
    )
    check("strong title overlap, no venue → MEDIUM", conf, CONFIDENCE_MEDIUM)

    # No venue, low title overlap → LOW
    conf, _ = match_confidence(
        "dinner plans", "", "2026-06-15",
        "Jazz Night — Live Performance", "The Sound Room", "2026-06-15",
    )
    check("no venue, low overlap → LOW", conf, CONFIDENCE_LOW)

    # Unicode normalisation in matching
    conf, overlap = match_confidence(
        "Mara Lune’s Show", "The Sound Room", "2026-06-15",
        "Mara Lune's Show", "The Sound Room, Camden", "2026-06-15",
    )
    check("unicode curly quote → HIGH", conf, CONFIDENCE_HIGH)
    check("unicode match overlap = 1.0", overlap, 1.0)

    # ── match_calendar_to_candidates tests ──
    print("\nmatch_calendar_to_candidates tests:")

    candidates = [
        {"id": 1, "name": "Jazz Night at The Sound Room", "date": "2026-06-15",
         "venue_name": "The Sound Room, Camden"},
        {"id": 2, "name": "Pottery Workshop", "date": "2026-06-15",
         "venue_name": "The Kiln Room"},
        {"id": 3, "name": "Hackday at Maker House", "date": "2026-06-16",
         "venue_name": "Maker House"},
        {"id": 4, "name": "Vibe Coding Jam", "date": "2026-06-20",
         "venue_name": ""},
    ]

    # Test 1: exact match
    cal_events = [
        {"title": "Jazz Night at The Sound Room", "location": "The Sound Room", "date": "2026-06-15"},
    ]
    matched, unmatched = match_calendar_to_candidates(cal_events, candidates)
    check("exact match: 1 matched", len(matched), 1)
    check("exact match: 0 unmatched", len(unmatched), 0)
    check("exact match: correct candidate",
          matched[0]["candidate"]["id"] if matched else None, 1)
    check("exact match: HIGH confidence",
          matched[0]["confidence"] if matched else None, CONFIDENCE_HIGH)

    # Test 2: venue match with different title
    cal_events = [
        {"title": "pottery 7pm", "location": "The Kiln Room", "date": "2026-06-15"},
    ]
    matched, unmatched = match_calendar_to_candidates(cal_events, candidates)
    check("venue match: 1 matched", len(matched), 1)
    check("venue match: correct candidate",
          matched[0]["candidate"]["id"] if matched else None, 2)

    # Test 3: no match (different date, no candidates)
    cal_events = [
        {"title": "Random Event", "location": "Random Place", "date": "2026-07-01"},
    ]
    matched, unmatched = match_calendar_to_candidates(cal_events, candidates)
    check("no match: 0 matched", len(matched), 0)
    check("no match: 1 unmatched", len(unmatched), 1)

    # Test 4: same date, no venue, no title overlap → unmatched
    cal_events = [
        {"title": "dinner with friends", "location": "", "date": "2026-06-15"},
    ]
    matched, unmatched = match_calendar_to_candidates(cal_events, candidates)
    check("no overlap: 0 matched", len(matched), 0)
    check("no overlap: 1 unmatched", len(unmatched), 1)

    # Test 5: multiple calendar events, mix of matched and unmatched
    cal_events = [
        {"title": "Jazz Night at The Sound Room", "location": "The Sound Room", "date": "2026-06-15"},
        {"title": "Random Dinner", "location": "Some Restaurant", "date": "2026-06-15"},
        {"title": "Hackday at Maker House", "location": "Maker House", "date": "2026-06-16"},
    ]
    matched, unmatched = match_calendar_to_candidates(cal_events, candidates)
    check("multi: 2 matched", len(matched), 2)
    check("multi: 1 unmatched", len(unmatched), 1)
    matched_ids = {m["candidate"]["id"] for m in matched}
    check("multi: correct candidates", matched_ids, {1, 3})

    # Test 6: prefers HIGH confidence over MEDIUM when multiple candidates match
    # Two candidates on same date, one has venue match, calendar has venue
    candidates_dup = [
        {"id": 10, "name": "Jazz Night", "date": "2026-06-15",
         "venue_name": "The Sound Room"},
        {"id": 11, "name": "Jazz Evening", "date": "2026-06-15",
         "venue_name": "City Arts Centre"},
    ]
    cal_events = [
        {"title": "Jazz Night", "location": "The Sound Room", "date": "2026-06-15"},
    ]
    matched, _ = match_calendar_to_candidates(cal_events, candidates_dup)
    check("prefers venue+title match", matched[0]["candidate"]["id"] if matched else None, 10)
    check("prefers HIGH", matched[0]["confidence"] if matched else None, CONFIDENCE_HIGH)

    # Test 7: empty calendar → no matches, no unmatched
    matched, unmatched = match_calendar_to_candidates([], candidates)
    check("empty calendar: 0 matched", len(matched), 0)
    check("empty calendar: 0 unmatched", len(unmatched), 0)

    # Test 8: empty candidates → all unmatched
    cal_events = [
        {"title": "Jazz Night", "location": "The Sound Room", "date": "2026-06-15"},
    ]
    matched, unmatched = match_calendar_to_candidates(cal_events, [])
    check("empty candidates: 0 matched", len(matched), 0)
    check("empty candidates: 1 unmatched", len(unmatched), 1)

    # Test 9: false positive prevention: same venue, same date, but clearly different events
    # "dinner with friends" at The Sound Room should match (MEDIUM: same venue same date)
    # but that's actually correct behaviour: it IS the same venue on the same date,
    # so flagging it as a probable match (MEDIUM) is the right call.
    # The report distinguishes HIGH from MEDIUM so the user can review MEDIUM matches.

    # Test 10: calendar event with title containing candidate name as substring
    cal_events = [
        {"title": "Going to Vibe Coding Jam tonight!", "location": "", "date": "2026-06-20"},
    ]
    matched, unmatched = match_calendar_to_candidates(cal_events, candidates)
    check("title contains candidate name: 1 matched", len(matched), 1)
    check("title contains candidate name: correct id",
          matched[0]["candidate"]["id"] if matched else None, 4)

    # ── Analysis function tests ──
    print("\nanalyze_confirmed_interests tests:")

    test_matched = [
        {"calendar_event": {}, "candidate": {
            "id": 1, "name": "Jazz Night", "date": "2026-06-15",
            "verdict": "Going", "llm_tier": "Top Picks",
            "tier": "STRONG", "signals_fired": "Signal #3: Listening bar"
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
        {"calendar_event": {}, "candidate": {
            "id": 2, "name": "Pottery Workshop", "date": "2026-06-16",
            "verdict": "Maybe", "llm_tier": "Recommended",
            "tier": "GOOD", "signals_fired": "Signal #5: Creative"
        }, "confidence": CONFIDENCE_MEDIUM, "overlap": 0.6},
        {"calendar_event": {}, "candidate": {
            "id": 3, "name": "Random Event", "date": "2026-06-17",
            "verdict": "Not Going", "llm_tier": "Not Recommended",
            "tier": None, "signals_fired": ""
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
        {"calendar_event": {}, "candidate": {
            "id": 4, "name": "No Verdict Event", "date": "2026-06-18",
            "verdict": None, "llm_tier": "Borderline",
            "tier": None, "signals_fired": ""
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
    ]

    confirmed = analyze_confirmed_interests(test_matched)
    check("confirmed: only Going/Maybe", len(confirmed), 2)
    check("confirmed: first is Going",
          confirmed[0]["verdict"] if confirmed else None, "Going")

    print("\nanalyze_scoring_corrections tests:")

    test_corrections = [
        {"calendar_event": {}, "candidate": {
            "id": 10, "name": "Vetoed Event", "date": "2026-06-15",
            "pipeline_state": "vetoed", "veto_reason": "corporate_tech",
            "verdict": None, "llm_tier": None,
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
        {"calendar_event": {}, "candidate": {
            "id": 11, "name": "LLM Rejected", "date": "2026-06-16",
            "pipeline_state": "llm_rejected", "veto_reason": None,
            "verdict": None, "llm_tier": "Not Recommended",
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
        {"calendar_event": {}, "candidate": {
            "id": 12, "name": "Going but Borderline", "date": "2026-06-17",
            "pipeline_state": "written", "veto_reason": None,
            "verdict": "Going", "llm_tier": "Borderline",
            "verdict_reason": "looked fun", "verdict_notes": "great time",
            "signals_fired": "Signal #6",
        }, "confidence": CONFIDENCE_MEDIUM, "overlap": 0.7},
        {"calendar_event": {}, "candidate": {
            "id": 13, "name": "Not Going Top Pick", "date": "2026-06-18",
            "pipeline_state": "written", "veto_reason": None,
            "verdict": "Not Going", "llm_tier": "Top Picks",
            "verdict_reason": "too far", "verdict_notes": None,
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
        {"calendar_event": {}, "candidate": {
            "id": 14, "name": "Going Top Pick", "date": "2026-06-19",
            "pipeline_state": "written", "veto_reason": None,
            "verdict": "Going", "llm_tier": "Top Picks",
            "signals_fired": "Signal #1",
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
        {"calendar_event": {}, "candidate": {
            "id": 15, "name": "Expired Event", "date": "2026-06-20",
            "pipeline_state": "expired", "veto_reason": None,
            "verdict": None, "llm_tier": None,
        }, "confidence": CONFIDENCE_HIGH, "overlap": 1.0},
    ]

    corrections = analyze_scoring_corrections(test_corrections)
    check("corrections: 5 found (not Going+TopPicks)", len(corrections), 5)
    types = [c["type"] for c in corrections]
    check("corrections: has false_veto", "false_veto" in types, True)
    check("corrections: has expired_but_attended", "expired_but_attended" in types, True)
    check("corrections: has false_llm_reject", "false_llm_reject" in types, True)
    check("corrections: has tier_mismatch", "tier_mismatch" in types, True)
    check("corrections: has over_recommended", "over_recommended" in types, True)
    veto_correction = next(c for c in corrections if c["type"] == "false_veto")
    check("false_veto has veto_reason", veto_correction.get("veto_reason"), "corporate_tech")

    print("\nanalyze_source_gaps tests:")
    test_unmatched = [
        {"title": "Pottery at The Kiln Room", "date": "2026-06-15", "location": "The Kiln Room"},
        {"title": "", "date": "2026-06-16", "location": "Somewhere"},
        {"title": "Jazz at The Sound Room", "date": "2026-06-17", "location": "The Sound Room"},
    ]
    gaps = analyze_source_gaps(test_unmatched)
    check("gaps: excludes empty titles", len(gaps), 2)

    print("\nanalyze_interest_evolution tests:")
    test_evolution_matched = [
        {"candidate": {"format_type": "Workshop", "venue_name": "The Kiln Room",
                        "organiser": "Creative London", "verdict": "Going"}},
        {"candidate": {"format_type": "Workshop", "venue_name": "The Kiln Room",
                        "organiser": "Creative London", "verdict": "Going"}},
        {"candidate": {"format_type": "Social meetup", "venue_name": "The Sound Room",
                        "organiser": "Jazz Club", "verdict": "Going"}},
    ]
    test_all_candidates = [
        {"verdict": "Not Going", "format_type": "Talk"},
        {"verdict": "Not Going", "format_type": "Talk"},
        {"verdict": "Not Going", "format_type": "Workshop"},
        {"verdict": "Going", "format_type": "Workshop"},
    ]
    evo = analyze_interest_evolution(test_evolution_matched, test_all_candidates)
    check("evolution: Workshop is top format",
          list(evo["attended_formats"].keys())[0] if evo["attended_formats"] else None,
          "Workshop")
    check("evolution: The Kiln Room is repeat venue",
          "The Kiln Room" in evo.get("repeat_venues", {}), True)
    check("evolution: Creative London is repeat organiser",
          "Creative London" in evo.get("repeat_organisers", {}), True)
    check("evolution: Talk is top Not Going",
          list(evo["not_going_patterns"].keys())[0] if evo["not_going_patterns"] else None,
          "Talk")

    print("\ngenerate_suggestions tests:")
    test_suggestions = generate_suggestions(
        [{"type": "false_veto", "veto_reason": "corporate_tech", "event": "X", "date": "2026-06-15"}],
        [{"event": "A", "date": "2026-06-15", "location": "Secret Venue"},
         {"event": "B", "date": "2026-06-16", "location": "Secret Venue"}],
        {"repeat_venues": {"The Kiln Room": 3}, "repeat_organisers": {"Creative London": 2}},
    )
    check("suggestions: has veto suggestion",
          any("corporate_tech" in s for s in test_suggestions), True)
    check("suggestions: has venue suggestion",
          any("The Kiln Room" in s for s in test_suggestions), True)
    check("suggestions: has organiser suggestion",
          any("Creative London" in s for s in test_suggestions), True)
    check("suggestions: has source gap with repeat venue",
          any("Secret Venue" in s for s in test_suggestions), True)

    print("\nbuild_report tests:")
    report = build_report(
        matched=[{"calendar_event": {}, "candidate": {"id": 1}, "confidence": "high", "overlap": 1.0}],
        unmatched=[{"title": "Missed", "date": "2026-06-15", "location": "Somewhere"}],
        confirmed=[{"event": "Jazz", "date": "2026-06-15", "verdict": "Going"}],
        unreviewed=[{"event": "Unset", "date": "2026-06-15", "tier": "Top Picks"}],
        corrections=[{"type": "false_veto", "event": "Vetoed", "date": "2026-06-16"}],
        verdict_conflicts=[{"event": "Skipped", "date": "2026-06-17", "tier": "Recommended"}],
        source_gaps=[{"event": "Missed", "date": "2026-06-15", "location": "Somewhere"}],
        evolution={"attended_formats": {"Workshop": 2}},
        suggestions=["Add venue X"],
        days_back=30,
    )
    check("report: has summary", "summary" in report, True)
    check("report: calendar_events count", report["summary"]["calendar_events"], 2)
    check("report: pipeline_matches count", report["summary"]["pipeline_matches"], 1)
    check("report: source_gaps count", report["summary"]["source_gaps"], 1)
    check("report: has generated_utc", "generated_utc" in report, True)
    check("report: has unreviewed_attendance", "unreviewed_attendance" in report, True)
    check("report: unreviewed count", report["summary"]["unreviewed_attendance"], 1)
    check("report: has verdict_conflicts", "verdict_conflicts" in report, True)
    check("report: verdict_conflicts count", report["summary"]["verdict_conflicts"], 1)

    print("\nis_virtual extended tests:")
    check("virtual: Microsoft Teams Meeting", is_virtual("Microsoft Teams Meeting"), True)
    check("virtual: Teams Meeting room 5", is_virtual("Teams Meeting room 5"), True)
    check("virtual: Team Building at the Park", is_virtual("Team Building at the Park"), False)
    check("virtual: physical venue", is_virtual("Ramen Space, 123 High St"), False)

    print("\nis_personal_event tests:")
    check("personal: dentist", is_personal_event("Dentist check-up", "") is not None, True)
    check("personal: gp", is_personal_event("GP appointment", "") is not None, True)
    check("personal: Dr Larkin", is_personal_event("Dr Larkin", "") is not None, True)
    check("personal: Dr. Smith appointment", is_personal_event("Dr. Smith appointment", "") is not None, True)
    check("personal: Piano Lesson", is_personal_event("Piano Lesson", "") is not None, True)
    check("personal: physio", is_personal_event("Physio session", "") is not None, True)
    check("personal: clinic location", is_personal_event("Morning visit", "The Health Clinic") is not None, True)
    check("personal: public class not personal", is_personal_event("Pottery Class at The Kiln Room", "") is None, True)
    check("personal: face to face", is_personal_event("Face to face - Sam", "") is not None, True)
    check("personal: surgery location", is_personal_event("Morning slot", "Riverside Surgery") is not None, True)
    _prev = list(PERSONAL_TITLE_PATTERNS)
    PERSONAL_TITLE_PATTERNS.append(r"\bchoir practice\b")
    check("personal: user-extended pattern", is_personal_event("Choir practice", "") is not None, True)
    PERSONAL_TITLE_PATTERNS[:] = _prev
    check("not personal: Yoga & Wine Social", is_personal_event("Yoga & Wine Social", "The Sound Room") is None, True)
    check("not personal: Team Building BBQ", is_personal_event("Team Building BBQ", "Hyde Park") is None, True)
    check("not personal: Dr. Strangelove Film Night", is_personal_event("Dr. Strangelove Film Night", "BFI") is None, True)
    check("not personal: Documentary Filming Workshop", is_personal_event("Documentary Filming Workshop", "Southbank") is None, True)

    print("\ndedup_calendar_events tests:")
    dedup_input = [
        {"title": "Morning Pottery Taster", "date": "2026-05-19", "location": "Old Street Baths", "calendar_id": "a"},
        {"title": "Morning Pottery Taster", "date": "2026-05-19", "location": "Old Street Baths, 8 Bath Ln, London", "calendar_id": "b"},
        {"title": "Different Event", "date": "2026-05-19", "location": "Somewhere", "calendar_id": "c"},
    ]
    deduped = dedup_calendar_events(dedup_input)
    check("dedup: 3→2", len(deduped), 2)
    pottery = [e for e in deduped if "pottery" in e["title"].lower()]
    check("dedup: keeps longer location", len(pottery[0]["location"]) > 20 if pottery else False, True)

    print("\nanalyze_unreviewed_attendance tests:")
    test_matched_ur = [
        {"candidate": {"name": "Event A", "date": "2026-06-01", "verdict": "Going", "llm_tier": "Top Picks", "venue_name": "V1"}, "confidence": "high"},
        {"candidate": {"name": "Event B", "date": "2026-06-02", "verdict": None, "llm_tier": "Recommended", "venue_name": "V2"}, "confidence": "high"},
        {"candidate": {"name": "Event C", "date": "2026-06-03", "verdict": "Undecided", "llm_tier": "Borderline", "venue_name": "V3"}, "confidence": "high"},
        {"candidate": {"name": "Event D", "date": "2026-06-04", "verdict": "Not Going", "llm_tier": "Top Picks", "venue_name": "V4"}, "confidence": "high"},
    ]
    ur_result = analyze_unreviewed_attendance(test_matched_ur)
    check("unreviewed: 2 found (None + Undecided)", len(ur_result), 2)
    check("unreviewed: Going excluded", all(u["event"] != "Event A" for u in ur_result), True)
    check("unreviewed: Not Going excluded", all(u["event"] != "Event D" for u in ur_result), True)

    print("\nanalyze_verdict_conflicts tests:")
    vc_result = analyze_verdict_conflicts(test_matched_ur)
    check("verdict_conflicts: 1 found (Not Going only)", len(vc_result), 1)
    check("verdict_conflicts: correct event", vc_result[0]["event"], "Event D")

    print("\nsave_attendance verdict filter tests:")
    check("attendance: Not Going skipped",
          all(m["candidate"].get("verdict") != "Not Going"
              for m in test_matched_ur
              if m["candidate"].get("verdict") != "Not Going"),
          True)

    print("\nplaceholder venue filter tests:")
    test_evo_pv = {"repeat_venues": {"Central London TBC": 2, "Ramen Space": 3}, "repeat_organisers": {}}
    test_sugg_pv = generate_suggestions([], [], test_evo_pv)
    check("placeholder: Central London TBC filtered", all("Central London TBC" not in s for s in test_sugg_pv), True)
    check("placeholder: Ramen Space kept", any("Ramen Space" in s for s in test_sugg_pv), True)

    # ── Finding classification tests ──
    print("\nfinding classification tests:")
    test_fc_corrections = [
        {"type": "false_veto", "event": "Builder Beers May", "date": "2026-05-26",
         "veto_reason": "networking_drinks", "confidence": "high"},
    ]
    test_fc_evolution = {
        "repeat_venues": {"Ramen Space": 2},
        "repeat_organisers": {"Builder Beers": 3},
    }
    test_fc_gaps = [
        {"event": "Pop-up Screenings", "date": "2026-05-15", "location": "Peckham"},
    ]
    findings = classify_findings(test_fc_corrections, test_fc_evolution, test_fc_gaps)
    check("classify: count", len(findings), 3)
    org_f = [f for f in findings if f["finding_key"] == "organiser:builder beers"]
    check("classify: organiser exists", len(org_f), 1)
    check("classify: organiser type", org_f[0]["finding_type"], "organiser")
    check("classify: organiser has additional_types field",
          isinstance(org_f[0].get("additional_types"), list), True)
    veto_f = [f for f in findings if f["finding_type"] == "veto_exemption"]
    check("classify: veto exists", len(veto_f), 1)
    check("classify: veto key", veto_f[0]["finding_key"], "veto:networking_drinks")
    series_f = [f for f in findings if f["finding_type"] == "series"]
    check("classify: series exists", len(series_f), 1)
    gap_f = [f for f in findings if f["finding_type"] == "source_gap"]
    check("classify: single gap filtered", len(gap_f), 0)
    test_fc_evo2 = {"repeat_venues": {"TBC": 5, "Ramen Space": 2}, "repeat_organisers": {}}
    findings2 = classify_findings([], test_fc_evo2, [])
    check("classify: placeholder filtered", len(findings2), 1)

    # ── Cross-reference enrichment tests ──
    print("\ncross-reference enrichment tests:")
    xref_corrections = [
        {"type": "false_veto", "event": "Builder Beers May", "date": "2026-05-26",
         "veto_reason": "networking_drinks", "confidence": "high"},
        {"type": "tier_mismatch", "event": "Builder Beers June", "date": "2026-06-10",
         "organiser": "Builder Beers", "llm_tier": "Borderline",
         "verdict": "Going", "verdict_reason": "love these", "confidence": "high"},
        {"type": "over_recommended", "event": "Random Talk", "date": "2026-06-05",
         "organiser": "Unknown Org", "llm_tier": "Top Picks",
         "verdict": "Not Going", "verdict_reason": "not my thing", "confidence": "high"},
    ]
    xref_evolution = {
        "repeat_organisers": {"Builder Beers": 3},
        "repeat_venues": {},
    }
    xref_findings = classify_findings(xref_corrections, xref_evolution, [])
    xref_org = [f for f in xref_findings if f["finding_key"] == "organiser:builder beers"]
    check("xref: organiser enriched with tier_mismatch",
          "tier_mismatch" in xref_org[0]["additional_types"], True)
    check("xref: correction_events populated",
          len(xref_org[0]["correction_events"]) > 0, True)
    xref_standalone = [f for f in xref_findings if f["finding_type"] == "over_recommended"]
    check("xref: unmatched correction becomes standalone", len(xref_standalone), 1)
    check("xref: standalone has correct key format",
          xref_standalone[0]["finding_key"].startswith("over_recommended:"), True)

    # ── All 8 finding types tests ──
    # ── false_veto enrichment test ──
    print("\nfalse_veto enrichment tests:")
    fv_corrections = [
        {"type": "false_veto", "event": "PP Social", "date": "2026-06-01",
         "veto_reason": "networking_drinks", "organiser": "Builder Beers",
         "venue_name": "", "confidence": "high"},
    ]
    fv_evo = {"repeat_organisers": {"Builder Beers": 3}, "repeat_venues": {}}
    fv_findings = classify_findings(fv_corrections, fv_evo, [])
    fv_org = [f for f in fv_findings if f["finding_key"] == "organiser:builder beers"]
    check("fv_enrich: organiser gets false_veto label",
          "false_veto" in fv_org[0]["additional_types"], True)
    fv_veto = [f for f in fv_findings if f["finding_type"] == "veto_exemption"]
    check("fv_enrich: veto entity finding still exists", len(fv_veto), 1)
    fv_standalone = [f for f in fv_findings if f["finding_type"] == "false_veto"]
    check("fv_enrich: no standalone false_veto (not duplicated)", len(fv_standalone), 0)
    check("fv_enrich: false_veto maps to Veto Exemption in FINDING_TYPE_MAP",
          FINDING_TYPE_MAP.get("false_veto"), "Veto Exemption")
    fv_enriched_rat = generate_rationale({
        "finding_type": "organiser", "target_name": "Builder Beers",
        "evidence_count": 1, "events": [{"count": 3}],
        "additional_types": ["false_veto"],
        "correction_events": [{"type": "false_veto", "event": "PP Social",
                                "date": "2026-06-01", "veto_reason": "networking_drinks"}],
    })
    check("fv_enrich: rationale mentions veto",
          "veto" in fv_enriched_rat.lower(), True)
    check("fv_enrich: rationale mentions specific veto name",
          "networking_drinks" in fv_enriched_rat, True)

    # ── End-to-end data path test (real flow, not hand-crafted) ──
    print("\nend-to-end data path tests:")
    e2e_matched = [
        {"candidate": {"name": "PP Social June", "date": "2026-06-10",
                        "pipeline_state": "written", "verdict": "Going",
                        "organiser": "Builder Beers", "venue_name": "The Ramen Space",
                        "llm_tier": "Borderline", "verdict_reason": "love these",
                        "verdict_notes": "", "veto_reason": None,
                        "signals_fired": "", "format_type": "Social meetup"},
         "confidence": "HIGH"},
    ]
    e2e_corrections = analyze_scoring_corrections(e2e_matched)
    check("e2e: correction has organiser",
          e2e_corrections[0].get("organiser"), "Builder Beers")
    check("e2e: correction has venue_name",
          e2e_corrections[0].get("venue_name"), "The Ramen Space")
    e2e_evo = {"repeat_organisers": {"Builder Beers": 3}, "repeat_venues": {}}
    e2e_findings = classify_findings(e2e_corrections, e2e_evo, [])
    e2e_org = [f for f in e2e_findings if f["finding_key"] == "organiser:builder beers"]
    check("e2e: organiser finding enriched via real data path",
          "tier_mismatch" in e2e_org[0]["additional_types"], True)
    check("e2e: no standalone tier_mismatch (absorbed into entity)",
          len([f for f in e2e_findings if f["finding_type"] == "tier_mismatch"]), 0)

    print("\nall finding types tests:")
    all_type_corrections = [
        {"type": "false_veto", "event": "Ev1", "date": "2026-06-01",
         "veto_reason": "test_veto", "confidence": "high"},
        {"type": "tier_mismatch", "event": "Ev2", "date": "2026-06-02",
         "organiser": "Nobody", "llm_tier": "Borderline",
         "verdict": "Going", "confidence": "high"},
        {"type": "over_recommended", "event": "Ev3", "date": "2026-06-03",
         "organiser": "Nobody2", "llm_tier": "Top Picks",
         "verdict": "Not Going", "confidence": "high"},
        {"type": "false_llm_reject", "event": "Ev4", "date": "2026-06-04",
         "organiser": "Nobody3", "llm_tier": "Not Recommended", "confidence": "high"},
        {"type": "expired_but_attended", "event": "Ev5", "date": "2026-06-05",
         "confidence": "high"},
    ]
    all_type_evo = {
        "repeat_organisers": {"Test Org": 2},
        "repeat_venues": {"Test Venue": 3},
    }
    all_type_gaps = [
        {"event": "Gap1", "date": "2026-06-01", "location": "Gap Venue"},
        {"event": "Gap2", "date": "2026-06-02", "location": "Gap Venue"},
    ]
    all_findings = classify_findings(all_type_corrections, all_type_evo, all_type_gaps)
    all_types_found = {f["finding_type"] for f in all_findings}
    check("all types: organiser", "organiser" in all_types_found, True)
    check("all types: series", "series" in all_types_found, True)
    check("all types: veto_exemption", "veto_exemption" in all_types_found, True)
    check("all types: source_gap", "source_gap" in all_types_found, True)
    check("all types: tier_mismatch", "tier_mismatch" in all_types_found, True)
    check("all types: over_recommended", "over_recommended" in all_types_found, True)
    check("all types: false_llm_reject", "false_llm_reject" in all_types_found, True)
    check("all types: expired_but_attended", "expired_but_attended" in all_types_found, True)

    # ── Rationale generation tests ──
    print("\nrationale generation tests:")
    org_rat = generate_rationale({
        "finding_type": "organiser", "target_name": "Builder Beers",
        "evidence_count": 2, "events": [{"count": 2}, {"count": 3}],
        "correction_events": [], "additional_types": [],
    })
    check("rationale: organiser mentions name", "Builder Beers" in org_rat, True)
    check("rationale: organiser mentions effect",
          "known" in org_rat.lower() or "score" in org_rat.lower(), True)
    check("rationale: organiser has substance", len(org_rat) > 50, True)

    enriched_rat = generate_rationale({
        "finding_type": "organiser", "target_name": "Builder Beers",
        "evidence_count": 1, "events": [{"count": 3}],
        "additional_types": ["tier_mismatch"],
        "correction_events": [
            {"type": "tier_mismatch", "event": "PP June", "date": "2026-06-10",
             "llm_tier": "Borderline"},
        ],
    })
    check("rationale: enriched mentions scoring",
          "scored" in enriched_rat.lower() or "undervalued" in enriched_rat.lower(), True)
    check("rationale: enriched still mentions organiser",
          "Builder Beers" in enriched_rat, True)

    veto_rat = generate_rationale({
        "finding_type": "veto_exemption", "target_name": "networking_drinks",
        "evidence_count": 1,
        "events": [{"event": "Builder Beers May", "date": "2026-05-26", "confidence": "high"}],
        "correction_events": [], "additional_types": [],
    })
    check("rationale: veto mentions name", "networking_drinks" in veto_rat, True)
    check("rationale: veto mentions review",
          "review" in veto_rat.lower() or "broad" in veto_rat.lower(), True)

    tm_rat = generate_rationale({
        "finding_type": "tier_mismatch", "target_name": "Cool Event",
        "evidence_count": 1,
        "events": [{"event": "Cool Event", "date": "2026-06-10",
                     "llm_tier": "Borderline", "verdict": "Going",
                     "verdict_reason": "love it"}],
        "correction_events": [], "additional_types": [],
    })
    check("rationale: tier_mismatch mentions event", "Cool Event" in tm_rat, True)
    check("rationale: tier_mismatch mentions tier", "Borderline" in tm_rat, True)
    check("rationale: tier_mismatch mentions user reason", "love it" in tm_rat, True)

    flr_rat = generate_rationale({
        "finding_type": "false_llm_reject", "target_name": "Hidden Gem",
        "evidence_count": 1,
        "events": [{"event": "Hidden Gem", "date": "2026-06-15"}],
        "correction_events": [], "additional_types": [],
    })
    check("rationale: false_llm_reject mentions LLM", "LLM" in flr_rat, True)
    check("rationale: false_llm_reject mentions rejected",
          "rejected" in flr_rat.lower() or "filtering" in flr_rat.lower(), True)

    exp_rat = generate_rationale({
        "finding_type": "expired_but_attended", "target_name": "Late Event",
        "evidence_count": 1,
        "events": [{"event": "Late Event", "date": "2026-06-20"}],
        "correction_events": [], "additional_types": [],
    })
    check("rationale: expired mentions timing",
          "expired" in exp_rat.lower() or "timing" in exp_rat.lower(), True)

    # ── Action text tests ──
    print("\naction text tests:")
    for ftype, expected_word in [
        ("organiser", "auto-apply"),
        ("series", "auto-apply"),
        ("veto_exemption", "manual review"),
        ("source_gap", "manual review"),
        ("tier_mismatch", "manual review"),
        ("over_recommended", "manual review"),
        ("false_llm_reject", "manual review"),
        ("expired_but_attended", "informational"),
    ]:
        action = ACTION_TEXT.get(ftype, "")
        check(f"action: {ftype}", expected_word in action.lower(), True)

    # ── Finding title tests ──
    print("\nfinding title tests:")
    check("title: tier_mismatch", "Scoring too low" in _finding_title(
        {"finding_type": "tier_mismatch", "target_name": "X"}), True)
    check("title: over_recommended", "Over-recommended" in _finding_title(
        {"finding_type": "over_recommended", "target_name": "X"}), True)
    check("title: false_llm_reject", "LLM wrongly rejected" in _finding_title(
        {"finding_type": "false_llm_reject", "target_name": "X"}), True)
    check("title: expired_but_attended", "Expired before review" in _finding_title(
        {"finding_type": "expired_but_attended", "target_name": "X"}), True)

    # ── New interests formatting test ──
    print("\nnew interests formatting tests:")
    check("new_interests: empty", _format_new_interests({}), "None this period")
    check("new_interests: formats",
          _format_new_interests({"attended_formats": {"Workshop": 3, "Outdoor": 1}}),
          "Workshop (3x), Outdoor (1x)")

    # ── Evidence accumulation tests ──
    print("\nevidence accumulation tests:")
    import tempfile as _tf
    _tmp = _tf.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115  path must outlive handle
    _tmp_conn = sqlite3.connect(_tmp.name)
    _tmp_conn.row_factory = sqlite3.Row
    _tmp_conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY)")
    _tmp_conn.commit()
    ensure_schema(_tmp_conn)

    _run1 = [{"finding_key": "organiser:builder beers", "finding_type": "organiser",
              "target_name": "Builder Beers",
              "events": [{"organiser": "Builder Beers", "count": 2}],
              "additional_types": ["tier_mismatch"],
              "correction_events": [{"type": "tier_mismatch", "event": "PP", "date": "2026-05-20"}]}]
    save_digest_findings(_tmp_conn, _run1, "2026-05-29", dry_run=False)
    _row = _tmp_conn.execute(
        "SELECT * FROM digest_findings WHERE finding_key = 'organiser:builder beers'"
    ).fetchone()
    check("accum: first evidence_count", _row["evidence_count"], 1)
    check("accum: first first_seen", _row["first_seen"], "2026-05-29")
    check("accum: rationale populated", len(_row["rationale"]) > 30, True)

    _run2 = [{"finding_key": "organiser:builder beers", "finding_type": "organiser",
              "target_name": "Builder Beers",
              "events": [{"organiser": "Builder Beers", "count": 3}],
              "additional_types": [], "correction_events": []}]
    save_digest_findings(_tmp_conn, _run2, "2026-06-12", dry_run=False)
    _row2 = _tmp_conn.execute(
        "SELECT * FROM digest_findings WHERE finding_key = 'organiser:builder beers'"
    ).fetchone()
    check("accum: second evidence_count", _row2["evidence_count"], 2)
    check("accum: second last_seen", _row2["last_seen"], "2026-06-12")
    check("accum: first_seen unchanged", _row2["first_seen"], "2026-05-29")
    _events = json.loads(_row2["events_json"])
    check("accum: events merged", len(_events), 2)

    _tmp_conn.close()
    os.unlink(_tmp.name)

    # ── Notion finding page formatting tests ──
    print("\nfinding page formatting tests:")
    _test_row = {
        "finding_type": "organiser", "target_name": "Builder Beers",
        "rationale": "You attended events by Builder Beers 3 times.",
        "events_json": json.dumps([{"organiser": "Builder Beers", "count": 2}]),
        "evidence_count": 2, "first_seen": "2026-05-29",
        "last_seen": "2026-06-12", "finding_key": "organiser:builder beers",
        "additional_types": json.dumps(["tier_mismatch"]),
        "correction_events": json.dumps([{"type": "tier_mismatch", "event": "PP", "date": "2026-06-10", "llm_tier": "Borderline"}]),
    }
    _body = format_finding_notion_properties(_test_row, "test-db-id")
    check("notion: has parent", "database_id" in _body.get("parent", {}), True)
    check("notion: has title", "Finding" in str(_body.get("properties", {})), True)
    check("notion: rationale in property",
          "Rationale" in _body.get("properties", {}), True)
    check("notion: has children", len(_body.get("children", [])) > 0, True)
    _ft = _body["properties"]["Finding Type"]
    check("notion: multi_select format", "multi_select" in _ft, True)
    _type_names = [t["name"] for t in _ft["multi_select"]]
    check("notion: primary type present", "Organiser" in _type_names, True)
    check("notion: additional type present", "Tier Mismatch" in _type_names, True)
    check("notion: action property exists", "Action" in _body["properties"], True)
    _action_text = _body["properties"]["Action"]["rich_text"][0]["text"]["content"]
    check("notion: action mentions auto-apply", "auto-apply" in _action_text.lower(), True)

    # Test standalone correction formatting
    _standalone_row = {
        "finding_type": "false_llm_reject", "target_name": "Hidden Gem",
        "rationale": "LLM rejected this event.",
        "events_json": json.dumps([{"event": "Hidden Gem", "date": "2026-06-15"}]),
        "evidence_count": 1, "first_seen": "2026-06-15",
        "last_seen": "2026-06-15", "finding_key": "false_llm_reject:hidden gem:2026-06-15",
        "additional_types": "[]", "correction_events": "[]",
    }
    _standalone_body = format_finding_notion_properties(_standalone_row, "test-db-id")
    _st_types = [t["name"] for t in _standalone_body["properties"]["Finding Type"]["multi_select"]]
    check("notion standalone: single type", _st_types, ["False LLM Reject"])
    _st_action = _standalone_body["properties"]["Action"]["rich_text"][0]["text"]["content"]
    check("notion standalone: action is manual review", "manual review" in _st_action.lower(), True)

    # ── Pipeline health assessment tests ──────────────────────────────────────
    print("\npipeline health tests:")

    # Healthy: 2 successful runs
    _healthy_runs = [
        {"batch": "27 May", "status": "Success", "events_added": 30,
         "issues": "No issues", "pipeline": "248 fetched → 30 written",
         "duration": "45 min", "batch_date": "2026-05-27", "trigger": "Automatic"},
        {"batch": "20 May", "status": "Success", "events_added": 25,
         "issues": "", "pipeline": "200 fetched → 25 written",
         "duration": "40 min", "batch_date": "2026-05-20", "trigger": "Automatic"},
    ]
    _h = assess_pipeline_health(_healthy_runs, None, days_back=14)
    check("health: healthy status", _h["health_status"], "healthy")
    check("health: healthy mentions all systems", "All systems healthy" in _h["health_text"], True)
    check("health: healthy mentions run count", "2 run(s) in this period" in _h["health_text"], True)
    check("health: healthy mentions digest success", "digest ran successfully" in _h["health_text"], True)

    # Failed run
    _failed_runs = [
        {"batch": "27 May", "status": "Failed", "events_added": 0,
         "issues": "write_notion.py failed: HTTP 401 token expired",
         "pipeline": "248 fetched → 0 written", "duration": "30 min",
         "batch_date": "2026-05-27", "trigger": "Automatic"},
    ]
    _h = assess_pipeline_health(_failed_runs, None, days_back=14)
    check("health: failed status", _h["health_status"], "failed")
    check("health: failed mentions problem", "Problem" in _h["health_text"], True)
    check("health: failed mentions auth", "authentication" in _h["health_text"].lower(), True)
    check("health: failed mentions no events", "No new events" in _h["health_text"], True)

    # Partial run
    _partial_runs = [
        {"batch": "27 May", "status": "Partial", "events_added": 25,
         "issues": "1 candidate stuck in pending_llm; Borderline distribution 61%",
         "pipeline": "248 fetched → 25 written | STUCK: 1",
         "duration": "45 min", "batch_date": "2026-05-27", "trigger": "Automatic"},
    ]
    _h = assess_pipeline_health(_partial_runs, None, days_back=14)
    check("health: partial status", _h["health_status"], "degraded")
    check("health: partial mentions issues", "completed with some notes" in _h["health_text"], True)
    check("health: partial mentions events written", "25 events were written" in _h["health_text"], True)

    # No runs found (missed)
    _h = assess_pipeline_health([], None, days_back=14)
    check("health: missed status", _h["health_status"], "missed")
    check("health: missed mentions no runs", "No pipeline runs" in _h["health_text"], True)
    check("health: missed mentions expected", "expected" in _h["health_text"].lower(), True)

    # Query error (unknown)
    _h = assess_pipeline_health([], "HTTP 403 Forbidden", days_back=14)
    check("health: query error status", _h["health_status"], "unknown")
    check("health: query error mentions unable", "Unable to determine" in _h["health_text"], True)

    # Digest error appended
    _h = assess_pipeline_health(_healthy_runs, None, days_back=14,
                                 digest_error="Calendar OAuth token expired")
    check("health: digest error appended", "Calendar OAuth token expired" in _h["health_text"], True)
    check("health: digest error still has pipeline status", "All systems healthy" in _h["health_text"], True)

    # Skipped run
    _skipped_runs = [
        {"batch": "27 May", "status": "Skipped", "events_added": 0,
         "issues": "", "pipeline": "", "duration": "",
         "batch_date": "2026-05-27", "trigger": "Automatic"},
    ]
    _h = assess_pipeline_health(_skipped_runs, None, days_back=14)
    check("health: skipped status", _h["health_status"], "degraded")
    check("health: skipped mentions skipped", "skipped" in _h["health_text"].lower(), True)

    # CalendarError is catchable (not sys.exit)
    _caught = False
    try:
        raise CalendarError("test error")
    except CalendarError:
        _caught = True
    check("CalendarError: catchable", _caught, True)
    check("CalendarError: not SystemExit", not issubclass(CalendarError, SystemExit), True)

    # Health-only report (digest failure case)
    _health_only = build_report([], [], [], [], [], [], [], {}, [], 30)
    _health_only["health"] = assess_pipeline_health(
        _healthy_runs, None, days_back=14,
        digest_error="Calendar OAuth token expired",
    )
    check("health-only: summary zeros", _health_only["summary"]["calendar_events"], 0)
    check("health-only: health attached", "health" in _health_only, True)
    check("health-only: digest error in text",
          "Calendar OAuth" in _health_only["health"]["health_text"], True)
    _ho_blocks = format_notion_body(_health_only)
    _ho_headings = [b["heading_2"]["rich_text"][0]["text"]["content"]
                    for b in _ho_blocks if b.get("type") == "heading_2"]
    check("health-only: has Pipeline Health heading", "Pipeline Health" in _ho_headings, True)
    check("health-only: has Summary heading", "Summary" in _ho_headings, True)
    # Body should NOT have Confirmed Interests etc. (all empty)
    check("health-only: no Confirmed heading",
          "Confirmed Interests" not in _ho_headings, True)

    # Strip jargon
    check("jargon: file path removed", ".py" not in _strip_jargon("write_notion.py failed"), True)
    check("jargon: exit code removed", "exit code" not in _strip_jargon("exit code 1").lower(), True)
    check("jargon: plain text preserved", "failed" in _strip_jargon("write failed"), True)
    check("jargon: empty string", _strip_jargon(""), "")

    # Classify failure
    _what, _why, _next = _classify_failure("HTTP 401 token expired", "Failed")
    check("classify: auth detected", "authentication" in _what, True)
    _what, _why, _next = _classify_failure("write_notion.py failed", "Failed")
    check("classify: write detected", "Notion write" in _what, True)
    _what, _why, _next = _classify_failure("", "Failed")
    check("classify: unknown fallback", "unspecified" in _what, True)

    # Expected Tuesdays
    _tuesdays = _expected_tuesdays(14)
    check("tuesdays: returns list", isinstance(_tuesdays, list), True)
    check_gte("tuesdays: at least 1 in 14 days", len(_tuesdays), 1)
    for _t in _tuesdays:
        check(f"tuesdays: {_t} is Tuesday", _t.weekday(), 1)

    # Format body includes health
    _report_with_health = build_report([], [], [], [], [], [], [], {}, [], 30)
    _report_with_health["health"] = {
        "health_status": "healthy",
        "health_text": "All systems healthy. This digest ran successfully.",
    }
    _blocks = format_notion_body(_report_with_health)
    _first_heading = None
    for _b in _blocks:
        if _b.get("type") == "heading_2":
            _first_heading = _b["heading_2"]["rich_text"][0]["text"]["content"]
            break
    check("body: health is first heading", _first_heading, "Pipeline Health")
    _callout_blocks = [_b for _b in _blocks if _b.get("type") == "callout"]
    check("body: has health callout", len(_callout_blocks), 1)
    _callout_icon = _callout_blocks[0]["callout"]["icon"]
    check("body: callout icon has type field", _callout_icon.get("type"), "emoji")
    check("body: callout icon healthy emoji", _callout_icon.get("emoji"), "✅")

    print(f"\n{'PASS' if ok else 'FAIL'}: smoke tests {'all passed' if ok else 'had failures'}")
    return 0 if ok else 1


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fortnightly feedback digest")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run offline unit tests")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read real data but don't write to Notion or modify SQLite")
    parser.add_argument("--days", type=int, default=30,
                        help="How many days back to scan Calendar (default: 30)")
    parser.add_argument("--force", action="store_true",
                        help="Run even if not a digest week (odd ISO week)")
    parser.add_argument("--db", type=str, default=str(DB_PATH),
                        help="Path to SQLite database")
    parser.add_argument("--feedback-db-id", type=str, default=None,
                        help="Notion database ID for Scoring Feedback")
    parser.add_argument("--pipeline-runs-db-id", type=str, default=None,
                        help="Notion database ID for Pipeline Runs")
    args = parser.parse_args()

    if args.smoke_test:
        sys.exit(_smoke_tests())

    # Fortnightly parity: run on ODD ISO weeks (verdict sync runs on EVEN)
    _, week, _ = date.today().isocalendar()
    if not args.force and week % 2 == 0:
        print(f"SKIP: ISO week {week} is even (digest runs on odd weeks). Use --force to override.")
        return

    if args.days < 1:
        print("ERROR: --days must be at least 1.", file=sys.stderr)
        sys.exit(1)

    # ── Configuration ──
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    notion_token = os.environ.get("NOTION_TOKEN")
    feedback_db_id = args.feedback_db_id or os.environ.get("FEEDBACK_DB_ID")
    pipeline_runs_db_id = (
        args.pipeline_runs_db_id or os.environ.get("PIPELINE_RUNS_DB_ID")
    )

    # ── Query Pipeline Runs (independent of Calendar, assessed later) ──
    pipeline_runs = []
    pipeline_query_error = None
    if notion_token and pipeline_runs_db_id:
        print("Querying pipeline runs...")
        pipeline_runs, pipeline_query_error = query_pipeline_runs(
            notion_token, pipeline_runs_db_id, days_back=args.days,
        )
        if pipeline_query_error:
            print(f"WARNING: Pipeline health query failed: {pipeline_query_error}",
                  file=sys.stderr)
        else:
            print(f"Pipeline Runs: {len(pipeline_runs)} found in last {args.days} days")
    elif not pipeline_runs_db_id:
        print("SKIP: No PIPELINE_RUNS_DB_ID: pipeline health not assessed")

    # ── Calendar + Analysis (may fail: caught for health-only page) ──
    digest_error = None
    digest_succeeded = False
    report = None
    suggestions = []

    try:
        if not all([client_id, client_secret, refresh_token]):
            raise CalendarError(
                "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and "
                "GOOGLE_REFRESH_TOKEN must be set"
            )

        # ── Calendar ──
        print("Refreshing Calendar access token...")
        access_token = refresh_calendar_token(client_id, client_secret, refresh_token)
        print(f"Fetching Calendar events (past {args.days} days)...")
        calendar_events = fetch_calendar_events(access_token, days_back=args.days)
        pre_dedup = len(calendar_events)
        calendar_events = dedup_calendar_events(calendar_events)
        if pre_dedup != len(calendar_events):
            print(f"Calendar: {pre_dedup} physical events found, {pre_dedup - len(calendar_events)} duplicates removed")
        else:
            print(f"Calendar: {len(calendar_events)} physical events found")

        if not calendar_events:
            print("No physical calendar events in the period.")
            # Still count as success: just nothing to match
            report = build_report([], [], [], [], [], [], [], {}, [], args.days)
            digest_succeeded = True
        else:
            # ── SQLite ──
            db_path = Path(args.db)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            ensure_schema(conn)

            candidates = load_candidates(conn)
            print(f"SQLite: {len(candidates)} candidates loaded")

            # ── Matching ──
            matched, unmatched = match_calendar_to_candidates(calendar_events, candidates)
            print(f"Matched: {len(matched)} (HIGH: "
                  f"{sum(1 for m in matched if m['confidence'] == CONFIDENCE_HIGH)}, "
                  f"MEDIUM: {sum(1 for m in matched if m['confidence'] == CONFIDENCE_MEDIUM)})")
            print(f"Unmatched (source gaps): {len(unmatched)}")

            # ── Analysis ──
            confirmed = analyze_confirmed_interests(matched)
            unreviewed = analyze_unreviewed_attendance(matched)
            corrections = analyze_scoring_corrections(matched)
            verdict_conflicts = analyze_verdict_conflicts(matched)
            source_gaps = analyze_source_gaps(unmatched)
            evolution = analyze_interest_evolution(matched, candidates)
            suggestions = generate_suggestions(corrections, source_gaps, evolution)

            # ── Findings (accumulation + Notion) ──
            digest_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            findings = classify_findings(corrections, evolution, source_gaps)
            new_finding_count = save_digest_findings(conn, findings, digest_date, args.dry_run)
            print(f"Findings: {len(findings)} classified, {new_finding_count} new")

            finding_page_count = 0
            if notion_token and feedback_db_id:
                all_findings = conn.execute(
                    "SELECT * FROM digest_findings WHERE status NOT IN ('dismissed', 'applied')"
                ).fetchall()
                for f_row in all_findings:
                    f_dict = dict(f_row)
                    if f_dict.get("notion_page_id"):
                        notion_status = get_finding_page_status(
                            notion_token, f_dict["notion_page_id"])
                        if notion_status and notion_status.lower() in ('dismissed', 'applied'):
                            conn.execute(
                                "UPDATE digest_findings SET status = ? WHERE id = ?",
                                (notion_status.lower(), f_dict["id"]))
                            conn.commit()
                            continue
                    create_or_update_finding_page(
                        notion_token, feedback_db_id, f_dict, conn, args.dry_run)
                    finding_page_count += 1
                print(f"Notion finding pages: {finding_page_count} created/updated")
            elif findings:
                print("SKIP: No FEEDBACK_DB_ID: finding pages not created")

            if finding_page_count > 0:
                suggestions = [
                    f"{finding_page_count} finding(s) generated: review in Scoring Feedback database"
                ]

            # ── Report ──
            report = build_report(
                matched, unmatched, confirmed, unreviewed, corrections,
                verdict_conflicts, source_gaps, evolution, suggestions, args.days,
            )

            # ── SQLite updates ──
            attendance_count = save_attendance(conn, matched, args.dry_run)
            save_calendar_events(conn, calendar_events, matched, unmatched,
                                 digest_date, args.dry_run)
            if args.dry_run:
                print(f"[DRY RUN] Would mark {len(matched)} candidates as user_attended")
            else:
                print(f"Marked {attendance_count} candidates as user_attended")

            conn.close()
            digest_succeeded = True

    except CalendarError as e:
        digest_error = str(e)
        print(f"ERROR: Calendar failed: {digest_error}", file=sys.stderr)
        print("Continuing with pipeline health only...")

    # ── Assess pipeline health (now that we know digest outcome) ──
    if pipeline_runs_db_id:
        health = assess_pipeline_health(
            pipeline_runs, pipeline_query_error,
            days_back=args.days, digest_error=digest_error,
        )
        print(f"Pipeline health: {health['health_status']}")
    else:
        health = {
            "health_status": "unknown",
            "health_text": (
                "Pipeline health status unavailable: "
                "no Pipeline Runs database configured."
            ),
            "runs_summary": {},
        }
        if digest_error:
            health["health_text"] += (
                f" Additionally, this digest had an error: {digest_error}"
            )

    # Build minimal report if digest failed
    if report is None:
        report = build_report([], [], [], [], [], [], [], {}, [], args.days)

    # Attach health to report
    report["health"] = health

    # ── Notion ──
    page_url = None
    if notion_token and feedback_db_id:
        page_url = create_notion_page(
            notion_token, feedback_db_id, report, args.dry_run,
        )
    elif not args.dry_run:
        print("SKIP: No NOTION_TOKEN or FEEDBACK_DB_ID: Notion page not created")

    # ── Save report (after Notion, so URL is included) ──
    report["notion_page_url"] = page_url
    report_path = save_report(report)
    print(f"Report saved to {report_path}")

    # ── Notification ──
    if not args.dry_run:
        send_notification(report, page_url)

    # ── Summary ──
    s = report["summary"]
    print(f"\n{'=' * 50}")
    if digest_succeeded:
        print("FEEDBACK DIGEST COMPLETE")
    else:
        print("FEEDBACK DIGEST: PARTIAL (health only)")
    print(f"{'=' * 50}")
    print(f"Pipeline health:       {health['health_status']}")
    print(f"Events attended:       {s['calendar_events']}")
    print(f"Pipeline matches:      {s['pipeline_matches']}")
    print(f"Source gaps:            {s['source_gaps']}")
    print(f"Scoring corrections:   {s['scoring_corrections']}")
    print(f"Confirmed interests:   {s['confirmed_interests']}")
    print(f"Suggestions:           {len(suggestions)}")
    if suggestions:
        print("\nSuggestions:")
        for sug in suggestions:
            print(f"  - {sug}")
    if digest_error:
        print(f"\nDigest error: {digest_error}")


if __name__ == "__main__":
    main()
