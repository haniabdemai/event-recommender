#!/usr/bin/env python3
"""
Fetch Luma events via the unauthenticated public discovery API.

Two passes (mirrors the Meetup fetcher):
  Source 3A: paginated listing via api.lu.ma/discover/get-paginated-events
  Source 3B: per-event detail via api.lu.ma/event/get

Writes new offline candidates for your city to SQLite. Dedups by URL, then (name, date).

Usage:
    python3 fetch_luma.py                     # today -> +6 weeks
    python3 fetch_luma.py --end-date 2026-06-30
    python3 fetch_luma.py --dry-run           # no SQLite writes; prints summary
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
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fetch_meetup import classify_format, check_rescheduled, _normalise_name

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.config import CITY_NAME, CITY_TZ, LUMA_PLACE_ID  # noqa: E402

LIST_URL = "https://api.lu.ma/discover/get-paginated-events"
DETAIL_URL = "https://api.lu.ma/event/get"
PAGE_SIZE = 50
PACE_SECONDS = 0.3
# Pagination runaway guard (audit P2: no cap or cursor-progress check)
MAX_LIST_PAGES = 50
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {"Accept": "application/json", "User-Agent": USER_AGENT}

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get_json(url, params):
    """GET a JSON endpoint. Raises HTTPError/URLError on failure."""
    full = f"{url}?{urlencode(params)}"
    req = Request(full, headers=HTTP_HEADERS)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        hdrs = dict(e.headers.items()) if e.headers else {}
        print(f"  DIAG Luma HTTP {e.code}: body={body!r}", file=sys.stderr)
        print(f"  DIAG Luma headers: {hdrs}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# TipTap flatten
# ---------------------------------------------------------------------------

# Block-level node types concatenate inline, then emit a trailing \n\n so
# paragraphs separate cleanly. Container node types (doc, bulletList,
# orderedList) just recurse: their child blocks already emit their own breaks.
_BLOCK_TYPES = {
    "paragraph", "heading", "listItem", "blockquote", "codeBlock",
}


def flatten_tiptap(node):
    """Recursively flatten a TipTap JSON node tree to plain text.

    Inline text within a block concatenates flat; blocks are separated by \\n\\n.
    Unknown node types are traversed transparently: we never drop content.
    """
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(flatten_tiptap(n) for n in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"

    content = node.get("content") or []

    if node_type in _BLOCK_TYPES:
        inner = "".join(flatten_tiptap(c) for c in content)
        return (inner + "\n\n") if inner else ""

    return "".join(flatten_tiptap(c) for c in content)


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


def _parse_start(start_at_utc):
    """Convert ISO UTC (e.g. '2026-04-18T09:00:00.000Z') to local date, time."""
    return iso_to_london(start_at_utc)


def _parse_venue(geo_info):
    """Return (venue_name, venue_postcode, area) from event.geo_address_info.

    When mode == 'shown', pull from localized.en-GB (or any available locale);
    when obfuscated, keep name/postcode None and use city_state as area.
    """
    if not isinstance(geo_info, dict):
        return "", "", CITY_NAME

    area = geo_info.get("city_state") or geo_info.get("city") or CITY_NAME
    if geo_info.get("mode") != "shown":
        return "", "", area

    localized = geo_info.get("localized") or {}
    loc = localized.get("en-GB")
    if not loc and localized:
        loc = localized[next(iter(localized))]
    loc = loc or {}

    full_address = loc.get("full_address") or ""
    venue_name = geo_info.get("address") or (full_address.split(",")[0] if full_address else "")
    return venue_name, full_address, area


def _price_value(field):
    """Return (cents_or_None, currency_upper_or_None) from a ticket price field.

    Luma sends price as either None or a dict {cents, currency, is_flexible}.
    """
    if field is None:
        return None, None
    if isinstance(field, dict):
        return field.get("cents"), (field.get("currency") or "GBP").upper()
    if isinstance(field, (int, float)):
        return int(field), "GBP"
    return None, None


def _parse_cost(ticket_info):
    """Return a cost string:
    'Free' if is_free; price or range when set; 'Unknown' otherwise.
    """
    if not isinstance(ticket_info, dict):
        return "Unknown"
    if ticket_info.get("is_free"):
        return "Free"
    cents_lo, cur_lo = _price_value(ticket_info.get("price"))
    cents_hi, cur_hi = _price_value(ticket_info.get("max_price"))
    if cents_lo is None and cents_hi is None:
        return "Unknown"
    currency = cur_lo or cur_hi or "GBP"
    symbol = "£" if currency == "GBP" else f"{currency} "
    def _fmt(cents):
        return f"{symbol}{float(cents) / 100:.0f}"
    if cents_lo is not None and cents_hi is not None and cents_hi != cents_lo:
        return f"{_fmt(cents_lo)}–{_fmt(cents_hi)}"
    return _fmt(cents_lo if cents_lo is not None else cents_hi)


from erlib.dates import iso_to_london  # noqa: E402
from erlib.dedup import is_duplicate, load_existing  # noqa: E402
from erlib.normalise import (  # noqa: E402
    normalise_luma_url as _normalise_luma_url,
    slug_from_luma_url as _slug_from_luma_url,
)


def _parse_url(event_slug):
    return f"https://lu.ma/{event_slug}" if event_slug else ""


def parse_event(detail):
    """Map a /event/get response to the candidate dict expected by SQLite.

    Note: description_mirror, hosts, ticket_info live at the TOP LEVEL of the
    detail response, not under `event`. The event sub-object holds name,
    start_at, geo_address_info, url.
    """
    event = detail.get("event") or {}
    name = event.get("name") or ""
    event_date, event_time = _parse_start(event.get("start_at") or "")
    end_date_str, _ = _parse_start(event.get("end_at") or "")
    end_date = end_date_str if (end_date_str and end_date_str != event_date) else None
    venue_name, venue_postcode, area = _parse_venue(event.get("geo_address_info"))

    hosts = detail.get("hosts") or []
    organiser = (hosts[0].get("name") if hosts and hosts[0] else "") or ""

    cost = _parse_cost(detail.get("ticket_info"))
    description = flatten_tiptap(detail.get("description_mirror")).strip()

    return {
        "name": name,
        "date": event_date,
        "time": event_time,
        "end_date": end_date,
        "venue_name": venue_name,
        "venue_postcode": venue_postcode,
        "area": area,
        "organiser": organiser,
        "cost": cost,
        "description": description,
        "format_type": classify_format(name, description),
        "source": "Luma",
        "url": _parse_url(event.get("url")),
        "_raw_node": detail,
    }


# ---------------------------------------------------------------------------
# Listing + detail fetch
# ---------------------------------------------------------------------------


def fetch_listing(end_date, verbose=True):
    """Paginate through discover listings. Return (offline_api_ids, stats).

    Stops when has_more is false or the last entry's start_at passes end_date.
    """
    stats = {"pages": 0, "entries_seen": 0, "offline": 0, "skipped_online": 0}
    offline_ids = []
    cursor = None
    prev_cursor = None

    while True:
        # Pagination runaway guard (audit P2): cap pages and stop when the
        # cursor makes no progress: a repeating cursor looped forever.
        if stats["pages"] >= MAX_LIST_PAGES:
            if verbose:
                print(f"  WARN: listing hit the {MAX_LIST_PAGES}-page cap: stopping", file=sys.stderr)
            break
        params = {
            "discover_place_api_id": LUMA_PLACE_ID,
            "pagination_limit": PAGE_SIZE,
        }
        if cursor:
            params["pagination_cursor"] = cursor

        data = http_get_json(LIST_URL, params)
        entries = data.get("entries") or []
        stats["pages"] += 1
        stats["entries_seen"] += len(entries)

        last_start = None
        for e in entries:
            ev = e.get("event") or {}
            start = ev.get("start_at")
            if start:
                last_start = start
            if ev.get("location_type") == "offline":
                if ev.get("api_id"):
                    offline_ids.append(ev["api_id"])
                    stats["offline"] += 1
            else:
                stats["skipped_online"] += 1

        if verbose:
            print(f"  Page {stats['pages']}: {len(entries)} entries, "
                  f"{stats['offline']} offline total")

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
        if cursor == prev_cursor:
            if verbose:
                print(f"  WARN: listing cursor made no progress on page {stats['pages']}: stopping", file=sys.stderr)
            break
        prev_cursor = cursor

        # Early stop if the page we just loaded is entirely past end_date
        if last_start:
            try:
                last_dt = datetime.fromisoformat(last_start.replace("Z", "+00:00"))
                if last_dt.astimezone(CITY_TZ).date() > end_date:
                    break
            except (ValueError, TypeError):
                pass

        time.sleep(PACE_SECONDS)

    return offline_ids, stats


def fetch_detail(api_id):
    """Fetch one event detail. Returns (candidate_dict, error_string_or_None)."""
    try:
        data = http_get_json(DETAIL_URL, {"event_api_id": api_id})
        return parse_event(data), None
    except (HTTPError, URLError) as e:
        return None, f"{type(e).__name__}: {e}"
    except Exception as e:  # JSON decode, etc.
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def is_in_window(event, start_date, end_date):
    try:
        d = date.fromisoformat(event["date"])
        return start_date <= d <= end_date
    except (ValueError, TypeError):
        return False






def insert_candidates(conn, candidates, run_date, dry_run=False):
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



def update_candidate(conn, candidate_id, enriched, dry_run=False):
    """UPDATE an existing candidate with fields from the Luma API.

    Never touches: source, source_snapshot, run_date, pipeline_state,
    notion_page_id, or any scoring/LLM/travel columns.
    """
    if dry_run:
        return
    _ENRICHABLE = [
        "description", "venue_name", "venue_postcode", "area",
        "organiser", "cost", "time", "format_type", "end_date",
    ]
    updates = {f: enriched[f] for f in _ENRICHABLE if enriched.get(f) is not None}
    if not updates:
        return
    updates["needs_enrichment"] = 0
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE candidates SET {set_clause} WHERE id = ?",
        list(updates.values()) + [candidate_id],
    )
    conn.commit()


def enrich_newsletter_luma_candidates(conn, dry_run=False, verbose=True,
                                      fetcher=None):
    """Fetch full detail for newsletter candidates with a lu.ma URL but no
    description. Updates existing rows in-place. Never inserts new rows.

    fetcher: callable(slug) -> (candidate_dict, error_or_None). Defaults to
    fetch_detail. Injectable for testing.
    """
    if fetcher is None:
        fetcher = fetch_detail
    cur = conn.cursor()
    cur.execute("""
        SELECT id, url, name FROM candidates
        WHERE needs_enrichment = 1
          AND pipeline_state = 'pending_llm'
    """)
    targets = [(r[0], r[1], r[2]) for r in cur.fetchall()]

    if verbose:
        print(f"\n{'=' * 50}")
        print("NEWSLETTER ENRICHMENT: Luma candidates needing API data")
        print("=" * 50)
        print(f"  Candidates flagged: {len(targets)}")

    if not targets:
        if verbose:
            print("  Nothing to enrich.")
        return 0, 0, 0

    enriched = 0
    skipped = 0
    errors = 0

    for idx, (row_id, url, name) in enumerate(targets, 1):
        slug = _slug_from_luma_url(url)
        if not slug:
            if verbose:
                print(f"  [{idx}/{len(targets)}] {name!r}: not a valid Luma slug")
            skipped += 1
            continue

        if verbose:
            print(f"  [{idx}/{len(targets)}] {name!r} (slug={slug})")

        cand, err = fetcher(slug)
        if err:
            print(f"    ERROR: {err}: leaving candidate as-is", file=sys.stderr)
            errors += 1
            time.sleep(PACE_SECONDS)
            continue

        if not cand.get("description"):
            if verbose:
                print("    API returned no description: clearing flag")
            conn.execute("UPDATE candidates SET needs_enrichment = 0 WHERE id = ?", (row_id,))
            conn.commit()
            skipped += 1
            time.sleep(PACE_SECONDS)
            continue

        update_candidate(conn, row_id, cand, dry_run=dry_run)
        if verbose:
            preview = (cand["description"] or "")[:80].replace("\n", " ")
            dry_tag = "[DRY RUN]" if dry_run else "Updated"
            print(f"    {dry_tag}: {preview!r}...")
        enriched += 1
        time.sleep(PACE_SECONDS)

    if verbose:
        print(f"\n  Enriched: {enriched}  Skipped: {skipped}  Errors: {errors}")

    return enriched, skipped, errors


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def _create_test_db():
    """Create an in-memory SQLite DB with the candidates schema for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT NOT NULL,
        name TEXT NOT NULL,
        date TEXT NOT NULL,
        time TEXT,
        venue_name TEXT,
        venue_postcode TEXT,
        area TEXT,
        organiser TEXT,
        cost TEXT,
        description TEXT,
        format_type TEXT,
        source TEXT,
        url TEXT,
        score INTEGER,
        tier TEXT,
        signals_fired TEXT,
        pipeline_state TEXT DEFAULT 'pending_llm',
        source_snapshot TEXT,
        needs_enrichment INTEGER DEFAULT 0,
        end_date TEXT,
        notion_page_id TEXT,
        travel_display TEXT,
        llm_tier TEXT,
        llm_reasoning TEXT,
        llm_reviewed TEXT
    )""")
    return conn


def _insert_test_candidate(conn, **overrides):
    """Insert a test candidate, returning the row id."""
    defaults = {
        "run_date": "2026-05-19", "name": "Test Event", "date": "2026-06-01",
        "time": None, "end_date": None, "venue_name": None, "venue_postcode": None,
        "area": None, "organiser": None, "cost": None, "description": None,
        "format_type": None, "source": "Newsletter", "url": None,
        "pipeline_state": "pending_llm", "source_snapshot": "abc123",
        "needs_enrichment": 0,
    }
    defaults.update(overrides)
    cur = conn.execute(
        """INSERT INTO candidates
           (run_date, name, date, time, end_date, venue_name, venue_postcode,
            area, organiser, cost, description, format_type, source, url,
            pipeline_state, source_snapshot, needs_enrichment)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(defaults[k] for k in [
            "run_date", "name", "date", "time", "end_date", "venue_name",
            "venue_postcode", "area", "organiser", "cost", "description",
            "format_type", "source", "url", "pipeline_state", "source_snapshot",
            "needs_enrichment",
        ]),
    )
    conn.commit()
    return cur.lastrowid


def _smoke_tests():
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    # === URL NORMALISATION ===
    print("\n--- URL normalisation ---")
    check("strip tracking params from luma.com",
          _normalise_luma_url("https://luma.com/hjk9nc4i?lm_api_id=abc&lm_medium=email"),
          "https://lu.ma/hjk9nc4i")
    check("strip tracking params from lu.ma",
          _normalise_luma_url("https://lu.ma/hjk9nc4i?t=123"),
          "https://lu.ma/hjk9nc4i")
    check("clean luma.com URL",
          _normalise_luma_url("https://luma.com/hjk9nc4i"),
          "https://lu.ma/hjk9nc4i")
    check("clean lu.ma URL unchanged",
          _normalise_luma_url("https://lu.ma/hjk9nc4i"),
          "https://lu.ma/hjk9nc4i")
    check("non-luma URL unchanged",
          _normalise_luma_url("https://meetup.com/event/123"),
          "https://meetup.com/event/123")
    check("None stays None",
          _normalise_luma_url(None), None)
    check("empty stays empty",
          _normalise_luma_url(""), "")

    # === SLUG EXTRACTION ===
    print("\n--- Slug extraction ---")
    check("slug from luma.com with params",
          _slug_from_luma_url("https://luma.com/hjk9nc4i?lm_api_id=abc"),
          "hjk9nc4i")
    check("slug from clean lu.ma",
          _slug_from_luma_url("https://lu.ma/bvvrlgam"),
          "bvvrlgam")
    check("no slug from non-luma",
          _slug_from_luma_url("https://meetup.com/event/123"),
          None)
    check("no slug from None",
          _slug_from_luma_url(None), None)
    check("no slug from path with slash",
          _slug_from_luma_url("https://lu.ma/r/some-calendar"),
          None)
    check("no slug from empty path",
          _slug_from_luma_url("https://lu.ma/"),
          None)

    # === DEDUP: newsletter tracking URL matches API URL ===
    print("\n--- Dedup: newsletter URL matches API URL after normalisation ---")
    conn = _create_test_db()
    _insert_test_candidate(conn,
        name="London Fintech Breakfast",
        date="2026-05-20",
        url="https://luma.com/hjk9nc4i?lm_api_id=discplace-QCcNk3HXowOR97j&lm_medium=email",
        source="Newsletter",
        needs_enrichment=1,
    )
    existing = load_existing(conn)
    api_event = {
        "name": "London Fintech Breakfast",
        "date": "2026-05-20",
        "url": "https://lu.ma/hjk9nc4i",
    }
    check("API event detected as duplicate of newsletter row",
          is_duplicate(api_event, existing),
          True)

    conn2 = _create_test_db()
    _insert_test_candidate(conn2,
        name="Test Event", date="2026-06-01",
        url="https://luma.com/abc123", source="Newsletter",
    )
    existing2 = load_existing(conn2)
    check("clean luma.com dedupes against lu.ma",
          is_duplicate({"name": "Other Name", "date": "2026-07-01",
                        "url": "https://lu.ma/abc123"}, existing2),
          True)
    check("different slug does NOT match",
          is_duplicate({"name": "Unrelated", "date": "2026-08-01",
                        "url": "https://lu.ma/zzz999"}, existing2),
          False)

    # === ENRICHMENT: updates correct fields, preserves protected fields ===
    print("\n--- Enrichment: field updates ---")
    conn3 = _create_test_db()
    row_id = _insert_test_candidate(conn3,
        name="Builders & Brews London",
        date="2026-05-22",
        url="https://luma.com/1rna5bdp?lm_api_id=abc",
        source="Newsletter",
        source_snapshot="19e36f2259b810fd",
        needs_enrichment=1,
        description=None,
        venue_name="Nexus Hub",
    )

    def mock_fetcher(slug):
        if slug == "1rna5bdp":
            return {
                "name": "Builders & Brews London", "date": "2026-05-22",
                "time": "09:00", "venue_name": "Nexus Hub",
                "venue_postcode": "41 Pitfield St, London N1 6DA, UK",
                "area": "London, United Kingdom", "organiser": "Nexus Club",
                "cost": "Free", "description": "A weekly co-working session for builders.",
                "format_type": "Social meetup", "source": "Luma",
                "url": "https://lu.ma/1rna5bdp",
            }, None
        return None, "Not found"

    enriched, skipped, errs = enrich_newsletter_luma_candidates(
        conn3, dry_run=False, verbose=False, fetcher=mock_fetcher)

    check("enriched count", enriched, 1)
    check("skipped count", skipped, 0)
    check("error count", errs, 0)

    row = conn3.execute("SELECT * FROM candidates WHERE id = ?", (row_id,)).fetchone()
    check("description filled",
          row["description"], "A weekly co-working session for builders.")
    check("organiser filled", row["organiser"], "Nexus Club")
    check("cost filled", row["cost"], "Free")
    check("source NOT changed", row["source"], "Newsletter")
    check("source_snapshot NOT changed", row["source_snapshot"], "19e36f2259b810fd")
    check("pipeline_state NOT changed", row["pipeline_state"], "pending_llm")
    check("needs_enrichment cleared", row["needs_enrichment"], 0)

    # === ENRICHMENT: only processes flagged candidates ===
    print("\n--- Enrichment: only flagged candidates ---")
    conn4 = _create_test_db()
    _insert_test_candidate(conn4, name="Unflagged Event",
        url="https://luma.com/xyz789", source="Newsletter",
        needs_enrichment=0, description=None)
    _insert_test_candidate(conn4, name="Flagged Event",
        url="https://luma.com/abc456", source="Newsletter",
        needs_enrichment=1, description=None)

    calls = []
    def tracking_fetcher(slug):
        calls.append(slug)
        return {"description": "Enriched desc", "venue_name": "", "venue_postcode": "",
                "area": "", "organiser": "", "cost": "", "time": "",
                "format_type": ""}, None

    enriched4, _, _ = enrich_newsletter_luma_candidates(
        conn4, verbose=False, fetcher=tracking_fetcher)
    check("only flagged candidate enriched", enriched4, 1)
    check("fetcher called with correct slug", calls, ["abc456"])

    # === ENRICHMENT: soft failure on API error ===
    print("\n--- Enrichment: soft failure ---")
    conn5 = _create_test_db()
    _insert_test_candidate(conn5, name="Error Event",
        url="https://luma.com/err123", source="Newsletter", needs_enrichment=1)

    def failing_fetcher(slug):
        return None, "HTTPError: 404"

    e5, s5, err5 = enrich_newsletter_luma_candidates(
        conn5, verbose=False, fetcher=failing_fetcher)
    check("error count on API failure", err5, 1)
    check("enriched count on failure", e5, 0)
    row5 = conn5.execute(
        "SELECT needs_enrichment FROM candidates WHERE name='Error Event'").fetchone()
    check("needs_enrichment stays 1 on error (retryable)", row5[0], 1)

    # === ENRICHMENT: API returns no description ===
    print("\n--- Enrichment: API has no description ---")
    conn6 = _create_test_db()
    _insert_test_candidate(conn6, name="No Desc API Event",
        url="https://luma.com/nodesc1", source="Newsletter", needs_enrichment=1)

    def empty_desc_fetcher(slug):
        return {"description": "", "venue_name": "Some Venue", "venue_postcode": "",
                "area": "", "organiser": "", "cost": "", "time": "",
                "format_type": ""}, None

    e6, s6, _ = enrich_newsletter_luma_candidates(
        conn6, verbose=False, fetcher=empty_desc_fetcher)
    check("skipped when API has no description", s6, 1)
    check("not enriched when API has no description", e6, 0)
    row6 = conn6.execute(
        "SELECT needs_enrichment FROM candidates WHERE name='No Desc API Event'").fetchone()
    check("needs_enrichment cleared even with no description", row6[0], 0)

    # === FULL FLOW: enrich then dedup prevents duplicate insertion ===
    print("\n--- Full flow: enrich then dedup prevents duplicate ---")
    conn7 = _create_test_db()
    _insert_test_candidate(conn7,
        name="Matchmaking for Friends", date="2026-05-28",
        url="https://luma.com/i4er88mx?lm_api_id=discplace-QCcNk3HXowOR97j",
        source="Newsletter", source_snapshot="19e36f2259b810fd",
        needs_enrichment=1, description=None)

    def flow_fetcher(slug):
        if slug == "i4er88mx":
            return {"description": "Find your next best friend.",
                    "venue_name": "sevente", "venue_postcode": "283 Hackney Rd",
                    "area": "London", "organiser": "Matchmaking Co",
                    "cost": "£10", "time": "18:00", "format_type": "Social meetup",
                    "source": "Luma", "url": "https://lu.ma/i4er88mx"}, None
        return None, "Not found"

    enrich_newsletter_luma_candidates(conn7, verbose=False, fetcher=flow_fetcher)

    existing7 = load_existing(conn7)
    check("after enrichment, API event detected as duplicate",
          is_duplicate({"name": "Matchmaking for Friends", "date": "2026-05-28",
                        "url": "https://lu.ma/i4er88mx"}, existing7),
          True)
    row7 = conn7.execute(
        "SELECT * FROM candidates WHERE name='Matchmaking for Friends'").fetchone()
    check("description enriched in full flow",
          row7["description"], "Find your next best friend.")
    check("source still Newsletter", row7["source"], "Newsletter")

    # === DRY RUN: no DB changes ===
    print("\n--- Dry run ---")
    conn8 = _create_test_db()
    row8_id = _insert_test_candidate(conn8, name="Dry Run Event",
        url="https://luma.com/dry123", source="Newsletter",
        needs_enrichment=1, description=None)
    enrich_newsletter_luma_candidates(conn8, dry_run=True, verbose=False,
                                      fetcher=tracking_fetcher)
    row8 = conn8.execute(
        "SELECT description, needs_enrichment FROM candidates WHERE id=?",
        (row8_id,)).fetchone()
    check("dry run: description unchanged", row8[0], None)
    check("dry run: needs_enrichment unchanged", row8[1], 1)

    # === END_DATE PARSING ===
    print("\n--- end_date parsing ---")
    multi_day = parse_event({
        "event": {
            "name": "Summer Exhibition",
            "start_at": "2026-06-20T09:00:00.000Z",
            "end_at": "2026-07-15T18:00:00.000Z",
            "url": "summer-ex",
        },
    })
    check("multi-day: end_date set", multi_day["end_date"], "2026-07-15")
    check("multi-day: date unchanged", multi_day["date"], "2026-06-20")

    same_day = parse_event({
        "event": {
            "name": "Evening Meetup",
            "start_at": "2026-06-20T17:00:00.000Z",
            "end_at": "2026-06-20T20:00:00.000Z",
            "url": "evening",
        },
    })
    check("same-day: end_date is None", same_day["end_date"], None)

    no_end = parse_event({
        "event": {
            "name": "No End Time",
            "start_at": "2026-06-20T10:00:00.000Z",
            "url": "no-end",
        },
    })
    check("no end_at: end_date is None", no_end["end_date"], None)

    null_end = parse_event({
        "event": {
            "name": "Null End",
            "start_at": "2026-06-20T10:00:00.000Z",
            "end_at": None,
            "url": "null-end",
        },
    })
    check("null end_at: end_date is None", null_end["end_date"], None)

    # === END_DATE IN INSERT ===
    print("\n--- end_date in insert ---")
    conn_ed = _create_test_db()
    insert_candidates(conn_ed, [multi_day], "2026-06-01")
    row_ed = conn_ed.execute(
        "SELECT end_date FROM candidates WHERE name='Summer Exhibition'"
    ).fetchone()
    check("multi-day inserted with end_date", row_ed[0], "2026-07-15")

    insert_candidates(conn_ed, [same_day], "2026-06-01")
    row_sd = conn_ed.execute(
        "SELECT end_date FROM candidates WHERE name='Evening Meetup'"
    ).fetchone()
    check("same-day inserted with NULL end_date", row_sd[0], None)

    # === END_DATE IN ENRICHMENT ===
    print("\n--- end_date in enrichment ---")
    conn_enr = _create_test_db()
    enr_id = _insert_test_candidate(conn_enr, name="Exhibition To Enrich",
        url="https://luma.com/enrich-ex", source="Newsletter",
        needs_enrichment=1, description=None)
    def enrich_fetcher(slug):
        return {
            "name": "Exhibition To Enrich", "date": "2026-06-20", "time": "10:00",
            "end_date": "2026-08-01", "venue_name": "Riverside Gallery",
            "venue_postcode": "SE1 8XZ", "area": "London", "organiser": "Riverside Gallery",
            "cost": "Free", "description": "A major exhibition",
            "format_type": "Exhibition", "source": "Luma",
            "url": "https://lu.ma/enrich-ex",
        }, None
    enrich_newsletter_luma_candidates(conn_enr, verbose=False, fetcher=enrich_fetcher)
    row_enr = conn_enr.execute(
        "SELECT end_date, description FROM candidates WHERE id=?", (enr_id,)
    ).fetchone()
    check("enrichment sets end_date", row_enr[0], "2026-08-01")
    check("enrichment sets description", row_enr[1], "A major exhibition")

    # === NAME NORMALISATION (Unicode) ===
    print("\n--- Name normalisation (Unicode) ---")
    check("basic lowercase", _normalise_name("Jazz Night"), "jazz night")
    check("collapse whitespace", _normalise_name("  Jazz   Night  "), "jazz night")
    check("curly apostrophe", _normalise_name("Gay’s We Chree"), "gay's we chree")
    check("curly matches straight",
          _normalise_name("Gay’s We Chree"), _normalise_name("Gay's We Chree"))
    check("em dash", _normalise_name("Art—Life"), "art-life")
    check("en dash", _normalise_name("6–15 June"), "6-15 june")
    check("NBSP", _normalise_name("Sat\xa0Jun"), "sat jun")
    check("narrow NBSP", _normalise_name("Sat Jun"), "sat jun")
    check("zero-width space", _normalise_name("hello​world"), "helloworld")
    check("curly double quotes", _normalise_name("“Quoted”"), '"quoted"')
    check("NFKD ligature", _normalise_name("ﬁnale"), "finale")
    check("ASCII unchanged", _normalise_name("plain ascii"), "plain ascii")
    check("NFC/NFD equivalence",
          _normalise_name("café"), _normalise_name("café"))

    # === PAGINATION RUNAWAY GUARD (WP5) ===
    print("\n--- pagination runaway guard ---")
    calls = {"n": 0}

    def _stuck_listing(url, params):
        calls["n"] += 1
        return {"entries": [], "has_more": True, "next_cursor": "same-cursor"}

    real_get, real_sleep = globals()["http_get_json"], time.sleep
    globals()["http_get_json"] = _stuck_listing
    time.sleep = lambda s: None
    try:
        _, stats = fetch_listing(date.today(), verbose=False)
        check("no-progress cursor stops after 2 pages", stats["pages"], 2)
        check("stuck listing did not loop", calls["n"] <= 2, True)

        calls["n"] = 0

        def _endless_listing(url, params):
            calls["n"] += 1
            return {"entries": [], "has_more": True, "next_cursor": f"c{calls['n']}"}

        globals()["http_get_json"] = _endless_listing
        _, stats = fetch_listing(date.today(), verbose=False)
        check("page cap stops an endless feed", stats["pages"], MAX_LIST_PAGES)
    finally:
        globals()["http_get_json"] = real_get
        time.sleep = real_sleep

    print(f"\n{'=' * 50}")
    print(f"{'ALL PASSED' if ok else 'SOME TESTS FAILED'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Fetch Luma events via public API")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB))
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date YYYY-MM-DD (default: today + 6 weeks)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not write to SQLite; print summary only")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run offline unit tests (no network, no DB file)")
    args = parser.parse_args()

    if args.smoke_test:
        sys.exit(_smoke_tests())

    today = date.today()
    end_date = (
        date.fromisoformat(args.end_date) if args.end_date
        else today + timedelta(weeks=6)
    )
    run_date = today.isoformat()

    print(f"Luma fetch: {today} → {end_date}")
    print(f"Database: {args.db}")
    if args.dry_run:
        print("DRY RUN: no writes to DB")
    print()

    started = time.monotonic()

    # ---- Source 3A: listing ----
    print("=" * 50)
    print("SOURCE 3A: Discovery listing")
    print("=" * 50)
    try:
        offline_ids, list_stats = fetch_listing(end_date)
    except (HTTPError, URLError) as e:
        msg = f"Luma listing failed: {type(e).__name__}: {e}"
        print(f"  ERROR: {msg}", file=sys.stderr)
        _emit_counts(0, 0, 0, 0, 0, error=msg, db_path=args.db)
        sys.exit(1)

    print(f"\nListing total: {list_stats['pages']} pages, "
          f"{list_stats['entries_seen']} entries seen, "
          f"{list_stats['offline']} offline, "
          f"{list_stats['skipped_online']} online skipped")

    if not offline_ids:
        print("WARNING: zero offline events in listing: flagging as empty result.")
        _emit_counts(list_stats["offline"], 0, 0, 0, 0,
                     error="Listing returned zero offline events", db_path=args.db)
        # This is a soft failure: the shell may still decide to continue.
        sys.exit(0)

    # ---- Source 3B: detail ----
    print(f"\n{'=' * 50}")
    print("SOURCE 3B: Per-event detail")
    print("=" * 50)

    conn = sqlite3.connect(args.db)

    # ---- Newsletter enrichment (before dedup sets are built) ----
    enrich_newsletter_luma_candidates(conn, dry_run=args.dry_run)

    existing = load_existing(conn)
    url_details = existing.url_details
    print(f"Existing candidates in DB: {len(existing.urls)} URLs, "
          f"{len(existing.name_dates)} name+date pairs\n")

    detail_ok = 0
    detail_errors = []
    candidates = []
    out_of_window = 0
    duplicates = 0
    no_date = 0
    rescheduled_count = 0

    for idx, api_id in enumerate(offline_ids, 1):
        cand, err = fetch_detail(api_id)
        if err:
            detail_errors.append((api_id, err))
            print(f"  [{idx}/{len(offline_ids)}] {api_id}: ERROR {err}")
            time.sleep(PACE_SECONDS)
            continue
        detail_ok += 1

        if not cand["date"]:
            no_date += 1
            time.sleep(PACE_SECONDS)
            continue
        if not is_in_window(cand, today, end_date):
            out_of_window += 1
            time.sleep(PACE_SECONDS)
            continue
        norm_cand = {**cand, "url": _normalise_luma_url(cand["url"])} if cand.get("url") else cand
        if check_rescheduled(norm_cand, url_details, conn, args.dry_run):
            rescheduled_count += 1
            time.sleep(PACE_SECONDS)
            continue
        if is_duplicate(cand, existing):
            duplicates += 1
            time.sleep(PACE_SECONDS)
            continue

        candidates.append(cand)
        existing.add(cand)
        time.sleep(PACE_SECONDS)

    # ---- Insert ----
    inserted = insert_candidates(conn, candidates, run_date, dry_run=args.dry_run)
    conn.close()

    elapsed = time.monotonic() - started

    # ---- Summary ----
    with_desc = sum(1 for c in candidates if c["description"])
    print(f"\n{'=' * 50}")
    print("SUMMARY")
    print("=" * 50)
    print(f"  Listed (offline):      {list_stats['offline']}")
    print(f"  Detail OK:             {detail_ok}")
    print(f"  Detail errors:         {len(detail_errors)}")
    print(f"  Out of window:         {out_of_window}")
    print(f"  No date:               {no_date}")
    print(f"  Rescheduled events:    {rescheduled_count}")
    print(f"  Duplicates skipped:    {duplicates}")
    print(f"  New candidates:        {len(candidates)}")
    print(f"  With description:      {with_desc}")
    print(f"  Elapsed:               {elapsed:.1f}s")
    if args.dry_run:
        print(f"  DRY RUN: would insert: {len(candidates)}")
    else:
        print(f"  Written to DB:         {inserted}")

    if detail_errors:
        print("\n  First 5 detail errors:")
        for api_id, err in detail_errors[:5]:
            print(f"    {api_id}: {err}")

    # Emit a parseable one-liner for weekly_run.sh to pick up.
    _emit_counts(
        listed=list_stats["offline"],
        detail_ok=detail_ok,
        detail_errors=len(detail_errors),
        new=len(candidates),
        with_description=with_desc,
        rescheduled=rescheduled_count,
        db_path=args.db,
    )


def _emit_counts(listed, detail_ok, detail_errors, new, with_description, rescheduled=0, error=None, db_path=None):
    """Print a machine-readable summary line and write counts file."""
    payload = {
        "source": "Luma",
        "listed": listed,
        "detail_ok": detail_ok,
        "detail_errors": detail_errors,
        "new": new,
        "with_description": with_description,
        "rescheduled": rescheduled,
        "ok": error is None,
    }
    if error:
        payload["error"] = error
    print(f"LUMA_COUNTS: {json.dumps(payload)}")
    counts_dir = Path(db_path).parent if db_path else SCRIPT_DIR
    counts_path = counts_dir / ".luma_fetch_counts.json"
    counts_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  Counts written to {counts_path}")


if __name__ == "__main__":
    main()
