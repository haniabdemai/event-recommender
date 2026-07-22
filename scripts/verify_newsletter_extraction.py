#!/usr/bin/env python3
"""
QA Phase 2: Verify newsletter extraction accuracy.

Pass 1 (deterministic): substring matching: check that each candidate's key
fields (name, venue, date, URL) appear in the source email body.

Pass 2 (subagent): for emails with flagged candidates or multiple candidates,
builds prompts for Sonnet subagent verification. The pipeline session spawns
the subagents (via the Agent tool) and writes results back. This script only
prepares the prompts: it does not call any external API.

Requires: processed_emails.body_text populated (Step 2f --body-stdin).
Newsletter candidates must have source_snapshot set to the Gmail message_id
(Step 2e) to link candidates to their source email.

Exit codes:
    0 = all checks passed (or no newsletter candidates to verify)
    1 = one or more mismatches found (see output and .last_qa_newsletter.json)

Usage:
    python3 scripts/verify_newsletter_extraction.py --run-date YYYY-MM-DD
"""
import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402

DATE_FORMATS = [
    "%Y-%m-%d",     # 2026-06-05
    "%-d %B",       # 5 June
    "%B %-d",       # June 5
    "%-d %b",       # 5 Jun
    "%b %-d",       # Jun 5
    "%d/%m",        # 05/06
    "%-d/%m",       # 5/6
]

# Platform-specific: %-d doesn't work on Windows, but pipeline runs on macOS/Linux
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        datetime.strptime("5", "%-d")
        _DASH_D_WORKS = True
    except ValueError:
        _DASH_D_WORKS = False


def _date_variants(date_str):
    """Generate possible text representations of a YYYY-MM-DD date."""
    if not date_str:
        return []
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return [date_str]

    variants = [date_str]
    for fmt in DATE_FORMATS:
        try:
            if not _DASH_D_WORKS and "%-" in fmt:
                fmt = fmt.replace("%-d", "%d").replace("%-m", "%m")
            variants.append(dt.strftime(fmt))
        except ValueError:
            continue

    day = dt.day
    month_full = dt.strftime("%B")
    month_short = dt.strftime("%b")
    variants.extend([
        f"{day} {month_full}",
        f"{month_full} {day}",
        f"{day} {month_short}",
        f"{month_short} {day}",
        f"{day}{_ordinal(day)} {month_full}",
        f"{day}{_ordinal(day)} {month_short}",
    ])

    return list(set(variants))


def _ordinal(n):
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _date_in_range_context(date_str, body_text):
    """Check if a YYYY-MM-DD date appears as the start of a date range in body_text.

    Venue newsletters use patterns like "6–13 Jun", "9 & 11 Jun", "19–21 Jun"
    where the month name follows the full range, not each individual day. Standard
    _date_variants() generates "6 Jun" which won't match "6–13 Jun" as a substring.

    This function checks for the day at the start of a range followed by the month.
    """
    if not date_str or not body_text:
        return False
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return False

    day = dt.day
    month_full = dt.strftime("%B")
    month_short = dt.strftime("%b")
    body_lower = body_text.lower()

    for month in (month_short, month_full):
        pattern = (
            r'\b'
            + str(day)
            + r'\s*[–\-&,]\s*\d{1,2}\s+'
            + re.escape(month.lower())
            + r'\b'
        )
        if re.search(pattern, body_lower):
            return True

        pattern_to = (
            r'\b'
            + str(day)
            + r'\s+to\s+\d{1,2}\s+'
            + re.escape(month.lower())
            + r'\b'
        )
        if re.search(pattern_to, body_lower):
            return True

    return False


import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from erlib.normalise import TYPO_TABLE as _TYPO_TABLE  # noqa: E402


def _normalise_unicode(text):
    """Normalise Unicode so typographic variants match their ASCII equivalents.

    NFKD handles: NBSP→space, ellipsis→'...', ligatures→expanded.
    The translation table handles what NFKD doesn't: curly quotes, em/en dashes,
    zero-width characters, narrow NBSP.
    """
    return unicodedata.normalize("NFKD", text).translate(_TYPO_TABLE)


def _fuzzy_contains(haystack, needle, threshold=0.8):
    """Case-insensitive substring check with Unicode normalisation."""
    if not needle or not haystack:
        return False
    return _normalise_unicode(needle).lower().strip() in _normalise_unicode(haystack).lower()


_BODY_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)


def _url_domain(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def url_check(url, body):
    """Verify a candidate URL against the email body.

    Extraction is allowed to canonicalise links (tracking wrappers resolved to
    the real event page, lu.ma slug normalisation), so an exact substring miss
    is only suspicious when the body carries links to compare against. Returns
    "match", "NOT_FOUND", or "skipped" (body has no links: stored bodies are
    sometimes plain-text-stripped and carry nothing to verify).
    2026-07-07: the exact-match version of this check auto-vetoed three real,
    already-written events (candidates 3364/3369/3370).

    Public: write_ready_check.py runs the SAME check as a pre-write gate
    (invented-URL incident 2026-07-05, candidates 3046-3051/3054).
    """
    if url in body:
        return "match"
    body_links = _BODY_URL_RE.findall(body)
    if not body_links:
        return "skipped"
    domain = _url_domain(url)
    # lu.ma and luma.com are the same service: scoring canonicalises stored
    # URLs to lu.ma while email bodies carry luma.com links. Without this
    # equivalence every canonicalised Luma newsletter event reads NOT_FOUND.
    domains = {"lu.ma", "luma.com"} if domain in ("lu.ma", "luma.com") else {domain}
    if domain and any(d in link.lower() for link in body_links for d in domains):
        return "match"
    return "NOT_FOUND"


def pass1_substring_check(candidates, emails_by_id):
    """Deterministic substring matching. Returns list of findings."""
    findings = []

    for cand in candidates:
        msg_id = cand["source_snapshot"]
        if not msg_id or msg_id not in emails_by_id:
            findings.append({
                "candidate_id": cand["id"],
                "candidate_name": cand["name"],
                "status": "skipped",
                "reason": "no email body available",
                "fields": {},
            })
            continue

        body = emails_by_id[msg_id]
        body_lower = _normalise_unicode(body).lower()
        field_results = {}

        # Name check
        name = cand["name"] or ""
        if name:
            name_found = _fuzzy_contains(body, name)
            if not name_found:
                words = name.split()
                if len(words) > 2:
                    name_found = any(_fuzzy_contains(body, " ".join(words[i:i+3]))
                                     for i in range(len(words) - 2))
            field_results["name"] = "match" if name_found else "NOT_FOUND"
        else:
            field_results["name"] = "missing"

        # Venue check
        venue = cand["venue_name"] or ""
        if venue and venue.lower() not in ("tbc", "unknown", "online", "various"):
            field_results["venue"] = "match" if _fuzzy_contains(body, venue) else "NOT_FOUND"
        else:
            field_results["venue"] = "skipped"

        # Date check
        event_date = cand["date"] or ""
        if event_date:
            variants = _date_variants(event_date)
            date_found = any(v.lower() in body_lower for v in variants)
            if not date_found:
                date_found = _date_in_range_context(event_date, body)
            field_results["date"] = "match" if date_found else "NOT_FOUND"
        else:
            field_results["date"] = "missing"

        # URL check
        url = cand["url"] or ""
        if url:
            field_results["url"] = url_check(url, body)
        else:
            field_results["url"] = "missing"

        has_mismatch = any(v == "NOT_FOUND" for v in field_results.values())
        findings.append({
            "candidate_id": cand["id"],
            "candidate_name": cand["name"],
            "message_id": msg_id,
            "status": "mismatch" if has_mismatch else "ok",
            "fields": field_results,
        })

    return findings


def build_pass2_subagent_prompts(flagged_emails, emails_by_id, candidates_by_email):
    """Build subagent prompts for LLM verification. Returns list of prompt dicts.

    These prompts are designed to be passed to Sonnet subagents by the pipeline
    session (via the Agent tool). Each prompt covers one email and its candidates.
    """
    prompts = []
    for msg_id in flagged_emails:
        body = emails_by_id.get(msg_id, "")
        cands = candidates_by_email.get(msg_id, [])
        if not body or not cands:
            continue

        cand_text = json.dumps([{
            "id": c["id"], "name": c["name"], "date": c["date"],
            "venue": c["venue_name"], "url": c["url"],
            "description": "[enriched from event page: skip description check]"
                if c.get("description_source") == "page"
                else (c["description"] or "")[:200],
        } for c in cands], indent=2)

        prompt = (
            "Compare each extracted event against the email below. "
            "For each candidate, check: does the name appear in the email? "
            "Does the date match? Does the venue match? Does the URL appear? "
            "Does the description match the email content?\n\n"
            "Return ONLY a JSON array with one object per candidate:\n"
            '{"id": <id>, "name_match": true/false, "date_match": true/false, '
            '"venue_match": true/false, "url_match": true/false, '
            '"description_match": true/false, "notes": "explanation"}\n\n'
            f"EXTRACTED CANDIDATES:\n{cand_text}\n\n"
            f"EMAIL BODY:\n{body[:8000]}\n"
        )

        prompts.append({
            "message_id": msg_id,
            "candidates_count": len(cands),
            "prompt": prompt,
        })

    return prompts


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    p.add_argument("--run-date", required=True, help="Run date to verify (YYYY-MM-DD)")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Load newsletter candidates from this run
    candidates = conn.execute(
        """SELECT id, name, date, time, venue_name, url, description,
                  source_snapshot, organiser, description_source
           FROM candidates
           WHERE run_date = ? AND source = 'Newsletter' AND pipeline_state != 'vetoed'""",
        (args.run_date,)
    ).fetchall()
    candidates = [dict(r) for r in candidates]

    if not candidates:
        print("No newsletter candidates to verify")
        conn.close()
        return 0

    print(f"Newsletter verification: {len(candidates)} candidates from {args.run_date}")

    # Load email bodies keyed by message_id
    emails = conn.execute(
        "SELECT gmail_message_id, body_text FROM processed_emails WHERE body_text IS NOT NULL"
    ).fetchall()
    emails_by_id = {r["gmail_message_id"]: r["body_text"] for r in emails}
    conn.close()

    print(f"Email bodies available: {len(emails_by_id)}")

    # Group candidates by email
    candidates_by_email = {}
    for c in candidates:
        msg_id = c.get("source_snapshot")
        if msg_id:
            candidates_by_email.setdefault(msg_id, []).append(c)

    # Pass 1: deterministic substring matching
    print("\n--- Pass 1: Substring Matching ---")
    p1_findings = pass1_substring_check(candidates, emails_by_id)

    ok_count = sum(1 for f in p1_findings if f["status"] == "ok")
    mismatch_count = sum(1 for f in p1_findings if f["status"] == "mismatch")
    skipped_count = sum(1 for f in p1_findings if f["status"] == "skipped")
    print(f"  OK: {ok_count}, Mismatches: {mismatch_count}, Skipped: {skipped_count}")

    for f in p1_findings:
        if f["status"] == "mismatch":
            not_found = [k for k, v in f["fields"].items() if v == "NOT_FOUND"]
            print(f"  MISMATCH [{f['candidate_id']}] {f['candidate_name']}: "
                  f"fields not in email: {not_found}")

    # Pass 2: identify emails needing LLM verification via subagent
    flagged_emails = set()
    for f in p1_findings:
        if f["status"] == "mismatch" and f.get("message_id"):
            flagged_emails.add(f["message_id"])
    for msg_id, cands in candidates_by_email.items():
        if len(cands) >= 3:
            flagged_emails.add(msg_id)

    p2_prompts = []
    if flagged_emails:
        p2_prompts = build_pass2_subagent_prompts(flagged_emails, emails_by_id, candidates_by_email)
        print(f"\n--- Pass 2: {len(p2_prompts)} emails need Sonnet subagent verification ---")
        for p in p2_prompts:
            print(f"  Email {p['message_id'][:20]}... ({p['candidates_count']} candidates)")
    else:
        print("\n--- Pass 2: Not needed (no flagged emails) ---")

    # Write results
    output = {
        "run_date": args.run_date,
        "candidates_checked": len(candidates),
        "emails_with_bodies": len(emails_by_id),
        "pass1": {
            "ok": ok_count,
            "mismatches": mismatch_count,
            "skipped": skipped_count,
            "findings": p1_findings,
        },
        "pass2": {
            "emails_flagged": len(flagged_emails),
            "subagent_prompts": p2_prompts,
        },
    }

    out_path = SCRIPT_DIR / ".last_qa_newsletter.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\nResults written to {out_path}")

    if mismatch_count > 0:
        print(f"\nNEWSLETTER VERIFICATION: {mismatch_count} MISMATCHES FOUND")
        return 1
    else:
        print("\nNEWSLETTER VERIFICATION PASSED")
        return 0


_DATE_RANGE_SELF_TESTS = [
    # (date_str, body_text, expected): True means date should be found
    # Exact date range patterns seen in real venue newsletters
    ("2026-06-09", "9 & 11 Jun, Talks", True),           # ampersand range
    ("2026-06-19", "19–21 Jun, Music", True),             # en-dash range
    ("2026-06-06", "6–13 Jun, The Pit", True),            # en-dash range, wider span
    ("2026-06-06", "6-13 Jun, The Pit", True),            # regular hyphen range
    ("2026-06-06", "6, 13 Jun, The Pit", True),           # comma-separated dates
    ("2026-06-09", "9 to 11 Jun, Talks", True),           # "to" range
    ("2026-06-06", "6–13 June, The Pit", True),           # full month name
    # Must NOT match: day appears mid-number (e.g. "16" should not match day 6)
    ("2026-06-06", "16–20 Jun, Exhibition", False),
    # Must NOT match: wrong month
    ("2026-06-09", "9 & 11 Jul, Talks", False),
    # Must NOT match: day is the END of a range, not start
    ("2026-06-13", "6–13 Jun, The Pit", False),
    # Single-date variants still work via _date_variants (not this function)
    ("2026-06-06", "6 Jun somewhere", False),             # _date_in_range_context alone won't match this
]


def _run_date_range_self_tests():
    failures = []
    for date_str, body, expected in _DATE_RANGE_SELF_TESTS:
        result = _date_in_range_context(date_str, body)
        if result != expected:
            failures.append(
                f"  _date_in_range_context({date_str!r}, {body!r}): "
                f"got {result}, expected {expected}"
            )
    if failures:
        print("DATE RANGE SELF-TEST FAILURES:", file=sys.stderr)
        for f in failures:
            print(f, file=sys.stderr)
        sys.exit(1)



def _smoke_test():
    """Verify Unicode normalisation handles known problem cases."""
    failures = []

    def _check(label, haystack, needle, expected):
        result = _fuzzy_contains(haystack, needle)
        if result != expected:
            failures.append(f"FAIL: {label}: expected {expected}, got {result}")

    # --- _normalise_unicode unit checks ---
    assert _normalise_unicode("Gay’s") == "Gay's", "curly apostrophe"
    assert _normalise_unicode("Art—Life") == "Art-Life", "em dash"
    assert _normalise_unicode("6–13 Jun") == "6-13 Jun", "en dash"
    assert _normalise_unicode("Sat 6\xa0Jun") == "Sat 6 Jun", "NBSP"
    assert _normalise_unicode("Sat 6 Jun") == "Sat 6 Jun", "narrow NBSP"
    assert _normalise_unicode("hello​world") == "helloworld", "zero-width space"
    assert _normalise_unicode("café") == _normalise_unicode("café"), "NFC/NFD equivalence"
    assert _normalise_unicode("plain ascii") == "plain ascii", "ASCII unchanged"
    assert _normalise_unicode("“Quoted”") == '"Quoted"', "curly double quotes"
    assert _normalise_unicode("wait…") == "wait...", "ellipsis"

    # --- _fuzzy_contains integration checks ---
    # The actual bug: candidate 1491 (curly apostrophe in email body)
    _check("curly apostrophe match",
           "Mara Lune’s We Chree at the Arts Centre", "Mara Lune's We Chree", True)
    _check("curly apostrophe reverse",
           "Mara Lune's We Chree at the Arts Centre", "Mara Lune’s We Chree", True)

    # NBSP in body text (candidate 1486 delegation from QA date check peer)
    _check("NBSP in body", "Sat 6\xa0Jun 2026", "Sat 6 Jun", True)
    _check("narrow NBSP", "Sat 6 Jun 2026", "Sat 6 Jun", True)

    # En dash in event names
    _check("en dash in name", "Festival 6–13 June", "Festival 6-13 June", True)

    # Zero-width space between words
    _check("zero-width space", "Hello​World event", "HelloWorld", True)

    # Em dash normalisation
    _check("em dash", "Art:Life Exhibition", "Art-Life Exhibition", True)

    # Ellipsis
    _check("ellipsis", "Ratzo Presents… at the Arts Centre", "Ratzo Presents...", True)

    # Normal ASCII still works (regression guard)
    _check("plain match", "London Jazz Festival at the Arts Centre", "Jazz Festival", True)
    _check("plain no match", "London Jazz Festival at the Arts Centre", "Rock Concert", False)
    _check("case insensitive", "LONDON jazz FESTIVAL", "London Jazz Festival", True)

    # Empty/None guards
    _check("empty needle", "some text", "", False)
    _check("empty haystack", "", "needle", False)

    if failures:
        for f in failures:
            print(f"  {f}")
        print(f"\n{len(failures)} smoke test(s) FAILED")
        return 1

    print("All smoke tests passed")
    return 0



if __name__ == "__main__":
    _run_date_range_self_tests()
    if "--smoke-test" in sys.argv:
        sys.exit(_smoke_test())
    sys.exit(main())
