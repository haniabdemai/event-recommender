#!/usr/bin/env python3
"""
Fetch Meetup events via the unauthenticated gql2 GraphQL API.

Two passes:
  Source 2A: events from MY_MEETUP_GROUPS (priority, no triage needed)
  Source 2B: recommendedEvents for your city (broad discovery, deduped against 2A)

Writes new candidates to SQLite. Skips duplicates by URL, then (name, date).

Usage:
    python3 fetch_meetup.py                     # today → +6 weeks
    python3 fetch_meetup.py --end-date 2026-06-30
    python3 fetch_meetup.py --dry-run           # print what would be inserted
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

import argparse
import functools
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import urllib.error
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.config import CITY_NAME, LONDON_LAT, LONDON_LON  # noqa: E402

GQL_URL = "https://www.meetup.com/gql2"
PACE_SECONDS = 0.3

# Your Meetup group memberships: configured via ER_MEETUP_GROUPS
# (see .env.example). score_candidates.py re-exports this same list.
from erlib.config import MEETUP_GROUPS as MY_MEETUP_GROUPS  # noqa: E402

# Typographic → ASCII mappings for Unicode normalisation.
# Matches the table in scripts/verify_newsletter_extraction.py and reconcile_notion.py.
from erlib.dates import end_date_if_different, iso_to_london  # noqa: E402
from erlib.dedup import is_duplicate, load_existing  # noqa: E402
from erlib.normalise import normalise_name as _normalise_name  # noqa: E402

GROUP_EVENTS_QUERY = """
query($urlname: String!) {
  groupByUrlname(urlname: $urlname) {
    id name urlname
    events(first: 50, status: ACTIVE) {
      edges {
        node {
          id title dateTime endTime description eventType eventUrl
          venue { name address city country }
          group { name urlname }
          feeSettings { amount currency }
        }
      }
    }
  }
}
"""

RECOMMENDED_QUERY = """
query($filter: RecommendedEventsFilter!, $first: Int, $after: String) {
  recommendedEvents(filter: $filter, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id title dateTime endTime description eventType eventUrl
        venue { name address city country }
        group { name urlname }
        feeSettings { amount currency }
      }
    }
  }
}
"""


class GraphQLError(RuntimeError):
    """A 200 response whose body carries GraphQL errors instead of data."""


def _raise_on_gql_errors(resp_json):
    """Surface in-band GraphQL errors (audit P2: a 200 with
    {"errors": ..., "data": null} was silently treated as "no events").

    Raises GraphQLError when errors arrive with no usable data; logs a
    warning when errors accompany partial data (run continues on it).
    """
    errors = resp_json.get("errors")
    if not errors:
        return resp_json
    messages = "; ".join(str(e.get("message", e)) for e in errors[:3])
    if not resp_json.get("data"):
        raise GraphQLError(f"GraphQL errors: {messages}")
    print(f"  WARN Meetup GraphQL partial errors: {messages}", file=sys.stderr)
    return resp_json


def gql_post(query, variables):
    """POST a GraphQL query to Meetup's gql2 endpoint."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = Request(GQL_URL, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    })
    try:
        with urlopen(req, timeout=30) as resp:
            return _raise_on_gql_errors(json.loads(resp.read()))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        hdrs = dict(e.headers.items()) if e.headers else {}
        print(f"  DIAG Meetup HTTP {e.code}: body={body!r}", file=sys.stderr)
        print(f"  DIAG Meetup headers: {hdrs}", file=sys.stderr)
        raise


@functools.cache
def _kw_re(kw):
    """Compile a keyword to a case-insensitive word-boundary regex."""
    return re.compile(r"\b" + re.escape(kw.strip().lower()) + r"\b", re.IGNORECASE)


def _matches_any(keywords, text):
    """True if any keyword appears as a whole word in text."""
    return any(_kw_re(kw).search(text) for kw in keywords)


def classify_format(title, description):
    """Classify event format from title and description.

    Uses the same vocabulary as score_candidates.py signals.
    All matching is word-boundary-bounded: no raw substring checks.
    Order matters: more specific categories checked first.

    Canonical categories (12): Social meetup, Workshop, Talk, Outdoor,
    Creative social, Concert, Exhibition, Wellness, Immersive experience,
    Film, Book event, Other.
    """
    blob = f"{title} {description}"

    # Outdoor / hiking / walking: title only for ambiguous terms
    # ("walk" in descriptions is often transit directions)
    if _matches_any([
        "hike", "hikes", "hiking",
        "walk", "walking tour", "guided walk", "nature walk",
        "canal walk", "riverside walk", "photo walk", "photowalk",
        "art walk", "ramble", "rambling", "trail",
        "trek", "stroll", "wander", "peaks",
        "day trip", "circular",
    ], title):
        return "Outdoor"

    # Miles in title: "14 miles", "6 or 8 miles", "7miles" etc.
    if re.search(r'\b\d+\s*(?:or\s+\d+\s*)?miles?\b', title, re.IGNORECASE):
        return "Outdoor"

    # Strong outdoor signals unambiguous even in descriptions
    if _matches_any(["hike", "hikes", "hiking"], blob):
        return "Outdoor"

    # Creative social: check before workshop (sketch/drawing are art, not workshops)
    if _matches_any([
        "sketch", "drawing", "painting", "collage", "pottery", "ceramics",
        "printmaking", "embroidery", "write club", "silent writing",
        "art jam", "creative workshop", "art workshop",
    ], blob):
        return "Creative social"

    # Workshop / hands-on (absorbs Hackday, Course)
    if _matches_any([
        "workshop", "hands-on", "hands on", "hackathon", "hackday", "hack day",
        "hack night", "hacknight", "build session", "jam session",
        "bring your laptop", "vibe coding", "live coding",
        "course", "training session", "masterclass",
    ], blob):
        return "Workshop"

    # Immersive experience
    if _matches_any([
        "immersive", "installation", "sensory experience",
        "multisensory", "interactive art", "multimedia",
    ], blob):
        return "Immersive experience"

    # Film
    if _matches_any([
        "film screening", "cinema", "movie night", "documentary screening",
        "screening", "short film",
    ], blob):
        return "Film"

    # Book event
    if _matches_any([
        "book launch", "book signing", "author event", "book reading",
        "book talk",
    ], blob):
        return "Book event"

    # Exhibition (absorbs Museum visit): before Talk so "exhibition" beats "panel"
    # "gallery" matches TITLE ONLY: descriptions use it for "photo gallery"
    if _matches_any(["gallery"], title) or _matches_any([
        "exhibition", "museum visit", "museum",
    ], blob):
        return "Exhibition"

    # Talk / panel / presentation
    # "talk" matches TITLE ONLY: too common in casual descriptions
    # ("no pressure to talk", "come talk to people")
    if _matches_any(["talk"], title) or _matches_any([
        "panel", "presentation", "fireside chat", "keynote",
        "speaker", "lecture", "seminar", "symposium",
        "demo day", "demo night",
    ], blob) or re.search(r"meetup\s*#\d", title, re.IGNORECASE):
        return "Talk"

    # Wellness
    if _matches_any([
        "breathwork", "sound bath", "sound healing", "yoga", "meditation",
        "wellness", "spa",
    ], blob):
        return "Wellness"

    # Concert / music performance
    # "dj" matches TITLE ONLY: social events mention DJs in descriptions
    if _matches_any(["dj"], title) or _matches_any([
        "concert", "gig", "live music", "dj set", "music festival", "tribute",
    ], blob):
        return "Concert"

    return "Social meetup"


_FORMAT_SELF_TESTS = [
    # Outdoor: title keywords
    ("Bank Holiday trek - Hampstead Heath, 6 miles", "", "Outdoor"),
    ("A stroll to (The Prospect of) Whitby", "", "Outdoor"),
    ("A wander through Wapping with Tim", "", "Outdoor"),
    ("Midsummer Surrey Hills Three Peaks Challenge", "", "Outdoor"),
    ("Evening guided walk through Shoreditch", "", "Outdoor"),
    ("Sunday hiking group - Box Hill", "", "Outdoor"),
    ("Regent's Canal ramble and pub lunch", "", "Outdoor"),
    ("FREE | Maple Fox Hikes #8 | The Iconic Seven Sisters", "", "Outdoor"),
    ("Day trip by train - to historic St. Albans!", "", "Outdoor"),
    ("Day trip by train - Hatfield House in Hertfordshire!", "", "Outdoor"),
    # Outdoor: miles pattern in title
    ("Harewood Bluebells (14 or 11 miles)", "", "Outdoor"),
    ("Beacon Point - (16 or 8 miles)", "", "Outdoor"),
    ("Bridge to Bridge 2.0: Leisure 7miles Stroll and Lunch", "", "Outdoor"),
    # Outdoor: hike in description catches events with no title keyword
    ("13 JUN - GROUP TICKETS - Saxon Shore Way", "Join us for a hike along the coast", "Outdoor"),
    # Talk: "talk" in TITLE triggers Talk
    ("AI Ethics Talk at City Arts Centre", "", "Talk"),
    ("Nexus AI Demo Night 2.0", "", "Talk"),
    ("Meetup #3", "", "Talk"),
    # Talk: "talk" in DESCRIPTION does NOT trigger Talk (too common as English word)
    ("Picnic in the Park Social", "come talk to new people and enjoy the park", "Social meetup"),
    ("Cosy Silent Reads", "no pressure to talk, just bring a book", "Social meetup"),
    ("20s 30s London Global Socializing", "talk to new friends from around the world", "Social meetup"),
    # Concert: "dj" in TITLE triggers Concert
    ("DJ Night at Village Underground", "", "Concert"),
    ("DAVID LIVE [BOWIE] Tribute", "", "Concert"),
    # Concert: "dj" in DESCRIPTION does NOT trigger Concert
    ("London Social Night | City Circle", "DJ playing background music at our social", "Social meetup"),
    # Exhibition: "gallery" in DESCRIPTION does NOT trigger Exhibition ("photo gallery")
    ("London Social Night", "see the photo gallery from the night", "Social meetup"),
    # Exhibition: "gallery" in TITLE does trigger Exhibition
    ("Whitechapel Gallery Open", "", "Exhibition"),
    # Exhibition: "exhibition" in description beats "panel" for Talk
    ("GearExpo UK", "full exhibition, panel discussions", "Exhibition"),
    # Talk: "symposium" in description triggers Talk
    ("Art and Politics", "A symposium exploring creativity", "Talk"),
    # Talk: "speaker" in description triggers Talk (even without "talk" in title)
    ("AI Builders London Meetup", "Join us for a talk by leading speaker", "Talk"),
    # Workshop: "demo night" must not steal from Workshop (Workshop checked first)
    ("Hands-on ML Demo Night Workshop", "", "Workshop"),
    # Guards: default must stay Social meetup
    ("Friday drinks in Soho", "", "Social meetup"),
    ("Evening drinks for 20s & 30s", "", "Social meetup"),
    ("Anish Kapoor", "", "Social meetup"),
    # Guards: other categories still work
    ("Film screening at City Arts Centre", "", "Film"),
    ("Book launch: New Novel", "", "Book event"),
    ("Pottery and wine evening", "", "Creative social"),
]


def _run_format_self_test():
    failures = []
    for title, desc, expected in _FORMAT_SELF_TESTS:
        result = classify_format(title, desc)
        if result != expected:
            failures.append(f"  {title!r} -> {result!r} (expected {expected!r})")
    if failures:
        print("FORMAT SELF-TEST FAILED:", file=sys.stderr)
        for f in failures:
            print(f, file=sys.stderr)
        sys.exit(1)


def parse_event(node):
    """Convert a GraphQL event node into a candidate dict matching the DB schema."""
    # erlib.dates converts any source offset to Europe/London: previously
    # this kept the raw offset's wall clock (audit: Meetup inconsistency).
    dt_str = node.get("dateTime") or ""
    event_date, event_time = iso_to_london(dt_str)
    end_date = end_date_if_different(dt_str, node.get("endTime") or "")

    venue = node.get("venue") or {}
    venue_name = venue.get("name") or ""
    venue_address = venue.get("address") or ""
    venue_city = venue.get("city") or ""
    venue_postcode = ", ".join(p for p in [venue_address, venue_city] if p)

    group = node.get("group") or {}
    organiser = group.get("name") or ""

    fee = node.get("feeSettings") or {}
    amount = fee.get("amount")
    currency = fee.get("currency") or ""
    if amount is not None and float(amount) > 0:
        symbol = "£" if currency.upper() == "GBP" else currency + " "
        cost = f"{symbol}{float(amount):.0f}"
    else:
        cost = "Free"

    title = node.get("title") or ""
    description = node.get("description") or ""

    return {
        "name": title,
        "date": event_date,
        "time": event_time,
        "end_date": end_date,
        "venue_name": venue_name,
        "venue_postcode": venue_postcode,
        "area": venue_city or CITY_NAME,
        "organiser": organiser,
        "cost": cost,
        "description": description,
        "format_type": classify_format(title, description),
        "source": "Meetup",
        "url": node.get("eventUrl") or "",
        "_raw_node": node,
    }


def is_in_window(event, start_date, end_date):
    """Check if event date falls within the coverage window."""
    try:
        d = date.fromisoformat(event["date"])
        return start_date <= d <= end_date
    except (ValueError, TypeError):
        return False


# Guard against pagination runaways (audit P2: no max-page or
# cursor-progress check: a repeating cursor meant an infinite loop).
MAX_REC_PAGES = 50


def build_counts(total_new, source_2a, source_2b, group_errors, error_details,
                 rescheduled):
    """Build the .meetup_fetch_counts.json payload.

    error_details carries the REAL exception strings (audit P2: the old
    builder reconstructed fake "urlname: 403" entries by guessing against
    the wrong namespace). ok flips false when more than half the groups
    fail: 23 of 24 groups erroring used to still report ok=true.
    """
    counts = {
        "source": "Meetup",
        "new": total_new,
        "2a": source_2a,
        "2b": source_2b,
        "groups_total": len(MY_MEETUP_GROUPS),
        "group_errors": group_errors,
        "ok": group_errors <= len(MY_MEETUP_GROUPS) / 2,
        "rescheduled": rescheduled,
    }
    if error_details:
        counts["errors"] = list(error_details)
    return counts


def fetch_group_events(urlname):
    """Fetch events for a single group. Returns list of GraphQL event nodes."""
    try:
        data = gql_post(GROUP_EVENTS_QUERY, {"urlname": urlname})
        group_data = (data.get("data") or {}).get("groupByUrlname")
        if not group_data:
            return [], None
        events = group_data.get("events") or {}
        edges = events.get("edges") or []
        return [e["node"] for e in edges if e.get("node")], None
    except Exception as e:
        return [], str(e)


def fetch_recommended_page(start_iso, end_iso, cursor=None):
    """Fetch one page of recommendedEvents. Returns (nodes, has_next, end_cursor)."""
    variables = {
        "filter": {
            "lat": LONDON_LAT,
            "lon": LONDON_LON,
            "startDateRange": start_iso,
            "endDateRange": end_iso,
            "eventType": "PHYSICAL",
        },
        "first": 50,
        "after": cursor,
    }
    data = gql_post(RECOMMENDED_QUERY, variables)
    rec = (data.get("data") or {}).get("recommendedEvents") or {}
    page_info = rec.get("pageInfo") or {}
    edges = rec.get("edges") or []
    nodes = [e["node"] for e in edges if e.get("node")]
    return nodes, page_info.get("hasNextPage", False), page_info.get("endCursor")




def check_rescheduled(event, url_details, conn, dry_run=False):
    """If event URL exists but date changed, update the stored date.
    Resets expired/rejected/vetoed candidates to pending_llm unless they have a
    notion_page_id (prevent_written_state_regression trigger would block that).
    Returns True if rescheduled."""
    url = event.get("url")
    if not url or url not in url_details:
        return False
    stored = url_details[url]
    new_date = event.get("date")
    if not new_date or new_date == stored["date"]:
        return False
    cid = stored["id"]
    old_date = stored["date"]
    print(f"    RESCHEDULED: ID {cid} date {old_date} → {new_date}")
    if not dry_run:
        conn.execute(
            "UPDATE candidates SET date = ?, end_date = ? WHERE id = ?",
            (new_date, event.get("end_date"), cid),
        )
        if stored["pipeline_state"] in ("expired", "llm_rejected", "vetoed"):
            if stored.get("notion_page_id"):
                print(f"    → Skipped state reset (has Notion page, was {stored['pipeline_state']})")
            else:
                conn.execute(
                    "UPDATE candidates SET pipeline_state = 'pending_llm' WHERE id = ?",
                    (cid,),
                )
                print(f"    → Reset to pending_llm (was {stored['pipeline_state']})")
                url_details[url]["pipeline_state"] = "pending_llm"
        conn.commit()
    url_details[url]["date"] = new_date
    return True


def insert_candidates(conn, candidates, run_date, dry_run=False):
    """Insert candidate rows into SQLite. Returns count inserted."""
    if dry_run or not candidates:
        return len(candidates)
    cur = conn.cursor()
    for c in candidates:
        raw_node = c.pop("_raw_node", None)
        snapshot = json.dumps(raw_node, ensure_ascii=False) if raw_node else None
        cur.execute(
            """INSERT INTO candidates
               (run_date, name, date, time, end_date, venue_name, venue_postcode,
                area, organiser, cost, description, format_type, source, url,
                pipeline_state, source_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_llm', ?)""",
            (
                run_date, c["name"], c["date"], c["time"], c.get("end_date"),
                c["venue_name"], c["venue_postcode"], c["area"], c["organiser"],
                c["cost"], c["description"], c["format_type"], c["source"],
                c["url"], snapshot,
            ),
        )
    conn.commit()
    return len(candidates)


def _smoke_tests():
    """Verify _normalise_name handles Unicode edge cases."""
    failures = 0

    def check(label, got, expected):
        nonlocal failures
        if got != expected:
            print(f"FAIL {label}: got {got!r}, expected {expected!r}")
            failures += 1
        else:
            print(f"  ok  {label}")

    print("_normalise_name tests:")
    check("basic lowercase", _normalise_name("Jazz Night"), "jazz night")
    check("strip whitespace", _normalise_name("  Jazz   Night  "), "jazz night")
    check("curly apostrophe", _normalise_name("Gay’s We Chree"), "gay's we chree")
    check("straight apostrophe matches",
          _normalise_name("Gay's We Chree"), _normalise_name("Gay’s We Chree"))
    check("em dash", _normalise_name("Art—Life"), "art-life")
    check("en dash", _normalise_name("6–15 June"), "6-15 june")
    check("NBSP", _normalise_name("Sat\xa0Jun"), "sat jun")
    check("narrow NBSP", _normalise_name("Sat Jun"), "sat jun")
    check("zero-width space stripped", _normalise_name("hello​world"), "helloworld")
    check("curly double quotes", _normalise_name("“Quoted”"), '"quoted"')
    check("NFKD ligature", _normalise_name("ﬁnale"), "finale")
    check("ASCII unchanged", _normalise_name("plain ascii"), "plain ascii")
    check("NFC/NFD equivalence",
          _normalise_name("café"), _normalise_name("café"))

    print("\nGraphQL error surfacing (WP5):")
    check("clean response passes through",
          _raise_on_gql_errors({"data": {"x": 1}}), {"data": {"x": 1}})
    try:
        _raise_on_gql_errors({"errors": [{"message": "rate limited"}], "data": None})
        check("errors with null data raise GraphQLError", "no raise", "GraphQLError")
    except GraphQLError as e:
        check("errors with null data raise GraphQLError",
              "rate limited" in str(e), True)
    partial = {"errors": [{"message": "partial"}], "data": {"x": 1}}
    check("errors alongside data pass through with warning",
          _raise_on_gql_errors(partial), partial)

    print("\nbuild_counts (WP5):")
    clean = build_counts(10, 6, 4, 0, [], 1)
    check("clean run ok=true", clean["ok"], True)
    check("clean run has no errors key", "errors" in clean, False)
    few = build_counts(10, 6, 4, 2, ["Meetup group a: HTTP 403 detail"], 1)
    check("few errors still ok", few["ok"], True)
    check("real error strings carried", few["errors"], ["Meetup group a: HTTP 403 detail"])
    many = build_counts(0, 0, 0, len(MY_MEETUP_GROUPS) - 1, ["e"] * 3, 0)
    check("majority group failure flips ok=false", many["ok"], False)

    if failures:
        print(f"\n{failures} FAILED")
    else:
        print("\nAll passed")
    return failures


def main():
    parser = argparse.ArgumentParser(description="Fetch Meetup events via gql2 API")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB))
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date YYYY-MM-DD (default: today + 6 weeks)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run normalisation smoke tests and exit")
    args = parser.parse_args()

    if args.smoke_test:
        sys.exit(_smoke_tests())

    _run_format_self_test()

    today = date.today()
    start_date = today
    end_date = (
        date.fromisoformat(args.end_date) if args.end_date
        else today + timedelta(weeks=6)
    )
    run_date = today.isoformat()

    tz_offset = datetime.now(ZoneInfo("Europe/London")).strftime("%z")
    tz_formatted = f"{tz_offset[:3]}:{tz_offset[3:]}"
    start_iso = f"{start_date.isoformat()}T00:00:00{tz_formatted}"
    end_iso = f"{end_date.isoformat()}T23:59:59{tz_formatted}"

    print(f"Meetup fetch: {start_date} → {end_date}")
    print(f"Database: {args.db}")
    if args.dry_run:
        print("DRY RUN: no writes to DB\n")

    conn = sqlite3.connect(args.db)
    existing = load_existing(conn)
    url_details = existing.url_details
    print(f"Existing candidates in DB: {len(existing.urls)} URLs, {len(existing.name_dates)} name+date pairs\n")

    # Track all event IDs from Source 2A for dedup in 2B
    seen_event_ids = set()
    all_new_candidates = []
    rescheduled_count = 0
    group_errors = 0
    groups_with_events = 0

    # ------ Source 2A: My groups ------
    print("=" * 50)
    print("SOURCE 2A: Your Meetup groups")
    print("=" * 50)

    error_details = []

    for urlname in MY_MEETUP_GROUPS:
        nodes, err = fetch_group_events(urlname)
        if err:
            group_errors += 1
            error_details.append(f"Meetup group {urlname}: {err[:200]}")
            print(f"  ERROR [{urlname}]: {err}")
            time.sleep(PACE_SECONDS)
            continue

        count = 0
        for node in nodes:
            event_id = node.get("id")
            if event_id:
                seen_event_ids.add(event_id)
            if node.get("eventType") != "PHYSICAL":
                continue
            event = parse_event(node)
            if not is_in_window(event, start_date, end_date):
                continue
            if check_rescheduled(event, url_details, conn, args.dry_run):
                rescheduled_count += 1
                continue
            if is_duplicate(event, existing):
                continue
            all_new_candidates.append(event)
            existing.add(event)  # so 2B dedup sees 2A inserts
            count += 1

        if count > 0:
            groups_with_events += 1
        status = f"{count} new" if nodes else "0 events"
        print(f"  {urlname}: {len(nodes)} events, {status}")
        time.sleep(PACE_SECONDS)

    source_2a_count = len(all_new_candidates)
    print(f"\nSource 2A total: {source_2a_count} new candidates from {groups_with_events} groups")
    if group_errors > 10:
        print(f"  CRITICAL: {group_errors} groups returned errors!")
    elif group_errors > 0:
        print(f"  Note: {group_errors} group(s) returned errors")

    # ------ Source 2B: Recommended events ------
    print(f"\n{'=' * 50}")
    print("SOURCE 2B: Recommended events sweep")
    print("=" * 50)

    page_num = 0
    total_recommended = 0
    new_from_recommended = 0
    cursor = None
    prev_cursor = None

    while True:
        page_num += 1
        if page_num > MAX_REC_PAGES:
            msg = f"Meetup recommended sweep hit the {MAX_REC_PAGES}-page cap: stopping"
            print(f"  WARN: {msg}", file=sys.stderr)
            error_details.append(msg)
            break
        try:
            nodes, has_next, cursor = fetch_recommended_page(start_iso, end_iso, cursor)
        except Exception as e:
            msg = f"Meetup recommended page {page_num}: {str(e)[:200]}"
            print(f"  ERROR: {msg}", file=sys.stderr)
            error_details.append(msg)
            break
        total_recommended += len(nodes)

        page_new = 0
        for node in nodes:
            event_id = node.get("id")
            if event_id and event_id in seen_event_ids:
                continue
            if event_id:
                seen_event_ids.add(event_id)
            if node.get("eventType") != "PHYSICAL":
                continue
            event = parse_event(node)
            if not is_in_window(event, start_date, end_date):
                continue
            if check_rescheduled(event, url_details, conn, args.dry_run):
                rescheduled_count += 1
                continue
            if is_duplicate(event, existing):
                continue
            all_new_candidates.append(event)
            existing.add(event)
            page_new += 1

        new_from_recommended += page_new
        print(f"  Page {page_num}: {len(nodes)} events, {page_new} new")

        if not has_next or not cursor:
            break
        if cursor == prev_cursor:
            msg = f"Meetup recommended sweep cursor made no progress on page {page_num}: stopping"
            print(f"  WARN: {msg}", file=sys.stderr)
            error_details.append(msg)
            break
        prev_cursor = cursor
        time.sleep(PACE_SECONDS)

    print(f"\nSource 2B total: {total_recommended} events across {page_num} pages, {new_from_recommended} new candidates")
    if total_recommended < 100:
        print(f"  WARNING: Only {total_recommended} recommended events returned: API may be having issues")

    # ------ Insert ------
    total_new = len(all_new_candidates)
    inserted = insert_candidates(conn, all_new_candidates, run_date, dry_run=args.dry_run)
    conn.close()

    # ------ Summary ------
    print(f"\n{'=' * 50}")
    print("SUMMARY")
    print("=" * 50)
    print(f"  Groups fetched:        {len(MY_MEETUP_GROUPS)} ({group_errors} errors)")
    print(f"  Recommended pages:     {page_num}")
    print(f"  New from groups (2A):  {source_2a_count}")
    print(f"  New from recs (2B):    {new_from_recommended}")
    print(f"  Rescheduled events:    {rescheduled_count}")
    print(f"  Total new candidates:  {total_new}")
    print(f"  Duplicates skipped:    (checked against {len(existing.urls)} URLs)")
    if args.dry_run:
        print(f"  DRY RUN: would insert: {total_new}")
    else:
        print(f"  Written to DB:         {inserted}")

    counts = build_counts(
        total_new, source_2a_count, new_from_recommended,
        group_errors, error_details, rescheduled_count,
    )
    counts_path = Path(args.db).parent / ".meetup_fetch_counts.json"
    counts_path.write_text(json.dumps(counts, indent=2) + "\n")
    print(f"\n  Counts written to {counts_path}")


if __name__ == "__main__":
    main()
