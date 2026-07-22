#!/usr/bin/env python3
"""
Verify upcoming event dates against source APIs and fix drift.

Catches organiser reschedules that the weekly fetch missed: events that
dropped out of the recommendation feed between runs have no other way
to get their dates corrected.

Updates SQLite and (when NOTION_TOKEN is set) Notion page Date/Time/Month
properties.  Google Calendar picks up the corrected data on the next
sync_to_gcal.py run.

Usage:
    python3 verify_event_dates.py [--dry-run] [--smoke-test]
    NOTION_TOKEN=... python3 verify_event_dates.py   # also patches Notion
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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fetch_meetup import gql_post
from fetch_luma import _slug_from_luma_url, DETAIL_URL as LUMA_DETAIL_URL, HTTP_HEADERS as LUMA_HEADERS
from write_notion import date_property_value, month_name, time_of_day

from erlib.config import DB_PATH
from erlib.dates import end_date_if_different, iso_to_london
from erlib.notion import NotionClient, NotionError

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)

BATCH_PAUSE_SEC = 0.35

# ── Meetup GraphQL ───────────────────────────────────────────────────────────

EVENT_QUERY = """
query($eventId: ID!) {
  event(id: $eventId) {
    id title dateTime endTime eventUrl
  }
}
"""

MEETUP_EVENT_RE = re.compile(r"meetup\.com/[^/]+/events/(\d+)")


# ── Source API queries ───────────────────────────────────────────────────────

def meetup_event_by_id(event_id: str) -> dict | None:
    """Query Meetup GraphQL for a single event. Returns node dict or None."""
    try:
        data = gql_post(EVENT_QUERY, {"eventId": event_id})
        return (data.get("data") or {}).get("event")
    except Exception as e:
        print(f"    Meetup API error: {e}", file=sys.stderr)
        return None


def luma_event_by_slug(slug: str) -> dict | None:
    """Query Luma API for a single event by slug. Returns response dict or None."""
    url = f"{LUMA_DETAIL_URL}?{urllib.parse.urlencode({'event_api_id': slug})}"
    req = urllib.request.Request(url, headers=LUMA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"    Luma API error: {e}", file=sys.stderr)
        return None


# ── URL parsing ──────────────────────────────────────────────────────────────

def extract_meetup_id(url: str) -> str | None:
    m = MEETUP_EVENT_RE.search(url or "")
    return m.group(1) if m else None


# ── Date/time parsing ────────────────────────────────────────────────────────

def parse_iso_datetime(dt_str: str) -> tuple[str | None, str | None]:
    """Parse an ISO datetime string to (YYYY-MM-DD, HH:MM) in the configured city timezone."""
    d, t = iso_to_london(dt_str)
    return (d or None, t or None)


def parse_end_date(end_str: str | None, start_date: str) -> str | None:
    """Parse endTime to an end_date, returning None if same day as start."""
    end_d, _ = iso_to_london(end_str)
    return end_d if end_d and end_d != start_date else None


def parse_luma_event(data: dict) -> tuple[str | None, str | None, str | None]:
    """Extract (date, time, end_date) from a Luma /event/get response.

    Always converts to the configured city timezone (erlib.config.CITY_TZ,
    via erlib.dates) to match fetch_luma._parse_start, which stores
    dates/times in local time regardless of the event's timezone field.
    """
    event = (data.get("event") or {})
    start = event.get("start_at") or ""
    end = event.get("end_at") or ""
    if not start:
        return None, None, None
    start_date, start_time = iso_to_london(start)
    if not start_date:
        return None, None, None
    return start_date, start_time, end_date_if_different(start, end)


# ── Notion update ────────────────────────────────────────────────────────────

def update_notion_page(page_id: str, new_date: str, new_time: str | None,
                       new_end_date: str | None,
                       date_changed: bool, time_changed: bool,
                       end_date_changed: bool, token: str) -> bool:
    """Patch Date, Month, and Time of day on a Notion page."""
    properties: dict = {}

    if date_changed or time_changed or end_date_changed:
        properties["Date"] = {
            "date": date_property_value(new_date, new_time, new_end_date),
        }
    if date_changed:
        properties["Month"] = {"select": {"name": month_name(new_date)}}
    if time_changed and new_time:
        properties["Time of day"] = {
            "rich_text": [{"text": {"content": time_of_day(new_time)}}],
        }

    if not properties:
        return True

    try:
        NotionClient(token).request("PATCH", f"/pages/{page_id}",
                                    {"properties": properties})
        return True
    except NotionError as e:
        print(f"    Notion update failed for {page_id}: {e}", file=sys.stderr)
        return False


# ── SQLite ───────────────────────────────────────────────────────────────────

SELECT_UPCOMING = """
SELECT id, name, date, end_date, time, url, source, notion_page_id
FROM candidates
WHERE verdict IN ('Going', 'Maybe', 'Undecided')
  AND COALESCE(end_date, date) >= date('now')
  AND notion_status = 'active'
  AND notion_page_id IS NOT NULL
  AND url IS NOT NULL
  AND source IN ('Meetup', 'Luma')
"""


# ── Core logic ───────────────────────────────────────────────────────────────

def verify(conn: sqlite3.Connection, dry_run: bool = False,
           notion_token: str | None = None) -> tuple[dict, list[dict]]:
    """Check upcoming events against source APIs and fix date/time/end_date drift."""
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        candidates = conn.execute(SELECT_UPCOMING).fetchall()
    finally:
        conn.row_factory = prev_factory

    stats = {
        "checked": 0, "date_fixed": 0, "time_fixed": 0,
        "end_date_fixed": 0, "not_found": 0, "errors": 0,
        "unchanged": 0, "notion_updated": 0, "notion_failed": 0,
    }
    changes: list[dict] = []

    print(f"Verifying {len(candidates)} upcoming events...")

    for row in candidates:
        cid = row["id"]
        source = row["source"]
        url = row["url"]
        stored_date = row["date"]
        stored_end_date = row["end_date"]
        stored_time = row["time"]
        name = row["name"]
        page_id = row["notion_page_id"]

        api_date: str | None = None
        api_time: str | None = None
        api_end_date: str | None = None

        if source == "Meetup":
            event_id = extract_meetup_id(url)
            if not event_id:
                continue
            node = meetup_event_by_id(event_id)
            if node is None:
                print(f"  NOT FOUND: {name} (ID {cid}): may be cancelled")
                stats["not_found"] += 1
                time.sleep(BATCH_PAUSE_SEC)
                continue
            api_date, api_time = parse_iso_datetime(node.get("dateTime"))
            api_end_date = parse_end_date(node.get("endTime"), api_date or "")

        elif source == "Luma":
            slug = _slug_from_luma_url(url)
            if not slug:
                continue
            data = luma_event_by_slug(slug)
            if data is None:
                print(f"  NOT FOUND: {name} (ID {cid}): may be cancelled")
                stats["not_found"] += 1
                time.sleep(BATCH_PAUSE_SEC)
                continue
            api_date, api_time, api_end_date = parse_luma_event(data)

        if not api_date:
            stats["errors"] += 1
            time.sleep(BATCH_PAUSE_SEC)
            continue

        stats["checked"] += 1

        date_changed = api_date != stored_date
        time_changed = bool(api_time and api_time != stored_time)
        end_date_changed = api_end_date != stored_end_date

        if not date_changed and not time_changed and not end_date_changed:
            stats["unchanged"] += 1
            time.sleep(BATCH_PAUSE_SEC)
            continue

        parts = []
        if date_changed:
            parts.append(f"date {stored_date} → {api_date}")
            stats["date_fixed"] += 1
        if time_changed:
            parts.append(f"time {stored_time} → {api_time}")
            stats["time_fixed"] += 1
        if end_date_changed:
            parts.append(f"end_date {stored_end_date} → {api_end_date}")
            stats["end_date_fixed"] += 1
        print(f"  CORRECTED: {name} (ID {cid}): {', '.join(parts)}")

        changes.append({
            "id": cid, "name": name,
            "old_date": stored_date, "new_date": api_date,
            "old_time": stored_time, "new_time": api_time,
            "old_end_date": stored_end_date, "new_end_date": api_end_date,
        })

        if dry_run:
            time.sleep(BATCH_PAUSE_SEC)
            continue

        # Update Notion first: if it fails, we skip SQLite so the next
        # run retries both (API date != SQLite date → change detected again).
        notion_ok = True
        if page_id and notion_token:
            notion_ok = update_notion_page(
                page_id, api_date, api_time, api_end_date,
                date_changed, time_changed, end_date_changed, notion_token,
            )
            if notion_ok:
                stats["notion_updated"] += 1
            else:
                stats["notion_failed"] += 1

        if not notion_ok:
            print("    Skipping SQLite update (Notion failed): will retry next run")
            time.sleep(BATCH_PAUSE_SEC)
            continue

        updates = []
        params: list = []
        if date_changed:
            updates.append("date = ?")
            params.append(api_date)
        if time_changed:
            updates.append("time = ?")
            params.append(api_time)
        if end_date_changed:
            updates.append("end_date = ?")
            params.append(api_end_date)
        params.append(cid)
        conn.execute(
            f"UPDATE candidates SET {', '.join(updates)} WHERE id = ?",
            params,
        )

        time.sleep(BATCH_PAUSE_SEC)

    conn.commit()
    return stats, changes


# ── Smoke tests ──────────────────────────────────────────────────────────────

def _run_smoke_tests() -> int:
    passed = 0

    # 1: extract_meetup_id
    assert extract_meetup_id("https://www.meetup.com/london-group/events/12345/") == "12345"
    assert extract_meetup_id("https://www.meetup.com/my-group/events/67890") == "67890"
    assert extract_meetup_id("https://example.com") is None
    assert extract_meetup_id(None) is None
    passed += 1

    # 2: _slug_from_luma_url (imported from fetch_luma)
    assert _slug_from_luma_url("https://lu.ma/abc-123") == "abc-123"
    assert _slug_from_luma_url("https://luma.com/my_event") == "my_event"
    assert _slug_from_luma_url("https://example.com") is None
    assert _slug_from_luma_url(None) is None
    assert _slug_from_luma_url("https://lu.ma/r/some-calendar") is None
    passed += 1

    # 3: parse_iso_datetime: timezone-aware
    d, t = parse_iso_datetime("2026-07-01T21:30:00+01:00")
    assert d == "2026-07-01", f"date: {d}"
    assert t == "21:30", f"time: {t}"
    passed += 1

    # 4: parse_iso_datetime: UTC offset
    d, t = parse_iso_datetime("2026-07-01T20:30:00+00:00")
    assert d == "2026-07-01", f"UTC date: {d}"
    assert t == "21:30", f"UTC→BST time: {t}"
    passed += 1

    # 5: parse_iso_datetime: naive (no tz)
    d, t = parse_iso_datetime("2026-07-01T19:00:00")
    assert d == "2026-07-01"
    assert t == "19:00"
    passed += 1

    # 6: parse_iso_datetime: empty/None
    assert parse_iso_datetime("") == (None, None)
    assert parse_iso_datetime(None) == (None, None)
    passed += 1

    # 7: parse_end_date
    assert parse_end_date("2026-07-02T23:00:00+01:00", "2026-07-01") == "2026-07-02"
    assert parse_end_date("2026-07-01T23:00:00+01:00", "2026-07-01") is None
    assert parse_end_date(None, "2026-07-01") is None
    assert parse_end_date("", "2026-07-01") is None
    passed += 1

    # 8: parse_luma_event
    d, t, ed = parse_luma_event({
        "event": {
            "start_at": "2026-07-15T18:00:00Z",
            "end_at": "2026-07-16T01:00:00Z",
            "timezone": "Europe/London",
        }
    })
    assert d == "2026-07-15", f"luma date: {d}"
    assert t == "19:00", f"luma time: {t}"
    assert ed == "2026-07-16", f"luma end_date: {ed}"
    passed += 1

    # 9: parse_luma_event: same-day end
    d, t, ed = parse_luma_event({
        "event": {
            "start_at": "2026-07-15T18:00:00Z",
            "end_at": "2026-07-15T21:00:00Z",
            "timezone": "Europe/London",
        }
    })
    assert ed is None, "same-day end should be None"
    passed += 1

    # 10: parse_luma_event: empty
    assert parse_luma_event({}) == (None, None, None)
    assert parse_luma_event({"event": {}}) == (None, None, None)
    passed += 1

    # 11: time_of_day (imported from write_notion)
    assert time_of_day("19:00") == "Evening"
    assert time_of_day("09:30") == "Morning"
    assert time_of_day(None) == "Not extracted"
    passed += 1

    # 12: verify with empty DB
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY, name TEXT, date TEXT, time TEXT, end_date TEXT,
        url TEXT, source TEXT, notion_page_id TEXT, verdict TEXT,
        notion_status TEXT
    )""")
    stats, changes = verify(conn, dry_run=True)
    assert stats["checked"] == 0
    assert changes == []
    conn.close()
    passed += 1

    # 13: row_factory restored after verify
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY, name TEXT, date TEXT, time TEXT, end_date TEXT,
        url TEXT, source TEXT, notion_page_id TEXT, verdict TEXT,
        notion_status TEXT
    )""")
    original_factory = conn.row_factory
    verify(conn, dry_run=True)
    assert conn.row_factory is original_factory, "row_factory should be restored"
    conn.close()
    passed += 1

    print(f"  All {passed} smoke tests passed (expected 13)")
    return 0


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Print changes without applying")
    p.add_argument("--smoke-test", action="store_true", help="Run offline smoke tests")
    p.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    args = p.parse_args()

    if args.smoke_test:
        return _run_smoke_tests()

    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token:
        print("NOTE: NOTION_TOKEN not set: Notion pages will not be updated")

    conn = sqlite3.connect(args.db)
    try:
        stats, changes = verify(conn, dry_run=args.dry_run, notion_token=notion_token)
        print(
            f"\nDone: {stats['checked']} checked, "
            f"{stats['date_fixed']} dates fixed, {stats['time_fixed']} times fixed, "
            f"{stats['end_date_fixed']} end_dates fixed, "
            f"{stats['unchanged']} unchanged, {stats['not_found']} not found, "
            f"{stats['errors']} errors"
        )
        if notion_token:
            if stats["notion_updated"]:
                print(f"  Notion pages updated: {stats['notion_updated']}")
            if stats["notion_failed"]:
                print(f"  Notion updates FAILED (will retry next run): {stats['notion_failed']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
