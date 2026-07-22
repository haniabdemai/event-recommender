#!/usr/bin/env python3
"""
Sync Notion-triaged events to Google Calendar.

Creates/updates/deletes events in a secondary events calendar
based on candidate verdicts (Going/Maybe/Undecided) in SQLite.

Usage:
    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=... \
    GCAL_CALENDAR_ID=... python3 sync_to_gcal.py [--dry-run] [--smoke-test]

Prerequisites:
    - OAuth token with calendar write scope
    - a dedicated events calendar created (GCAL_CALENDAR_ID in .env)
    - Fresh verdicts (run sync_verdicts.py --force first)
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
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib.config import CITY_TZ, DB_PATH as DEFAULT_DB  # noqa: E402

CALENDAR_API = "https://www.googleapis.com/calendar/v3"

VERDICT_EMOJI = {"Going": "\U0001f7e2", "Maybe": "\U0001f7e1", "Undecided": "⚪"}
VERDICT_COLOR = {"Going": "10", "Maybe": "5", "Undecided": "8"}
SYNC_VERDICTS = set(VERDICT_EMOJI.keys())

BATCH_PAUSE_SEC = 0.35


# ── Formatting ────────────────────────────────────────────────────────────────

def format_title(name: str, verdict: str) -> str:
    emoji = VERDICT_EMOJI.get(verdict, "⚪")
    return f"{emoji} {name}"


def format_description(candidate: dict) -> str:
    parts = []

    line1_parts = []
    if candidate.get("format_type"):
        line1_parts.append(candidate["format_type"])
    if candidate.get("cost"):
        line1_parts.append(candidate["cost"])
    if candidate.get("tier"):
        line1_parts.append(candidate["tier"])
    if line1_parts:
        parts.append(" · ".join(line1_parts))

    if candidate.get("travel_display"):
        parts.append(f"\U0001f4cd {candidate['travel_display']} from home")

    score_line_parts = []
    if candidate.get("score") is not None:
        score_line_parts.append(f"Score: {candidate['score']}")
    if candidate.get("signals_fired"):
        try:
            signals = json.loads(candidate["signals_fired"])
            short = [s.split(": ", 1)[-1] if ": " in s else s for s in signals]
            score_line_parts.append(", ".join(short))
        except (json.JSONDecodeError, TypeError):
            pass
    if score_line_parts:
        parts.append("\U0001f3af " + " · ".join(score_line_parts))

    if candidate.get("organiser"):
        parts.append(f"\U0001f464 {candidate['organiser']}")

    if candidate.get("description"):
        desc = candidate["description"]
        if len(desc) > 500:
            desc = desc[:497] + "..."
        parts.append(f"\n{desc}")

    links = []
    if candidate.get("url"):
        links.append(f"\U0001f517 Source: {candidate['url']}")
    if candidate.get("notion_page_id"):
        page_id = candidate["notion_page_id"].replace("-", "")
        links.append(f"\U0001f4cb Notion: https://www.notion.so/{page_id}")
    if links:
        parts.append("\n".join(links))

    return "\n".join(parts)


def format_location(candidate: dict) -> str:
    parts = []
    if candidate.get("venue_name"):
        parts.append(candidate["venue_name"])
    if candidate.get("venue_postcode"):
        parts.append(candidate["venue_postcode"])
    return ", ".join(parts) if parts else ""


def build_event_body(candidate: dict) -> dict:
    verdict = candidate["verdict"]
    body = {
        "summary": format_title(candidate["name"], verdict),
        "description": format_description(candidate),
        "colorId": VERDICT_COLOR.get(verdict, "8"),
    }

    location = format_location(candidate)
    if location:
        body["location"] = location

    if candidate.get("url"):
        body["source"] = {"title": "Event page", "url": candidate["url"]}

    event_date = candidate["date"]
    end_date = candidate.get("end_date")
    event_time = candidate.get("time")

    has_time = bool(
        event_time
        and event_time != "TBC"
        and re.match(r"^\d{1,2}:\d{2}$", event_time)
    )

    if has_time and end_date:
        # Multi-day event with a known start time: all-day span is more useful
        body["start"] = {"date": event_date}
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        body["end"] = {"date": end_dt.strftime("%Y-%m-%d")}
    elif has_time:
        padded = event_time.zfill(5)
        start_dt = f"{event_date}T{padded}:00"
        end_obj = datetime.strptime(start_dt, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=2)
        end_dt = end_obj.strftime("%Y-%m-%dT%H:%M:%S")
        body["start"] = {"dateTime": start_dt, "timeZone": CITY_TZ.key}
        body["end"] = {"dateTime": end_dt, "timeZone": CITY_TZ.key}
    elif end_date:
        body["start"] = {"date": event_date}
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        body["end"] = {"date": end_dt.strftime("%Y-%m-%d")}
    else:
        body["start"] = {"date": event_date}
        end_dt = datetime.strptime(event_date, "%Y-%m-%d") + timedelta(days=1)
        body["end"] = {"date": end_dt.strftime("%Y-%m-%d")}

    return body


def candidate_hash(candidate: dict) -> str:
    relevant = {
        "name": candidate.get("name"),
        "date": candidate.get("date"),
        "end_date": candidate.get("end_date"),
        "time": candidate.get("time"),
        "venue_name": candidate.get("venue_name"),
        "venue_postcode": candidate.get("venue_postcode"),
        "cost": candidate.get("cost"),
        "format_type": candidate.get("format_type"),
        "tier": candidate.get("tier"),
        "score": candidate.get("score"),
        "signals_fired": candidate.get("signals_fired"),
        "travel_display": candidate.get("travel_display"),
        "url": candidate.get("url"),
        "verdict": candidate.get("verdict"),
        "description": (candidate.get("description") or "")[:500],
        "organiser": candidate.get("organiser"),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


# ── SQLite ────────────────────────────────────────────────────────────────────

GCAL_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS gcal_events (
    candidate_id INTEGER PRIMARY KEY,
    gcal_event_id TEXT NOT NULL,
    last_synced TEXT NOT NULL,
    last_verdict TEXT NOT NULL,
    content_hash TEXT NOT NULL
)
"""

SELECT_CANDIDATES = """
SELECT id, name, date, end_date, time, venue_name, venue_postcode,
       cost, format_type, tier, score, signals_fired, travel_display,
       url, notion_page_id, verdict, description, organiser
FROM candidates
WHERE verdict IN ('Going', 'Maybe', 'Undecided')
  AND COALESCE(end_date, date) >= date('now')
  AND notion_status = 'active'
  AND notion_page_id IS NOT NULL
"""


def ensure_gcal_table(conn: sqlite3.Connection) -> None:
    conn.execute(GCAL_EVENTS_SCHEMA)
    conn.commit()


def get_synced_events(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT candidate_id, gcal_event_id, last_verdict, content_hash FROM gcal_events"
    ).fetchall()
    return {
        r[0]: {"gcal_event_id": r[1], "last_verdict": r[2], "content_hash": r[3]}
        for r in rows
    }


def get_eligible_candidates(conn: sqlite3.Connection) -> list[dict]:
    prev_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(SELECT_CANDIDATES).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.row_factory = prev_factory


# ── Google Calendar API ───────────────────────────────────────────────────────

from erlib.google_auth import refresh_access_token  # noqa: E402

# Set by main(): (client_id, client_secret, refresh_token). Long syncs
# outlive the ~1h access token; a 401 mid-run re-refreshes once instead
# of failing the rest of the sync (audit P2). None during smoke tests.
_GCAL_CREDS = None


def _gcal_open(build_req, access_token, max_retries=3):
    """Send a Calendar API request with bounded 429 backoff and 401
    token re-refresh. build_req: token -> urllib Request."""
    token = access_token
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(build_req(token), timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 401 and _GCAL_CREDS and attempt < max_retries:
                token = refresh_access_token(*_GCAL_CREDS)
                continue
            if e.code == 429 and attempt < max_retries:
                try:
                    wait = int(e.headers.get("Retry-After", "5") or "5")
                except (ValueError, AttributeError):
                    wait = 5
                time.sleep(min(wait, 60))
                continue
            raise
    raise last_err


def _json_request(url, method, event_body, token):
    req = urllib.request.Request(
        url, data=json.dumps(event_body).encode() if event_body is not None else None,
        method=method,
    )
    req.add_header("Authorization", f"Bearer {token}")
    if event_body is not None:
        req.add_header("Content-Type", "application/json")
    return req


def gcal_create(access_token: str, calendar_id: str, event_body: dict) -> str:
    url = f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}/events"
    raw = _gcal_open(lambda t: _json_request(url, "POST", event_body, t), access_token)
    return json.loads(raw)["id"]


def gcal_update(access_token: str, calendar_id: str, event_id: str, event_body: dict) -> None:
    url = (f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}"
           f"/events/{urllib.parse.quote(event_id)}")
    _gcal_open(lambda t: _json_request(url, "PUT", event_body, t), access_token)


def gcal_delete(access_token: str, calendar_id: str, event_id: str) -> None:
    url = (f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}"
           f"/events/{urllib.parse.quote(event_id)}")
    try:
        _gcal_open(lambda t: _json_request(url, "DELETE", None, t), access_token)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            pass
        else:
            raise


# ── Sync logic ────────────────────────────────────────────────────────────────

def sync(
    conn: sqlite3.Connection,
    access_token: str,
    calendar_id: str,
    dry_run: bool = False,
) -> dict:
    ensure_gcal_table(conn)
    candidates = get_eligible_candidates(conn)
    synced = get_synced_events(conn)

    eligible_ids = {c["id"] for c in candidates}
    stats = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "errors": 0}

    for candidate in candidates:
        cid = candidate["id"]
        new_hash = candidate_hash(candidate)
        event_body = build_event_body(candidate)

        if cid not in synced:
            print(f"  CREATE: {candidate['name']} ({candidate['verdict']})")
            if not dry_run:
                try:
                    gcal_id = gcal_create(access_token, calendar_id, event_body)
                    conn.execute(
                        "INSERT INTO gcal_events (candidate_id, gcal_event_id, last_synced, last_verdict, content_hash) "
                        "VALUES (?, ?, datetime('now'), ?, ?)",
                        (cid, gcal_id, candidate["verdict"], new_hash),
                    )
                    conn.commit()
                    time.sleep(BATCH_PAUSE_SEC)
                except Exception as e:
                    print(f"    ERROR creating: {e}", file=sys.stderr)
                    stats["errors"] += 1
                    continue
            stats["created"] += 1

        elif synced[cid]["content_hash"] != new_hash:
            print(f"  UPDATE: {candidate['name']} ({synced[cid]['last_verdict']} -> {candidate['verdict']})")
            if not dry_run:
                try:
                    gcal_update(access_token, calendar_id, synced[cid]["gcal_event_id"], event_body)
                    conn.execute(
                        "UPDATE gcal_events SET last_synced = datetime('now'), last_verdict = ?, content_hash = ? "
                        "WHERE candidate_id = ?",
                        (candidate["verdict"], new_hash, cid),
                    )
                    conn.commit()
                    time.sleep(BATCH_PAUSE_SEC)
                except Exception as e:
                    print(f"    ERROR updating: {e}", file=sys.stderr)
                    stats["errors"] += 1
                    continue
            stats["updated"] += 1

        else:
            stats["unchanged"] += 1

    stale_ids = set(synced.keys()) - eligible_ids
    for cid in stale_ids:
        gcal_event_id = synced[cid]["gcal_event_id"]
        print(f"  DELETE: candidate {cid} (gcal {gcal_event_id})")
        if not dry_run:
            try:
                gcal_delete(access_token, calendar_id, gcal_event_id)
                conn.execute("DELETE FROM gcal_events WHERE candidate_id = ?", (cid,))
                conn.commit()
                time.sleep(BATCH_PAUSE_SEC)
            except Exception as e:
                print(f"    ERROR deleting: {e}", file=sys.stderr)
                stats["errors"] += 1
                continue
        stats["deleted"] += 1

    return stats


# ── Smoke tests ───────────────────────────────────────────────────────────────

def _run_smoke_tests() -> int:
    passed = 0

    # Test 1: format_title
    assert format_title("Jazz Night", "Going") == "\U0001f7e2 Jazz Night"
    assert format_title("Poetry", "Maybe") == "\U0001f7e1 Poetry"
    assert format_title("Art Show", "Undecided") == "⚪ Art Show"
    passed += 1

    # Test 2: format_location
    assert format_location({"venue_name": "City Arts Centre", "venue_postcode": "EC1A 4EE"}) == "City Arts Centre, EC1A 4EE"
    assert format_location({"venue_name": "City Arts Centre"}) == "City Arts Centre"
    assert format_location({}) == ""
    passed += 1

    # Test 3: format_description contains key fields
    candidate = {
        "format_type": "Workshop",
        "cost": "£10",
        "tier": "Recommended",
        "travel_display": "25 min (transit)",
        "score": 42,
        "signals_fired": '["Signal #1: AI/tech community", "Signal #4: Hands-on"]',
        "organiser": "Tech Meetup London",
        "description": "A hands-on workshop about electronics.",
        "url": "https://meetup.com/test/123",
        "notion_page_id": "00000000-0000-4000-8000-000000000001",
    }
    desc = format_description(candidate)
    assert "Workshop" in desc and "Recommended" in desc, "first line format"
    assert "25 min (transit) from home" in desc, "travel"
    assert "Score: 42" in desc, "score"
    assert "AI/tech community" in desc, "signal parsed"
    assert "Tech Meetup London" in desc, "organiser"
    assert "hands-on workshop" in desc, "description body"
    assert "meetup.com/test/123" in desc, "source link"
    assert "notion.so/00000000000040008000000000000001" in desc, "notion link"
    passed += 1

    # Test 4: format_description with minimal data
    desc_min = format_description({"name": "Test"})
    assert isinstance(desc_min, str), "minimal returns string"
    passed += 1

    # Test 5: format_description truncates long descriptions
    desc_long = format_description({"description": "x" * 1000})
    assert len(desc_long) < 600, "description truncated"
    assert desc_long.endswith("..."), "truncation marker"
    passed += 1

    # Test 6: build_event_body with time
    timed = {
        "name": "Jazz Night", "verdict": "Going", "date": "2026-07-15",
        "time": "19:00", "end_date": None, "venue_name": "The Jazz Cellar",
        "venue_postcode": "W1F 8AA", "cost": "£30", "format_type": "Concert",
        "tier": "Top Picks", "score": 55, "signals_fired": "[]",
        "travel_display": "20 min (transit)", "url": "https://example.com",
        "notion_page_id": "abc-def", "description": "Live jazz", "organiser": None,
    }
    body = build_event_body(timed)
    assert body["summary"] == "\U0001f7e2 Jazz Night", "timed title"
    assert body["colorId"] == "10", "Going = Basil"
    assert body["start"]["dateTime"] == "2026-07-15T19:00:00", "timed start"
    assert body["end"]["dateTime"] == "2026-07-15T21:00:00", "timed end (2h default)"
    assert body["location"] == "The Jazz Cellar, W1F 8AA", "timed location"
    passed += 1

    # Test 7: build_event_body all-day (no time)
    allday = dict(timed, time=None, verdict="Maybe")
    body_ad = build_event_body(allday)
    assert body_ad["start"] == {"date": "2026-07-15"}, "all-day start"
    assert body_ad["end"] == {"date": "2026-07-16"}, "all-day end (next day)"
    assert body_ad["colorId"] == "5", "Maybe = Banana"
    passed += 1

    # Test 8: build_event_body TBC time treated as all-day
    tbc = dict(timed, time="TBC", verdict="Undecided")
    body_tbc = build_event_body(tbc)
    assert "date" in body_tbc["start"], "TBC = all-day"
    assert body_tbc["colorId"] == "8", "Undecided = Graphite"
    passed += 1

    # Test 9: build_event_body multi-day without time (end_date only)
    multi = dict(timed, time=None, end_date="2026-07-20")
    body_m = build_event_body(multi)
    assert body_m["start"] == {"date": "2026-07-15"}, "multi start"
    assert body_m["end"] == {"date": "2026-07-21"}, "multi end (day after end_date)"
    passed += 1

    # Test 9b: build_event_body multi-day WITH time → all-day span (not 2h slot)
    multi_timed = dict(timed, time="15:00", end_date="2026-07-31")
    body_mt = build_event_body(multi_timed)
    assert body_mt["start"] == {"date": "2026-07-15"}, "multi+time start is all-day"
    assert body_mt["end"] == {"date": "2026-08-01"}, "multi+time end covers full span"
    assert "dateTime" not in body_mt["start"], "multi+time must NOT be a timed event"
    passed += 1

    # Test 9c: single-digit hour time (e.g. "9:00") → timed event, zero-padded
    morning = dict(timed, time="9:00", end_date=None)
    body_am = build_event_body(morning)
    assert body_am["start"]["dateTime"] == "2026-07-15T09:00:00", "single-digit hour padded"
    passed += 1

    # Test 9d: malformed time → treated as all-day
    bad_time = dict(timed, time="Doors 7pm", end_date=None)
    body_bad = build_event_body(bad_time)
    assert "date" in body_bad["start"], "malformed time → all-day"
    passed += 1

    # Test 10: candidate_hash changes when verdict changes
    h1 = candidate_hash({"name": "Test", "verdict": "Maybe"})
    h2 = candidate_hash({"name": "Test", "verdict": "Going"})
    assert h1 != h2, "hash differs on verdict change"
    passed += 1

    # Test 11: candidate_hash stable for same input
    h3 = candidate_hash({"name": "Test", "verdict": "Maybe"})
    assert h1 == h3, "hash stable"
    passed += 1

    # Test 12: candidate_hash changes when travel_display changes
    h4 = candidate_hash({"name": "Test", "verdict": "Maybe", "travel_display": "25 min"})
    h5 = candidate_hash({"name": "Test", "verdict": "Maybe", "travel_display": "30 min"})
    assert h4 != h5, "hash differs on travel change"
    passed += 1

    # Test 13: gcal_events table creation
    conn = sqlite3.connect(":memory:")
    ensure_gcal_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(gcal_events)").fetchall()}
    assert cols == {"candidate_id", "gcal_event_id", "last_synced", "last_verdict", "content_hash"}, f"table cols: {cols}"
    conn.close()
    passed += 1

    # Test 14: get_synced_events with empty table
    conn = sqlite3.connect(":memory:")
    ensure_gcal_table(conn)
    assert get_synced_events(conn) == {}, "empty synced"
    conn.close()
    passed += 1

    # Test 15: get_synced_events with data
    conn = sqlite3.connect(":memory:")
    ensure_gcal_table(conn)
    conn.execute(
        "INSERT INTO gcal_events VALUES (1, 'gcal123', '2026-06-30', 'Going', 'abc')"
    )
    synced = get_synced_events(conn)
    assert synced[1]["gcal_event_id"] == "gcal123", "synced lookup"
    assert synced[1]["last_verdict"] == "Going", "synced verdict"
    conn.close()
    passed += 1

    # Test 16: _gcal_open retries a 429 with Retry-After, then succeeds
    import email.message
    import io

    def _http_error(code, retry_after=None):
        hdrs = email.message.Message()
        if retry_after:
            hdrs["Retry-After"] = retry_after
        return urllib.error.HTTPError("http://x", code, "err", hdrs, io.BytesIO(b""))

    attempts = {"n": 0}

    class _FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"id": "ok123"}'

    def _fake_urlopen_429(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, "0")
        return _FakeResp()

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen_429
    try:
        raw = _gcal_open(lambda t: _json_request("http://x", "GET", None, t), "tok")
        assert json.loads(raw)["id"] == "ok123", "429 retry result"
        assert attempts["n"] == 2, "429 retried exactly once"
    finally:
        urllib.request.urlopen = real_urlopen
    passed += 1

    # Test 17: _gcal_open re-refreshes the token on 401 mid-run
    global _GCAL_CREDS
    tokens_seen = []

    def _fake_urlopen_401(req, timeout=None):
        tokens_seen.append(req.get_header("Authorization"))
        if len(tokens_seen) == 1:
            raise _http_error(401)
        return _FakeResp()

    real_refresh = globals()["refresh_access_token"]
    globals()["refresh_access_token"] = lambda cid, cs, rt: "fresh-token"
    urllib.request.urlopen = _fake_urlopen_401
    _GCAL_CREDS = ("cid", "cs", "rt")
    try:
        _gcal_open(lambda t: _json_request("http://x", "GET", None, t), "stale-token")
        assert tokens_seen == ["Bearer stale-token", "Bearer fresh-token"], \
            f"401 re-refresh tokens: {tokens_seen}"
    finally:
        urllib.request.urlopen = real_urlopen
        globals()["refresh_access_token"] = real_refresh
        _GCAL_CREDS = None
    passed += 1

    print(f"  All {passed} smoke tests passed (expected 20)")
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    p.add_argument("--smoke-test", action="store_true", help="Run offline smoke tests")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    args = p.parse_args()

    if args.smoke_test:
        return _run_smoke_tests()

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    calendar_id = os.environ.get("GCAL_CALENDAR_ID")

    if not all([client_id, client_secret, refresh_token, calendar_id]):
        print(
            "ERROR: Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
            "GOOGLE_REFRESH_TOKEN, and GCAL_CALENDAR_ID",
            file=sys.stderr,
        )
        return 1

    print("Refreshing OAuth token...")
    global _GCAL_CREDS
    _GCAL_CREDS = (client_id, client_secret, refresh_token)
    access_token = refresh_access_token(client_id, client_secret, refresh_token)

    conn = sqlite3.connect(args.db)
    try:
        print(f"Syncing to calendar {calendar_id}...")
        stats = sync(conn, access_token, calendar_id, dry_run=args.dry_run)
        print(
            f"\nDone: {stats['created']} created, {stats['updated']} updated, "
            f"{stats['deleted']} deleted, {stats['unchanged']} unchanged, "
            f"{stats['errors']} errors"
        )
        return 1 if stats["errors"] > 0 else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
