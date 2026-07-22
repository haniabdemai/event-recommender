#!/usr/bin/env python3
"""
Write LLM-reviewed candidates from SQLite to the Notion "Event Recommandations" database.

Deterministic. No LLM. Reads NOTION_TOKEN and NOTION_DATABASE_ID from env.
Field mapping, eligibility filtering and page-body format follow the rules
below (field mapping,
§1.3 body). The write-time validator (see §Write-time
validator) blocks writes when scoring-pipeline artefacts are detected.

Usage:
    NOTION_TOKEN=ntn_... NOTION_DATABASE_ID=<your-db-id> python3 write_notion.py [--dry-run] [--limit N]

A row is eligible when:
    pipeline_state = 'ready_to_write'

The llm_tier clause means llm_sense_check.py must run before this script.
The last clause ensures travel_time.py has run first (or marked failure).
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
import os
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

_HHMM_RE = re.compile(r'^\d{2}:\d{2}$')

from erlib.config import CITY_TZ, DB_PATH, TRAVEL_ORIGIN_LABEL
from erlib.dates import batch_label
from erlib.notion import (
    NOTION_TEXT_LIMIT, NotionClient, truncate_utf16, truncate_with_ellipsis,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)

SOURCE_ENUM = {
    "Meetup", "Luma", "Eventbrite", "Newsletter", "Venue website",
    # reconcile_notion --recover stubs user-added pages as source='manual';
    # if one ever reaches a write, label it truthfully rather than remapping
    # to 'Newsletter'
    "manual",
}

# Place property (map pin) only attached for events from this date onward.
# Earlier events stay without a pin: no backfill is performed.
PLACE_FROM_DATE = "2026-05-01"


# ---------------------------------------------------------------------------
# Derivations (field mapping + §1.3 body format)
# ---------------------------------------------------------------------------

def month_name(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%B")


def body_date(iso_date: str) -> str:
    # "Sat 12 April": %-d is strftime's "no-leading-zero day"
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%a %-d %B")


def tier_label(tier: str) -> str:
    if tier not in ("Top Picks", "Recommended", "Borderline", "Couldn't Process"):
        raise ValueError(f"Unexpected tier {tier!r}: expected Top Picks, Recommended, Borderline, or Couldn't Process")
    return tier


def transit_only(travel_display: str | None, travel_lookup_failed: int,
                venue_name: str | None = None) -> str:
    """Short, specific label for Notion's Travel field: fits the card preview.

    Cases:
      - we have travel numbers            → "35 min (transit)"
      - venue itself is unknown/empty     → "Venue unknown"
      - venue known but no usable route   → "Route unavailable"
    """
    if travel_display:
        return travel_display.split("·")[0].strip()
    name = (venue_name or "").strip()
    if not name or name.lower() in ("unknown", "tbc", "tba", "n/a"):
        return "Venue unknown"
    return "Route unavailable"


def _notion_text(s: str) -> str:
    """Truncate to Notion's rich_text limit, appending ellipsis if cut."""
    return truncate_with_ellipsis(s)


def date_property_value(iso_date: str, time_str: str | None,
                        end_date: str | None = None) -> dict:
    """Notion Date property value: datetime when time is HH:MM, date-only otherwise.

    Supports date ranges: when end_date is set, Notion shows "start → end".
    The configured city zoneinfo (erlib.config.CITY_TZ, ER_TIMEZONE) handles
    DST transitions correctly.
    """
    if time_str and _HHMM_RE.match(time_str.strip()):
        h, m = time_str.strip().split(":")
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        dt = datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=CITY_TZ)
        result = {"start": dt.isoformat(timespec="seconds")}
    else:
        result = {"start": iso_date}
    if end_date:
        # Notion requires both start and end to be the same type (both date-only
        # or both datetime). Force date-only for ranges; time is in "Time of day".
        result["start"] = iso_date
        result["end"] = end_date
    return result


def time_of_day(time_str: str | None) -> str:
    """Bucket label for the 'Time of day' Select property.

    Three-way transparency:
      - real HH:MM   → "Morning" / "Afternoon" / "Evening"
      - "TBC"        → "TBC" (source genuinely said TBC)
      - null/garbage → "Not extracted" (we never captured it)
    """
    if time_str is None or not time_str.strip():
        return "Not extracted"
    s = time_str.strip()
    if s.upper() == "TBC":
        return "TBC"
    if not _HHMM_RE.match(s):
        return "Not extracted"
    h = int(s[:2])
    if h < 12:
        return "Morning"
    if h < 17:
        return "Afternoon"
    return "Evening"


def time_display(time_str: str | None) -> str:
    """Human-readable time for the body 'Time:' line: preserves transparency."""
    if time_str is None or not time_str.strip():
        return "Not extracted"
    s = time_str.strip()
    if s.upper() == "TBC":
        return "TBC"
    if _HHMM_RE.match(s):
        return s
    return "Not extracted"


def get_description(row: sqlite3.Row) -> str:
    row_dict = dict(row)
    desc = row_dict.get("description")
    if desc is None or not desc.strip():
        return "TBC"
    return truncate_utf16(desc, NOTION_TEXT_LIMIT)


def source_value(source: str | None) -> str:
    """Map to a known Source select option. Out-of-enum values fall back to
    "Newsletter" with a log line: writing them straight through would mint
    new select options in Notion (audit P2: the old else-branch returned
    ``source`` either way, so the enum check did nothing)."""
    if source in SOURCE_ENUM:
        return source
    print(f"  WARN: unknown source {source!r}: writing as 'Newsletter'", file=sys.stderr)
    return "Newsletter"


GMAIL_URL = "https://mail.google.com/mail/u/0/#all/{}"


def _format_email_date(iso_date: str | None) -> str | None:
    """'2026-05-14' → '14 May'."""
    if not iso_date:
        return None
    try:
        return batch_label(iso_date)
    except ValueError:
        return None


def fetch_newsletter_lookup(conn: sqlite3.Connection) -> dict[str, tuple[str, str | None]]:
    """Pre-fetch processed_emails into {gmail_message_id: (sender, email_date)}."""
    lookup: dict[str, tuple[str, str | None]] = {}
    for r in conn.execute(
        "SELECT gmail_message_id, sender, email_date FROM processed_emails"
    ):
        lookup[r[0]] = (r[1], r[2])
    return lookup


def source_detail_text(row: sqlite3.Row, pe_lookup: dict) -> str | None:
    """Build 'Riverfront Arts Newsletter, 14 May' text for Source Detail property. None if unavailable."""
    if (row["source"] or "") != "Newsletter":
        return None
    snapshot = row["source_snapshot"]
    if not snapshot or snapshot not in pe_lookup:
        return None
    sender, email_date = pe_lookup[snapshot]
    formatted = _format_email_date(email_date)
    if formatted:
        return f"{sender} Newsletter, {formatted}"
    return f"{sender} Newsletter"


def source_detail_rich_text(row: sqlite3.Row, pe_lookup: dict) -> dict | None:
    """Build Notion rich_text property value for Source Detail. None if unavailable."""
    text = source_detail_text(row, pe_lookup)
    if not text:
        return None
    return {"rich_text": [{"text": {"content": text}}]}


def source_detail_gmail_url(row: sqlite3.Row) -> str | None:
    """Build Gmail URL from source_snapshot. None if not a newsletter or no snapshot."""
    if (row["source"] or "") != "Newsletter":
        return None
    snapshot = row["source_snapshot"]
    if not snapshot:
        return None
    return GMAIL_URL.format(snapshot)


_ARTEFACT_RE = re.compile(r'(?i)(signal\s*\d+|past\s*pattern\s*:|already\s*counted)')


def validate_no_artefacts(row: sqlite3.Row) -> str | None:
    """Return offending field name if scoring artefacts leak into text, else None."""
    for field in ("name", "description", "venue_name", "cost"):
        val = row[field]
        if val and _ARTEFACT_RE.search(val):
            return field
    return None


# ---------------------------------------------------------------------------
# Notion HTTP (stdlib only)
# ---------------------------------------------------------------------------

class Notion:
    def __init__(self, token: str):
        self._client = NotionClient(token)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        # erlib client: bounded 429 retries, raises NotionError (RuntimeError).
        return self._client.request(method, path, body)

    def find_duplicate(self, database_id: str, name: str, iso_date: str) -> str | None:
        body = {
            "filter": {
                "and": [
                    {"property": "Event", "title": {"equals": name}},
                    {"property": "Date", "date": {"equals": iso_date}},
                ]
            },
            "page_size": 1,
        }
        r = self._request("POST", f"/databases/{database_id}/query", body)
        results = r.get("results", [])
        return results[0]["id"] if results else None

    def create_page(self, database_id: str, properties: dict, body_blocks: list[dict]) -> str:
        payload = {
            "parent": {"database_id": database_id},
            "properties": properties,
            "children": body_blocks,
        }
        r = self._request("POST", "/pages", payload)
        return r["id"]


# ---------------------------------------------------------------------------
# Property + body builders (the exact shape Notion expects)
# ---------------------------------------------------------------------------

def get_venue_geo(conn: sqlite3.Connection,
                  venue_name: str | None,
                  venue_postcode: str | None) -> sqlite3.Row | None:
    """Look up cached {venue_lat, venue_lon, formatted_address} for this venue.

    Matches travel_time.py's fetch_cache logic: venue_name first, then
    venue_postcode (both case-insensitive). No TTL check: lat/lon doesn't
    expire the way travel time does (venues don't move). Returns the full
    venues row, or None.
    """
    name = (venue_name or "").strip() or None
    pc = (venue_postcode or "").strip() or None
    if name:
        r = conn.execute(
            "SELECT * FROM venues WHERE venue_name = ? COLLATE NOCASE "
            "AND venue_lat IS NOT NULL ORDER BY lookup_date DESC LIMIT 1",
            (name,),
        ).fetchone()
        if r:
            return r
    if pc:
        r = conn.execute(
            "SELECT * FROM venues WHERE venue_postcode = ? COLLATE NOCASE "
            "AND venue_lat IS NOT NULL ORDER BY lookup_date DESC LIMIT 1",
            (pc,),
        ).fetchone()
        if r:
            return r
    return None


def build_place(row: sqlite3.Row, venue_geo: sqlite3.Row | None) -> dict | None:
    """Return a Notion Place property payload, or None if unavailable.

    `name` is always the row's venue_name (stays consistent with the Venue
    field on the card). `address` falls back to the raw venue_postcode if the
    cache didn't store a formatted one. Rejected (returns None) when:
      - event date is before PLACE_FROM_DATE
      - no venue_name
      - no cached lat/lon
    """
    if row["date"] < PLACE_FROM_DATE:
        return None
    name = (row["venue_name"] or "").strip()
    if not name or not venue_geo or venue_geo["venue_lat"] is None:
        return None
    # Notion Place API requires 'lat'/'lon' for both CREATE and PATCH.
    return {
        "name": name,
        "address": venue_geo["formatted_address"] or (row["venue_postcode"] or name),
        "lat": venue_geo["venue_lat"],
        "lon": venue_geo["venue_lon"],
    }


def build_properties(row: sqlite3.Row, today_iso: str,
                     venue_geo: sqlite3.Row | None = None) -> dict:
    props: dict[str, dict] = {
        "Event":        {"title":     [{"text": {"content": _notion_text(row["name"])}}]},
        "Date":         {"date":      date_property_value(row["date"], row["time"],
                                                           dict(row).get("end_date"))},
        "Time of day":  {"rich_text": [{"text": {"content": time_of_day(row["time"])}}]},
        "Month":        {"select":    {"name": month_name(row["date"])}},
        "Tier":         {"select":    {"name": tier_label(row["llm_tier"])}},
        "Score":        {"number":    int(row["score"]) if row["score"] is not None else 0},
        "Type":         {"select":    {"name": row["format_type"] or "Other"}},
        "Added":        {"date":      {"start": today_iso}},
        "Batch":        {"select":    {"name": batch_label(today_iso)}},
    }
    if row["venue_name"]:
        props["Venue"] = {"rich_text": [{"text": {"content": row["venue_name"]}}]}
    if row["cost"]:
        props["Cost"] = {"rich_text": [{"text": {"content": row["cost"]}}]}
    desc = get_description(row)
    if desc != "TBC":
        props["Description"] = {"rich_text": [{"text": {"content": _notion_text(desc)}}]}
    if row["url"]:
        props["Link"] = {"url": row["url"]}
    if row["source"]:
        props["Source"] = {"select": {"name": source_value(row["source"])}}
    travel = transit_only(row["travel_display"], row["travel_lookup_failed"] or 0,
                           row["venue_name"])
    props["Travel from home"] = {"rich_text": [{"text": {"content": travel}}]}
    props["Verdict"] = {"select": {"name": "Undecided"}}
    place = build_place(row, venue_geo)
    if place is not None:
        props["Place"] = {"place": place}
    return props


def _h2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": text}}]}}


def _italic(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [
                {"text": {"content": text}, "annotations": {"italic": True}}
            ]}}


def _kv(label: str, value: str) -> dict:
    # "**Date:** Sat 19 April" as a paragraph with a bold prefix.
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [
                {"text": {"content": f"{label} "}, "annotations": {"bold": True}},
                {"text": {"content": value}},
            ]}}


def _source_line(row: sqlite3.Row, pe_lookup: dict | None) -> str:
    """Enhanced source line: 'Riverfront Arts Newsletter, 14 May' or plain 'Newsletter'."""
    detail = source_detail_text(row, pe_lookup or {}) if pe_lookup else None
    if detail:
        return detail
    return row["source"] or ""


def build_body(row: sqlite3.Row, pe_lookup: dict | None = None) -> list[dict]:
    """Render the standard §1.3 page body (source line, description, reasoning)."""
    travel_transit = transit_only(row["travel_display"], row["travel_lookup_failed"] or 0,
                                   row["venue_name"])
    score_line = f"+{row['score']}: {tier_label(row['llm_tier'])}"
    venue_bits = [row["venue_name"] or "Venue TBC"]
    if row["venue_postcode"]:
        venue_bits.append(row["venue_postcode"])
    venue_line = ", ".join(venue_bits)
    blocks = [
        _h2(_notion_text(row["name"])),
        _italic(_source_line(row, pe_lookup)),
        _kv("Date:",                           body_date(row["date"])),
        _kv("Time:",                           time_display(row["time"])),
        _kv("Venue:",                          venue_line),
        _kv(f"Travel from {TRAVEL_ORIGIN_LABEL}:", travel_transit),
        _kv("Cost:",                           row["cost"] or "TBC"),
        _kv("Score:",                          score_line),
        _kv("What it is:",                     _notion_text(get_description(row))),
    ]
    if row["url"]:
        blocks.append(_kv("Link:", row["url"]))
    gmail_url = source_detail_gmail_url(row)
    if gmail_url:
        blocks.append(_kv("Source email:", gmail_url))
    return blocks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SELECT_ELIGIBLE = """
SELECT *
FROM candidates
WHERE pipeline_state = 'ready_to_write'
ORDER BY date ASC, id ASC
"""


def run(db_path: Path, *, dry_run: bool, limit: int | None) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = list(conn.execute(SELECT_ELIGIBLE))
    if limit is not None:
        rows = rows[:limit]

    print(f"Eligible rows: {len(rows)}  (dry_run={dry_run})")
    if not rows:
        return 0

    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not token or not db_id:
        print("NOTION_TOKEN and NOTION_DATABASE_ID must be set.", file=sys.stderr)
        return 2

    notion = Notion(token)
    today_iso = date.today().isoformat()
    pe_lookup = fetch_newsletter_lookup(conn)

    written = 0
    skipped_dupe = 0
    errors = 0
    for row in rows:
        rid, name, iso_date = row["id"], row["name"], row["date"]
        print(f"\n[id={rid}] {row['llm_tier']} {iso_date}  {name!r}")

        try:
            bad_field = validate_no_artefacts(row)
            if bad_field:
                print(f"  BLOCKED by validator: field={bad_field!r} matches artefact regex"
                      ": fix upstream, do not strip")
                errors += 1
                continue

            venue_geo = get_venue_geo(conn, row["venue_name"], row["venue_postcode"])
            properties = build_properties(row, today_iso, venue_geo)
            sd = source_detail_rich_text(row, pe_lookup)
            if sd:
                properties["Source Detail"] = sd
            body_blocks = build_body(row, pe_lookup)
            place_ok = "Place" in properties

            if dry_run:
                print(f"  DRY RUN: would create page with {len(properties)} properties "
                      f"and {len(body_blocks)} body blocks")
                continue

            existing = notion.find_duplicate(db_id, name, iso_date)
            if existing:
                print(f"  DEDUP: existing page {existing}, marking pipeline_state=written, not overwriting")
                conn.execute(
                    "UPDATE candidates SET pipeline_state='written', notion_page_id=?, "
                    "notion_status='active', place_written=? WHERE id=?",
                    (existing, 1 if place_ok else 0, rid),
                )
                conn.commit()
                skipped_dupe += 1
                continue

            try:
                page_id = notion.create_page(db_id, properties, body_blocks)
            except RuntimeError as e:
                drop_keys = [k for k in ("Place", "Source Detail") if k in properties]
                if drop_keys:
                    print(f"  WARN: create_page failed: retrying without {', '.join(drop_keys)}")
                    print(f"  Error detail: {e}")
                    props_retry = {k: v for k, v in properties.items() if k not in drop_keys}
                    page_id = notion.create_page(db_id, props_retry, body_blocks)
                    if "Place" in drop_keys:
                        place_ok = False
                else:
                    raise

            conn.execute(
                "UPDATE candidates SET pipeline_state='written', notion_page_id=?, "
                "notion_status='active', place_written=? WHERE id=?",
                (page_id, 1 if place_ok else 0, rid),
            )
            conn.commit()
            print(f"  WROTE page {page_id}")
            written += 1

        except Exception as e:
            print(f"  FAILED [id={rid}]: {type(e).__name__}: {e}")
            conn.execute(
                "UPDATE candidates SET pipeline_state='write_failed' WHERE id=?",
                (rid,),
            )
            conn.commit()
            errors += 1

    print(f"\nSummary: wrote={written} dedup_skipped={skipped_dupe} errors={errors} "
          f"eligible={len(rows)}")
    if written > 0 or skipped_dupe > 0:
        return 0
    return 1 if errors > 0 else 0


# ---------------------------------------------------------------------------
# Smoke tests (run offline: no network): python3 write_notion.py --smoke-test
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

    check("batch_label",         batch_label("2026-04-30"), "30 Apr")
    check("batch_label single digit", batch_label("2026-05-03"), "3 May")
    check("tier_label Top Picks", tier_label("Top Picks"), "Top Picks")
    check("tier_label Recommended", tier_label("Recommended"), "Recommended")
    check("tier_label Borderline", tier_label("Borderline"), "Borderline")
    check("tier_label Couldn't Process", tier_label("Couldn't Process"), "Couldn't Process")
    check("body_date",          body_date("2026-04-19"), "Sun 19 April")
    check("month_name",         month_name("2026-04-19"), "April")
    check("travel normal",      transit_only("35 min (transit) · 18 min (cycle)", 0),
                                "35 min (transit)")
    check("no travel, no venue",
                                transit_only(None, 1, None),
                                "Venue unknown")
    check("no travel, venue known",
                                transit_only(None, 1, "Balderton Capital"),
                                "Route unavailable")
    check("no travel, venue literal 'Unknown'",
                                transit_only(None, 1, "Unknown"),
                                "Venue unknown")
    check("no travel, venue empty string",
                                transit_only(None, 0, ""),
                                "Venue unknown")
    check("source known passes through", source_value("Luma"), "Luma")
    check("source unknown falls back to Newsletter",
          source_value("Instagram"), "Newsletter")
    check("source None falls back to Newsletter", source_value(None), "Newsletter")

    # date_property_value: datetime when time is HH:MM, date-only otherwise.
    # BST window (Apr–Oct): expect +01:00. GMT window (Nov–Mar): expect +00:00.
    check("date_property BST datetime",
          date_property_value("2026-05-01", "19:00"),
          {"start": "2026-05-01T19:00:00+01:00"})
    check("date_property GMT datetime",
          date_property_value("2026-12-15", "18:30"),
          {"start": "2026-12-15T18:30:00+00:00"})
    check("date_property None time",
          date_property_value("2026-05-01", None),
          {"start": "2026-05-01"})
    check("date_property TBC time",
          date_property_value("2026-05-01", "TBC"),
          {"start": "2026-05-01"})
    check("date_property garbage time",
          date_property_value("2026-05-01", "evening"),
          {"start": "2026-05-01"})

    # time_of_day buckets + transparency states
    check("time_of_day morning",     time_of_day("09:30"), "Morning")
    check("time_of_day noon",        time_of_day("12:00"), "Afternoon")
    check("time_of_day late afternoon", time_of_day("16:59"), "Afternoon")
    check("time_of_day evening",     time_of_day("17:00"), "Evening")
    check("time_of_day late evening", time_of_day("21:30"), "Evening")
    check("time_of_day TBC",         time_of_day("TBC"), "TBC")
    check("time_of_day tbc lower",   time_of_day("tbc"), "TBC")
    check("time_of_day None",        time_of_day(None), "Not extracted")
    check("time_of_day empty",       time_of_day("   "), "Not extracted")
    check("time_of_day garbage",     time_of_day("evening"), "Not extracted")

    # time_display passthrough
    check("time_display real",       time_display("19:30"), "19:30")
    check("time_display TBC",        time_display("TBC"), "TBC")
    check("time_display None",       time_display(None), "Not extracted")
    check("time_display garbage",    time_display("about 7"), "Not extracted")

    # Validator
    class _FakeRow(dict):
        def __getitem__(self, key): return self.get(key)

    clean = _FakeRow(name="Jazz Night", description="Live music at The Spiral",
                     venue_name="The Spiral", cost="£10")
    check("validator clean", validate_no_artefacts(clean), None)

    dirty_signal = _FakeRow(name="Jazz Night", description="Signal 3 triggered here",
                            venue_name="The Spiral", cost=None)
    check("validator signal leak", validate_no_artefacts(dirty_signal), "description")

    dirty_past = _FakeRow(name="Jazz Night", description="past pattern: repeated",
                          venue_name="The Spiral", cost=None)
    check("validator past-pattern leak", validate_no_artefacts(dirty_past), "description")

    dirty_counted = _FakeRow(name="Jazz Night", description="already counted in total",
                             venue_name="The Spiral", cost=None)
    check("validator already-counted leak", validate_no_artefacts(dirty_counted), "description")

    dirty_name = _FakeRow(name="Signal 14 tennis event", description="A match",
                          venue_name="Court", cost=None)
    check("validator leak in name", validate_no_artefacts(dirty_name), "name")

    # build_place
    class _R(dict):
        def __getitem__(self, k): return self.get(k)

    geo_ok = _R(venue_lat=51.52, venue_lon=-0.09, formatted_address="Mill Ln, London EC1A 4EE, UK")
    geo_no_lat = _R(venue_lat=None, venue_lon=None, formatted_address=None)

    check("build_place May+ row with geo",
          build_place(_R(date="2026-05-10", venue_name="City Arts Centre", venue_postcode="EC1A 4EE"), geo_ok),
          {"name": "City Arts Centre", "address": "Mill Ln, London EC1A 4EE, UK",
           "lat": 51.52, "lon": -0.09})
    check("build_place April row (before cutoff) returns None",
          build_place(_R(date="2026-04-30", venue_name="City Arts Centre", venue_postcode="EC1A 4EE"), geo_ok),
          None)
    check("build_place May+ row but no geo returns None",
          build_place(_R(date="2026-05-10", venue_name="City Arts Centre", venue_postcode="EC1A 4EE"), None),
          None)
    check("build_place May+ row but venue_lat null returns None",
          build_place(_R(date="2026-05-10", venue_name="City Arts Centre", venue_postcode="EC1A 4EE"), geo_no_lat),
          None)
    check("build_place May+ row no venue_name returns None",
          build_place(_R(date="2026-05-10", venue_name=None, venue_postcode="EC1A 4EE"), geo_ok),
          None)
    check("build_place falls back to postcode when formatted_address is null",
          build_place(_R(date="2026-05-10", venue_name="Somewhere", venue_postcode="SE1 9AN"),
                      _R(venue_lat=51.5, venue_lon=-0.1, formatted_address=None)),
          {"name": "Somewhere", "address": "SE1 9AN", "lat": 51.5, "lon": -0.1})

    # API contract: Notion Place property requires {name, address, lat, lon}
    # (see developers.notion.com/reference/patch-page: same format for CREATE and PATCH)
    place_result = build_place(
        _R(date="2026-05-10", venue_name="City Arts Centre", venue_postcode="EC1A 4EE"), geo_ok)
    check("build_place API contract: keys match Notion Place spec",
          set(place_result.keys()) if place_result else set(),
          {"name", "address", "lat", "lon"})
    place_postcode = build_place(
        _R(date="2026-05-10", venue_name="Somewhere", venue_postcode="SE1 9AN"),
        _R(venue_lat=51.5, venue_lon=-0.1, formatted_address=None))
    check("build_place API contract: postcode fallback keys match spec",
          set(place_postcode.keys()) if place_postcode else set(),
          {"name", "address", "lat", "lon"})

    # Source detail helpers
    check("_format_email_date normal", _format_email_date("2026-05-14"), "14 May")
    check("_format_email_date None", _format_email_date(None), None)
    check("_format_email_date garbage", _format_email_date("not-a-date"), None)

    pe = {"msg123": ("Riverfront Arts", "2026-05-14"), "msg456": ("City Gallery", None)}

    nl_row = _R(source="Newsletter", source_snapshot="msg123")
    check("source_detail_text newsletter with data",
          source_detail_text(nl_row, pe), "Riverfront Arts Newsletter, 14 May")

    nl_no_date = _R(source="Newsletter", source_snapshot="msg456")
    check("source_detail_text newsletter no email_date",
          source_detail_text(nl_no_date, pe), "City Gallery Newsletter")

    nl_no_snapshot = _R(source="Newsletter", source_snapshot=None)
    check("source_detail_text newsletter no snapshot",
          source_detail_text(nl_no_snapshot, pe), None)

    meetup_row = _R(source="Meetup", source_snapshot="json-blob")
    check("source_detail_text meetup returns None",
          source_detail_text(meetup_row, pe), None)

    nl_missing = _R(source="Newsletter", source_snapshot="msg999")
    check("source_detail_text newsletter snapshot not in lookup",
          source_detail_text(nl_missing, pe), None)

    check("source_detail_rich_text returns dict",
          source_detail_rich_text(nl_row, pe),
          {"rich_text": [{"text": {"content": "Riverfront Arts Newsletter, 14 May"}}]})

    check("source_detail_rich_text None for meetup",
          source_detail_rich_text(meetup_row, pe), None)

    check("source_detail_gmail_url newsletter",
          source_detail_gmail_url(nl_row),
          "https://mail.google.com/mail/u/0/#all/msg123")

    check("source_detail_gmail_url no snapshot",
          source_detail_gmail_url(nl_no_snapshot), None)

    check("source_detail_gmail_url meetup",
          source_detail_gmail_url(meetup_row), None)

    # _source_line (body italic line)
    check("_source_line newsletter with data",
          _source_line(nl_row, pe), "Riverfront Arts Newsletter, 14 May")
    check("_source_line newsletter no snapshot",
          _source_line(nl_no_snapshot, pe), "Newsletter")
    check("_source_line meetup",
          _source_line(meetup_row, pe), "Meetup")
    check("_source_line no lookup",
          _source_line(nl_row, None), "Newsletter")

    # build_body with pe_lookup: newsletter row
    nl_full = _R(id=3, name="Outdoor Cinema", date="2026-06-15", time="20:00",
                 llm_tier="Recommended", score=30, venue_name="Riverfront Arts",
                 venue_postcode="EC1A 4EE", cost="£12", source="Newsletter",
                 source_snapshot="msg123",
                 url="https://example.com/event",
                 description="Open-air film screening.",
                 travel_display="25 min (transit)", travel_lookup_failed=0)
    body = build_body(nl_full, pe)
    body_texts = []
    for b in body:
        if b["type"] == "paragraph":
            for seg in b["paragraph"].get("rich_text", []):
                body_texts.append(seg.get("text", {}).get("content", ""))
    check("build_body has source email line",
          any("mail.google.com" in t for t in body_texts), True)
    italic_block = body[1]
    italic_text = italic_block["paragraph"]["rich_text"][0]["text"]["content"]
    check("build_body italic shows sender",
          "Riverfront Arts Newsletter" in italic_text, True)

    # build_body without pe_lookup: source email still appears (uses source_snapshot directly)
    body_no_pe = build_body(nl_full)
    body_no_pe_texts = []
    for b in body_no_pe:
        if b["type"] == "paragraph":
            for seg in b["paragraph"].get("rich_text", []):
                body_no_pe_texts.append(seg.get("text", {}).get("content", ""))
    check("build_body no pe_lookup, source email still shown (snapshot available)",
          any("mail.google.com" in t for t in body_no_pe_texts), True)
    # But italic line falls back to plain "Newsletter" without lookup
    no_pe_italic = body_no_pe[1]["paragraph"]["rich_text"][0]["text"]["content"]
    check("build_body no pe_lookup, italic falls back to Newsletter",
          no_pe_italic, "Newsletter")

    # build_body meetup row: no source email
    meetup_full = _R(id=4, name="Tech Meetup", date="2026-06-20", time="18:30",
                     llm_tier="Recommended", score=25, venue_name="The Workspace",
                     venue_postcode="W1T 1JY", cost="Free", source="Meetup",
                     source_snapshot='{"json": true}',
                     url="https://meetup.com/event",
                     description="Monthly tech meetup.",
                     travel_display="20 min (transit)", travel_lookup_failed=0)
    body_meetup = build_body(meetup_full, pe)
    meetup_body_texts = []
    for b in body_meetup:
        if b["type"] == "paragraph":
            for seg in b["paragraph"].get("rich_text", []):
                meetup_body_texts.append(seg.get("text", {}).get("content", ""))
    check("build_body meetup has no source email",
          any("mail.google.com" in t for t in meetup_body_texts), False)

    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DB_PATH), type=Path)
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be written, no API calls")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--smoke-test", action="store_true",
                   help="Run offline unit tests of derivations + validator")
    args = p.parse_args()

    if args.smoke_test:
        return _smoke_tests()

    return run(args.db, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
