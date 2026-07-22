#!/usr/bin/env python3
"""
Travel-time lookup for scored candidates missing a travel_display.

Deterministic. No LLM. Reads GOOGLE_MAPS_API_KEY from env.
Origin is your home postcode (ER_HOME_POSTCODE, see .env.example); three
modes (transit/bicycle/walk), 90-day venues cache, batched ComputeRouteMatrix
calls.

Usage:
    GOOGLE_MAPS_API_KEY=AIza... python3 travel_time.py [--dry-run] [--limit N]

Eligible row:
    tier IN ('Top Picks','Recommended','Borderline','Couldn''t Process')
    AND travel_display IS NULL
    AND travel_lookup_failed = 0
    AND pipeline_state NOT IN ('written','vetoed','llm_rejected','duplicate')

Pre-passes (run before the main lookup, zero API calls):
    1. Cache population: fills venues cache gaps from candidate travel data.
       Venues with known travel times in the candidates table but no cache
       entry get one, so the main flow's cache check catches them.
    2. Failed-candidate recovery: candidates flagged travel_lookup_failed=1
       are re-checked against the (now enriched) venues cache. If a hit
       exists, travel data is applied and the flag is reset.

Three-tier venues cache (checked before any Routes API call):
    1. venue_name  (case-insensitive, 90-day TTL)
    2. venue_postcode (case-insensitive, 90-day TTL)
    3. google_place_id (geocode first, then match: catches different names
       for the same physical venue, e.g. "Rich Mix" vs "RichMix")

Uncached venues are batched into ComputeRouteMatrix calls (one per mode).
Results are written to the venues cache and to every candidate row that
shares the venue. On a place_id cache hit, an alias entry is stored so
future runs match by name directly. If all three modes fail for a venue,
the candidate rows are flagged travel_lookup_failed=1.

travel_display format (matching existing DB values):
    "35 min (transit)"                             : always
    " · 18 min (cycle)" appended                   : only if cycle ≤25 min
    " · 14 min (walk)" appended                    : only if walk   ≤20 min
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
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.config import (  # noqa: E402
    GEOCODE_REGION,
    GEOCODE_SUFFIX,
    TRAVEL_ORIGIN_POSTCODE,
)

ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
ORIGIN = f"{TRAVEL_ORIGIN_POSTCODE}, {GEOCODE_SUFFIX}"

CACHE_TTL_DAYS = 90
CYCLE_DISPLAY_MAX = 25
WALK_DISPLAY_MAX = 20
BATCH_SIZE = 1  # 1 venue at a time: batched calls cause spurious ROUTE_NOT_FOUND for transit.
# Routes API free tier: 10 req/min. We fire 3 calls (one per mode) per batch, so 3 batches
# back-to-back burns the full minute's quota. Sleep between batches keeps us under the limit.
# 20s × 3 calls = 15s per-batch ceiling: safe margin under 10 req/min. Only applies between
# API batches; cache hits are local and not paced.
BATCH_PAUSE_SEC = 20


# ---------------------------------------------------------------------------
# Venue address resolution
# ---------------------------------------------------------------------------

# Best-effort UK-postcode pattern. If a description contains a postcode in
# this format it's added to the geocoding query for precision. For cities
# with other postal formats this simply never matches: geocoding still
# works from "venue name, {GEOCODE_SUFFIX}", so it degrades gracefully.
# UK forms like "SE1 9AN", "EC3N 1NU", "W1A 0AX".
UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s+\d[A-Z]{2}\b", re.IGNORECASE)

# Tokens that are not real venue/postcode values: skip these when building
# the geocoding query so we don't pollute it with "unknown, London, UK".
_USELESS = {"", "unknown", "tbc", "tba", "n/a", "london", "greater, london", "greater"}


def venue_address(venue_name: str | None,
                  venue_postcode: str | None,
                  description: str | None = None) -> str | None:
    """Build a rich geocoding query from every useful venue signal.

    Google Maps handles redundant context well: "The Corner Tavern,
    40 Market Row, EC1A 4FF, London, UK" geocodes better than any
    single part alone. We only fall back to description for a postcode if
    neither venue_name nor venue_postcode contains one.
    """
    name = (venue_name or "").strip()
    pc = (venue_postcode or "").strip()
    desc = (description or "").strip()

    if name.lower() in _USELESS:
        name = ""
    if pc.lower() in _USELESS:
        pc = ""

    parts: list[str] = []
    if name:
        parts.append(name)
    if pc and pc.lower() != name.lower():
        parts.append(pc)

    # If no postcode is present in what we've collected, try the description.
    combined = " ".join(parts)
    if not UK_POSTCODE_RE.search(combined) and desc:
        m = UK_POSTCODE_RE.search(desc)
        if m:
            parts.append(m.group(0).upper())

    if not parts:
        return None
    return ", ".join(parts) + f", {GEOCODE_SUFFIX}"


def venue_cache_key(row: sqlite3.Row) -> tuple[str | None, str | None]:
    name = (row["venue_name"] or "").strip() or None
    postcode = (row["venue_postcode"] or "").strip() or None
    return name, postcode


# ---------------------------------------------------------------------------
# Routes API v2: ComputeRouteMatrix
# ---------------------------------------------------------------------------

MODES = ("TRANSIT", "BICYCLE", "WALK")


# ---------------------------------------------------------------------------
# Geocoding API (for Notion Place property)
# ---------------------------------------------------------------------------

def _geocode(api_key: str, address: str) -> tuple[float | None, float | None, str | None, str | None]:
    """Return (lat, lon, google_place_id, formatted_address) or all None on failure.

    Notion's Place property requires lat/lon explicitly (the public API does
    not geocode server-side). One call per unique venue, cached alongside
    travel-time results in the venues table.
    """
    import urllib.parse
    q = urllib.parse.quote(address)
    url = f"{GEOCODE_URL}?address={q}&key={api_key}"
    if GEOCODE_REGION:
        url += f"&region={urllib.parse.quote(GEOCODE_REGION)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"    Geocode FAILED for {address!r}: {e}")
        return None, None, None, None
    if data.get("status") != "OK" or not data.get("results"):
        print(f"    Geocode returned {data.get('status')!r} for {address!r}")
        return None, None, None, None
    top = data["results"][0]
    loc = top.get("geometry", {}).get("location", {})
    return (loc.get("lat"), loc.get("lng"),
            top.get("place_id"), top.get("formatted_address"))


def _arrival_iso_next_sunday() -> str:
    """Next Sunday 19:00 local London, expressed as ISO UTC.
    19:00 BST == 18:00 UTC during April–October. We use 18:00 UTC year-round
    as a "close-enough" reference: the venues cache is venue-keyed, not
    time-keyed, so this affects the queried time, not cache hit rate.
    """
    today = datetime.now(timezone.utc).date()
    days_ahead = (6 - today.weekday()) % 7  # Mon=0 … Sun=6
    if days_ahead == 0:
        days_ahead = 7
    target = datetime.combine(today + timedelta(days=days_ahead),
                              datetime.min.time(), tzinfo=timezone.utc).replace(hour=18)
    return target.isoformat().replace("+00:00", "Z")


def _call_routes(api_key: str, destinations: list[str], mode: str,
                 *, retry_on_rate_limit: bool = True) -> list[dict]:
    """Returns a list of response-matrix elements (one per destination).

    Retries once on HTTP 429 (rate limit) after a short backoff. Other HTTP
    errors raise immediately: callers handle per-mode failures so one
    mode's error doesn't punish the other two modes.
    """
    import time

    payload: dict = {
        "origins": [{"waypoint": {"address": ORIGIN}}],
        "destinations": [{"waypoint": {"address": d}} for d in destinations],
        "travelMode": mode,
    }
    if mode == "TRANSIT":
        payload["arrivalTime"] = _arrival_iso_next_sunday()

    req = urllib.request.Request(ROUTES_URL, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Goog-Api-Key", api_key)
    req.add_header("X-Goog-FieldMask",
                   "originIndex,destinationIndex,duration,status,condition")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry_on_rate_limit:
            print(f"    Routes[{mode}] rate-limited; backing off 8s and retrying once")
            time.sleep(8)
            return _call_routes(api_key, destinations, mode, retry_on_rate_limit=False)
        raise RuntimeError(f"Routes {mode} HTTP {e.code}: {e.read().decode()}") from None


def _seconds_to_min(seconds_str: str | None) -> int | None:
    if not seconds_str:
        return None
    s = seconds_str.rstrip("s")
    try:
        return max(1, round(int(s) / 60))
    except ValueError:
        return None


def _lookup_batch(api_key: str, venues: list[tuple[str, str]]
                  ) -> dict[str, dict[str, int | None]]:
    """venues: list of (venue_key, address). Returns {venue_key: {mode: minutes or None}}.

    Per-mode isolation: if TRANSIT's API call errors out (rate limit, 5xx, etc),
    BICYCLE and WALK still try and their results are kept. Previously a single
    API error on one mode flagged every venue in the batch as fully failed.
    """
    addresses = [addr for _, addr in venues]
    keys = [k for k, _ in venues]

    RATE_LIMIT_SENTINEL = -1
    results: dict[str, dict[str, int | None]] = {k: {} for k in keys}
    for mode in MODES:
        print(f"    Routes[{mode}] batch: {len(addresses)} destinations")
        try:
            matrix = _call_routes(api_key, addresses, mode)
        except RuntimeError as e:
            print(f"    Routes[{mode}] FAILED ({e}); marking as rate-limited (won't cache)")
            for k in keys:
                results[k].setdefault(mode, RATE_LIMIT_SENTINEL)
            continue
        for elem in matrix:
            didx = elem.get("destinationIndex")
            if didx is None or didx >= len(keys):
                continue
            key = keys[didx]
            status_code = elem.get("status", {}).get("code", 0)
            condition = elem.get("condition")
            if status_code != 0 or condition == "ROUTE_NOT_FOUND":
                results[key][mode] = None
            else:
                results[key][mode] = _seconds_to_min(elem.get("duration"))
    return results


# ---------------------------------------------------------------------------
# Display string
# ---------------------------------------------------------------------------

def travel_display(transit: int | None, cycle: int | None, walk: int | None) -> str | None:
    """Show modes within practical thresholds only.

    Cycle > 25 min and walk > 20 min are suppressed: not useful for
    the card. Returns None when no mode is within threshold, which means
    "Route unavailable" (a successful lookup with no practical option).
    """
    parts: list[str] = []
    if transit is not None:
        parts.append(f"{transit} min (transit)")
    if cycle is not None and cycle <= CYCLE_DISPLAY_MAX:
        parts.append(f"{cycle} min (cycle)")
    if walk is not None and walk <= WALK_DISPLAY_MAX:
        parts.append(f"{walk} min (walk)")
    return " · ".join(parts) if parts else None


def _has_any_data(transit: int | None, cycle: int | None, walk: int | None) -> bool:
    return transit is not None or cycle is not None or walk is not None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive-only migration: add lat/lon/place_id/formatted_address to venues
    if missing. Safe to run every time."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(venues)")}
    for col, sql in (
        ("venue_lat",         "ALTER TABLE venues ADD COLUMN venue_lat REAL"),
        ("venue_lon",         "ALTER TABLE venues ADD COLUMN venue_lon REAL"),
        ("google_place_id",   "ALTER TABLE venues ADD COLUMN google_place_id TEXT"),
        ("formatted_address", "ALTER TABLE venues ADD COLUMN formatted_address TEXT"),
    ):
        if col not in cols:
            print(f"Migrating venues table: adding {col}")
            conn.execute(sql)
    idx = {row[1] for row in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='venues'"
    )}
    if "idx_venues_place_id" not in idx:
        print("Migrating venues table: adding idx_venues_place_id")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_venues_place_id "
            "ON venues(google_place_id)"
        )
    conn.commit()


SELECT_ELIGIBLE = """
SELECT id, name, date, venue_name, venue_postcode, description
FROM candidates
WHERE tier IN ('Top Picks','Recommended','Borderline','Couldn''t Process')
  AND travel_display IS NULL
  AND travel_lookup_failed = 0
  AND pipeline_state NOT IN ('written','vetoed','llm_rejected','duplicate')
  AND COALESCE(end_date, date) >= date('now')
ORDER BY date ASC, id ASC
"""

SELECT_BACKFILL = """
SELECT id, name, date, venue_name, venue_postcode, description
FROM candidates
WHERE notion_status = 'active'
  AND travel_display IS NULL
  AND travel_lookup_failed = 0
  AND COALESCE(end_date, date) >= date('now')
ORDER BY date ASC, id ASC
"""

SELECT_BACKFILL_ALL = """
SELECT id, name, date, venue_name, venue_postcode, description
FROM candidates
WHERE notion_status = 'active'
  AND travel_display IS NULL
  AND travel_lookup_failed = 0
ORDER BY date ASC, id ASC
"""


def fetch_cache(conn: sqlite3.Connection,
                name: str | None,
                postcode: str | None,
                within_days: int) -> sqlite3.Row | None:
    cutoff_iso = (date.today() - timedelta(days=within_days)).isoformat()
    if name:
        r = conn.execute(
            "SELECT * FROM venues WHERE venue_name = ? COLLATE NOCASE "
            "AND lookup_date >= ? ORDER BY lookup_date DESC LIMIT 1",
            (name, cutoff_iso),
        ).fetchone()
        if r:
            return r
    if postcode:
        r = conn.execute(
            "SELECT * FROM venues WHERE venue_postcode = ? COLLATE NOCASE "
            "AND lookup_date >= ? ORDER BY lookup_date DESC LIMIT 1",
            (postcode, cutoff_iso),
        ).fetchone()
        if r:
            return r
    return None


def fetch_cache_by_place_id(conn: sqlite3.Connection,
                            place_id: str,
                            within_days: int) -> sqlite3.Row | None:
    """Match by resolved physical location when name/postcode differ across sources."""
    cutoff_iso = (date.today() - timedelta(days=within_days)).isoformat()
    return conn.execute(
        "SELECT * FROM venues WHERE google_place_id = ? AND lookup_date >= ? "
        "ORDER BY lookup_date DESC LIMIT 1",
        (place_id, cutoff_iso),
    ).fetchone()


def apply_result(conn: sqlite3.Connection,
                 candidate_ids: list[int],
                 transit: int | None,
                 cycle: int | None,
                 walk: int | None) -> None:
    display = travel_display(transit, cycle, walk)
    has_data = _has_any_data(transit, cycle, walk)
    if not has_data:
        for cid in candidate_ids:
            conn.execute("UPDATE candidates SET travel_lookup_failed=1 WHERE id=?", (cid,))
            conn.execute("UPDATE candidates SET pipeline_state='ready_to_write' WHERE id=? AND pipeline_state='pending_travel'", (cid,))
    elif display is None:
        # Data exists but all modes above display thresholds: store raw
        # values but flag as failed to prevent infinite reprocessing
        for cid in candidate_ids:
            conn.execute(
                "UPDATE candidates SET travel_transit_min=?, travel_cycle_min=?, "
                "travel_walk_min=?, travel_lookup_failed=1 WHERE id=?",
                (transit, cycle, walk, cid),
            )
            conn.execute("UPDATE candidates SET pipeline_state='ready_to_write' WHERE id=? AND pipeline_state='pending_travel'", (cid,))
    else:
        for cid in candidate_ids:
            conn.execute(
                "UPDATE candidates SET travel_transit_min=?, travel_cycle_min=?, "
                "travel_walk_min=?, travel_display=?, travel_lookup_failed=0 WHERE id=?",
                (transit, cycle, walk, display, cid),
            )
            conn.execute("UPDATE candidates SET pipeline_state='ready_to_write' WHERE id=? AND pipeline_state='pending_travel'", (cid,))

    conn.commit()


def _populate_cache_from_candidates(conn: sqlite3.Connection,
                                    dry_run: bool) -> int:
    """Fill venues cache gaps from existing candidate travel data.

    Finds candidates with travel data whose venue_name has no matching venues
    cache entry. Inserts cache entries so the main flow's three-tier lookup
    catches venues we've already computed travel for.
    """
    rows = conn.execute("""
        SELECT venue_name, venue_postcode, run_date,
               travel_transit_min, travel_cycle_min, travel_walk_min
        FROM candidates
        WHERE travel_display IS NOT NULL
          AND venue_name IS NOT NULL AND TRIM(venue_name) != ''
          AND NOT EXISTS (
              SELECT 1 FROM venues v
              WHERE LOWER(v.venue_name) = LOWER(candidates.venue_name)
          )
        ORDER BY id DESC
    """).fetchall()

    if not rows:
        return 0

    seen: set[str] = set()
    inserted = 0
    for row in rows:
        key = row["venue_name"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        if fetch_cache(conn, None, row["venue_postcode"], CACHE_TTL_DAYS):
            continue
        if not dry_run:
            # lookup_date = the candidate's run_date (when its travel was
            # actually computed), NOT today: stamping today reset the 90-day
            # TTL and re-presented stale times as fresh (audit P2). Rows with
            # no run_date get an already-expired date so they re-fetch.
            aged = (date.today() - timedelta(days=CACHE_TTL_DAYS + 1)).isoformat()
            conn.execute(
                "INSERT INTO venues (venue_name, venue_postcode, travel_transit_min, "
                "travel_cycle_min, travel_walk_min, lookup_date) VALUES (?, ?, ?, ?, ?, ?)",
                (row["venue_name"], row["venue_postcode"] or "",
                 row["travel_transit_min"], row["travel_cycle_min"],
                 row["travel_walk_min"], row["run_date"] or aged),
            )
        inserted += 1

    if inserted:
        if not dry_run:
            conn.commit()
        print(f"Cache populated from candidates: {inserted} venues backfilled"
              f"{' (dry-run)' if dry_run else ''}")
    return inserted


def _recover_failed_from_cache(conn: sqlite3.Connection,
                               dry_run: bool) -> int:
    """Recover travel_lookup_failed candidates from the venues cache.

    Candidates flagged travel_lookup_failed=1 are permanently excluded from
    SELECT_ELIGIBLE. But the venue may have been successfully looked up later
    for a different candidate. This pass checks the cache and applies any
    available data: zero API calls.
    """
    rows = list(conn.execute(
        "SELECT id, venue_name, venue_postcode, pipeline_state "
        "FROM candidates "
        "WHERE travel_lookup_failed = 1 AND travel_display IS NULL"
    ))
    if not rows:
        return 0

    by_venue: dict[tuple[str | None, str | None], dict] = {}
    for row in rows:
        name, postcode = venue_cache_key(row)
        key = (name.lower() if name else None,
               postcode.upper().replace(" ", "") if postcode else None)
        slot = by_venue.setdefault(
            key, {"ids": [], "name": name, "postcode": postcode})
        slot["ids"].append(row["id"])

    recovered = 0
    for _key, slot in by_venue.items():
        hit = fetch_cache(conn, slot["name"], slot["postcode"], CACHE_TTL_DAYS)
        if hit is None or not _has_any_data(
            hit["travel_transit_min"], hit["travel_cycle_min"],
            hit["travel_walk_min"],
        ):
            continue
        transit = hit["travel_transit_min"]
        cycle = hit["travel_cycle_min"]
        walk = hit["travel_walk_min"]
        display = travel_display(transit, cycle, walk)
        if display is None:
            continue
        print(f"  recover: venue={slot['name']!r} → "
              f"{transit}/{cycle}/{walk} ({len(slot['ids'])} candidates)")
        if not dry_run:
            apply_result(conn, slot["ids"], transit, cycle, walk)
        recovered += len(slot["ids"])

    if recovered:
        print(f"Cache recovery: {recovered} candidates recovered"
              f"{' (dry-run)' if dry_run else ''}")
    return recovered


def run(db_path: Path, *, dry_run: bool, limit: int | None,
        backfill: bool = False, include_past: bool = False) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    # Pre-passes: populate must run before recover (recover uses enriched cache)
    _populate_cache_from_candidates(conn, dry_run)
    recovered = _recover_failed_from_cache(conn, dry_run)
    if recovered:
        print()

    if backfill:
        fixed = conn.execute(
            "UPDATE candidates SET travel_display = NULL "
            "WHERE travel_display = 'Travel time pending'"
        ).rowcount
        conn.commit()
        print(f"Backfill: cleared 'Travel time pending' from {fixed} rows")

    if backfill and include_past:
        query = SELECT_BACKFILL_ALL
    elif backfill:
        query = SELECT_BACKFILL
    else:
        query = SELECT_ELIGIBLE
    rows = list(conn.execute(query))
    if limit is not None:
        rows = rows[:limit]
    print(f"Eligible candidates needing travel lookup: {len(rows)}  (dry_run={dry_run})")
    if not rows:
        return 0

    # Group candidate IDs by venue_key.
    # venue_key = (name_lower_or_None, postcode_lower_or_None): ensures same venue spelled
    # consistently maps to one cache entry per run.
    by_venue: dict[tuple[str | None, str | None], dict] = {}
    for row in rows:
        name, postcode = venue_cache_key(row)
        key = (name.lower() if name else None,
               postcode.upper().replace(" ", "") if postcode else None)
        slot = by_venue.setdefault(
            key,
            {"ids": [], "name": name, "postcode": postcode, "description": row["description"]},
        )
        slot["ids"].append(row["id"])

    print(f"Unique venues: {len(by_venue)}")

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")

    # Cache lookup pass (three tiers):
    #   1. venue_name (case-insensitive)
    #   2. venue_postcode (case-insensitive)
    #   3. geocode → google_place_id (catches different names for same physical venue)
    # Cached venues missing lat/lon get a lazy geocode backfill in the same pass.
    uncached: list[tuple[tuple, dict]] = []
    seen_place_ids: dict[str, dict] = {}
    for key, slot in by_venue.items():
        hit = fetch_cache(conn, slot["name"], slot["postcode"], CACHE_TTL_DAYS)
        if hit is not None:
            print(f"  cache HIT  venue={slot['name']!r}: "
                  f"{hit['travel_transit_min']}/{hit['travel_cycle_min']}/"
                  f"{hit['travel_walk_min']}")
            if not dry_run:
                apply_result(conn, slot["ids"],
                             hit["travel_transit_min"],
                             hit["travel_cycle_min"],
                             hit["travel_walk_min"])
                if hit["venue_lat"] is None and api_key:
                    addr = venue_address(slot["name"], slot["postcode"], slot.get("description"))
                    if addr:
                        lat, lon, pid, fmt = _geocode(api_key, addr)
                        if lat is not None:
                            conn.execute(
                                "UPDATE venues SET venue_lat=?, venue_lon=?, "
                                "google_place_id=?, formatted_address=? WHERE id=?",
                                (lat, lon, pid, fmt, hit["id"]),
                            )
                            conn.commit()
                            print(f"    geocoded (backfill)  lat={lat} lon={lon}")
        else:
            # Tier 3: geocode and check by google_place_id.
            if api_key:
                addr = venue_address(slot["name"], slot["postcode"], slot.get("description"))
                if addr:
                    lat, lon, pid, fmt = _geocode(api_key, addr)
                    slot["geocode"] = (lat, lon, pid, fmt)
                    if pid:
                        # Within-run dedup: another venue in this batch already
                        # resolved to the same physical location.
                        if pid in seen_place_ids:
                            seen_place_ids[pid]["ids"].extend(slot["ids"])
                            print(f"  within-run DEDUP  venue={slot['name']!r} "
                                  f"→ merged with {seen_place_ids[pid]['name']!r}")
                            continue
                        place_hit = fetch_cache_by_place_id(conn, pid, CACHE_TTL_DAYS)
                        if place_hit is not None:
                            print(f"  cache HIT (place_id)  venue={slot['name']!r} "
                                  f"→ matched {place_hit['venue_name']!r}: "
                                  f"{place_hit['travel_transit_min']}/"
                                  f"{place_hit['travel_cycle_min']}/"
                                  f"{place_hit['travel_walk_min']}")
                            if not dry_run:
                                apply_result(conn, slot["ids"],
                                             place_hit["travel_transit_min"],
                                             place_hit["travel_cycle_min"],
                                             place_hit["travel_walk_min"])
                                conn.execute(
                                    "INSERT INTO venues (venue_name, venue_postcode, "
                                    "travel_transit_min, travel_cycle_min, travel_walk_min, "
                                    "lookup_date, venue_lat, venue_lon, google_place_id, "
                                    "formatted_address) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (slot["name"] or "UNKNOWN", slot["postcode"],
                                     place_hit["travel_transit_min"],
                                     place_hit["travel_cycle_min"],
                                     place_hit["travel_walk_min"],
                                     place_hit["lookup_date"],
                                     lat, lon, pid, fmt),
                                )
                                conn.commit()
                            seen_place_ids[pid] = slot
                            continue
                        seen_place_ids[pid] = slot
            uncached.append((key, slot))

    print(f"Uncached venues needing API lookup: {len(uncached)}")
    if not uncached:
        return 0

    if not api_key:
        print("GOOGLE_MAPS_API_KEY must be set.", file=sys.stderr)
        return 2

    # Batch API lookups, BATCH_SIZE destinations per mode-call.
    import time as _time
    for start in range(0, len(uncached), BATCH_SIZE):
        if start > 0:
            print(f"  pausing {BATCH_PAUSE_SEC}s between batches to stay under the "
                  f"Routes API rate limit")
            _time.sleep(BATCH_PAUSE_SEC)
        batch = uncached[start:start + BATCH_SIZE]
        pairs = []  # (venue_key, address)
        addressable_slots = []
        unaddressable_slots = []
        for key, slot in batch:
            addr = venue_address(slot["name"], slot["postcode"], slot.get("description"))
            if addr is None:
                unaddressable_slots.append(slot)
            else:
                pairs.append((str(key), addr))
                addressable_slots.append(slot)

        for slot in unaddressable_slots:
            print(f"  venue has no usable name/postcode; flagging {slot['ids']} as failed")
            if not dry_run:
                apply_result(conn, slot["ids"], None, None, None)

        if not pairs:
            continue

        print(f"  batch {start // BATCH_SIZE + 1}: {len(pairs)} venues × 3 modes")
        if dry_run:
            for slot in addressable_slots:
                print(f"    DRY: would look up {slot['name']!r} ({slot['postcode']})")
            continue

        try:
            results = _lookup_batch(api_key, pairs)
        except RuntimeError as e:
            print(f"  ERROR calling Routes API: {e}")
            # All candidates in this batch flagged failed so we don't retry indefinitely.
            for slot in addressable_slots:
                apply_result(conn, slot["ids"], None, None, None)
            continue

        SENTINEL = -1
        for (pkey, _addr), slot in zip(pairs, addressable_slots, strict=False):
            modes = results.get(pkey, {})
            raw_transit = modes.get("TRANSIT")
            raw_cycle   = modes.get("BICYCLE")
            raw_walk    = modes.get("WALK")

            has_rate_limit = raw_transit == SENTINEL or raw_cycle == SENTINEL or raw_walk == SENTINEL
            transit = None if raw_transit == SENTINEL else raw_transit
            cycle   = None if raw_cycle == SENTINEL else raw_cycle
            walk    = None if raw_walk == SENTINEL else raw_walk

            print(f"    venue={slot['name']!r}  transit={transit} cycle={cycle} walk={walk}"
                  f"{'  (rate-limited mode: NOT caching)' if has_rate_limit else ''}")

            if not has_rate_limit and (transit is not None or cycle is not None or walk is not None):
                geo = slot.get("geocode")
                if geo:
                    lat, lon, pid, fmt = geo
                else:
                    addr = venue_address(slot["name"], slot["postcode"], slot.get("description"))
                    lat = lon = pid = fmt = None
                    if addr:
                        lat, lon, pid, fmt = _geocode(api_key, addr)
                conn.execute(
                    "INSERT INTO venues (venue_name, venue_postcode, travel_transit_min, "
                    "travel_cycle_min, travel_walk_min, lookup_date, "
                    "venue_lat, venue_lon, google_place_id, formatted_address) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (slot["name"] or "UNKNOWN", slot["postcode"],
                     transit, cycle, walk, date.today().isoformat(),
                     lat, lon, pid, fmt),
                )

            if has_rate_limit:
                print("      skipping apply: will retry next run")
            else:
                apply_result(conn, slot["ids"], transit, cycle, walk)

    return 0


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------

def _smoke_tests() -> int:
    ok = True
    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    check("display transit only",
          travel_display(35, None, None),
          "35 min (transit)")
    check("display transit + cycle ≤25",
          travel_display(26, 12, None),
          "26 min (transit) · 12 min (cycle)")
    check("display transit + cycle 26 (cutoff)",
          travel_display(26, 26, None),
          "26 min (transit)")
    check("display transit + cycle 25 (boundary)",
          travel_display(26, 25, None),
          "26 min (transit) · 25 min (cycle)")
    check("display with walk ≤20",
          travel_display(30, 10, 15),
          "30 min (transit) · 10 min (cycle) · 15 min (walk)")
    check("display with walk 21 (excluded)",
          travel_display(30, 10, 21),
          "30 min (transit) · 10 min (cycle)")
    check("display no transit but cycle+walk work",
          travel_display(None, 12, 18),
          "12 min (cycle) · 18 min (walk)")
    check("display no transit, walk only",
          travel_display(None, None, 15),
          "15 min (walk)")
    check("display all three None returns None",
          travel_display(None, None, None),
          None)
    check("display above-threshold cycle/walk returns None",
          travel_display(None, 44, 126),
          None)
    check("display above-threshold walk only returns None",
          travel_display(None, None, 45),
          None)
    check("display transit shown even when cycle/walk above threshold",
          travel_display(35, 44, 126),
          "35 min (transit)")

    check("_seconds_to_min 2886s", _seconds_to_min("2886s"), 48)
    check("_seconds_to_min None", _seconds_to_min(None), None)
    check("_seconds_to_min zero-ish", _seconds_to_min("30s"), 1)  # clamp to >=1

    # venue_address: combines every useful signal; no longer picks one and discards the rest
    check("venue_address name + postcode combined",
          venue_address("The Corner Tavern", "40 Market Row, EC1A 4FF, London"),
          "The Corner Tavern, 40 Market Row, EC1A 4FF, London, London, UK")
    check("venue_address clean postcode",
          venue_address("Something Place", "SE1 9DT"),
          "Something Place, SE1 9DT, London, UK")
    check("venue_address name only",
          venue_address("City Arts Centre", None),
          "City Arts Centre, London, UK")
    check("venue_address both None",
          venue_address(None, None),
          None)
    check("venue_address empty-string postcode falls back to name",
          venue_address("City Arts Centre", "   "),
          "City Arts Centre, London, UK")
    check("venue_address name='Unknown' treated as empty",
          venue_address("Unknown", None),
          None)
    check("venue_address postcode pulled from description when missing",
          venue_address("The Lodge Tavern", None,
                        "Come along to the Broadway N16 7XB for the social."),
          "The Lodge Tavern, N16 7XB, London, UK")
    check("venue_address postcode NOT pulled from desc when already present in venue",
          venue_address("Place SE1 9AN", None,
                        "Other postcode EC2A 4PS mentioned in description"),
          "Place SE1 9AN, London, UK")
    check("venue_address same name and postcode not duplicated",
          venue_address("City Arts Centre", "City Arts Centre"),
          "City Arts Centre, London, UK")

    iso = _arrival_iso_next_sunday()
    print(f"  INFO next-Sunday arrival ISO: {iso}")
    if "T18:00:00" not in iso or not iso.endswith("Z"):
        ok = False; print(f"  FAIL arrival format unexpected: {iso}")
    else:
        print("  OK  arrival format")

    # --- Cache lookup tests (in-memory DB) ---
    tdb = sqlite3.connect(":memory:")
    tdb.row_factory = sqlite3.Row
    tdb.execute("""CREATE TABLE venues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, venue_name TEXT NOT NULL,
        venue_postcode TEXT, travel_transit_min INTEGER, travel_cycle_min INTEGER,
        travel_walk_min INTEGER, lookup_date TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        venue_lat REAL, venue_lon REAL, google_place_id TEXT,
        formatted_address TEXT)""")
    tdb.execute(
        "INSERT INTO venues (venue_name, venue_postcode, travel_transit_min, "
        "travel_cycle_min, travel_walk_min, lookup_date, google_place_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Riverside Gallery", "SE1 8XZ", 48, 53, 171, date.today().isoformat(),
         "ChIJ_test_gallery"),
    )
    tdb.commit()

    check("cache: case-insensitive name match",
          fetch_cache(tdb, "riverside gallery", None, 90) is not None, True)
    check("cache: case-insensitive name match (UPPER)",
          fetch_cache(tdb, "RIVERSIDE GALLERY", None, 90) is not None, True)
    check("cache: case-insensitive postcode match",
          fetch_cache(tdb, None, "se1 8xz", 90) is not None, True)
    check("cache: exact miss on different name",
          fetch_cache(tdb, "The Annex", None, 90) is None, True)
    check("cache: place_id lookup hit",
          fetch_cache_by_place_id(tdb, "ChIJ_test_gallery", 90) is not None, True)
    check("cache: place_id lookup miss",
          fetch_cache_by_place_id(tdb, "ChIJ_nonexistent", 90) is None, True)

    stale = date.today() - timedelta(days=91)
    tdb.execute(
        "INSERT INTO venues (venue_name, venue_postcode, travel_transit_min, "
        "lookup_date, google_place_id) VALUES (?, ?, ?, ?, ?)",
        ("Stale Venue", "W1 1AA", 30, stale.isoformat(), "ChIJ_stale"),
    )
    tdb.commit()
    check("cache: expired entry not returned (name)",
          fetch_cache(tdb, "Stale Venue", None, 90) is None, True)
    check("cache: expired entry not returned (place_id)",
          fetch_cache_by_place_id(tdb, "ChIJ_stale", 90) is None, True)
    tdb.close()

    # --- Cache population from candidates tests ---
    tdb2 = sqlite3.connect(":memory:")
    tdb2.row_factory = sqlite3.Row
    tdb2.execute("""CREATE TABLE venues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, venue_name TEXT NOT NULL,
        venue_postcode TEXT, travel_transit_min INTEGER, travel_cycle_min INTEGER,
        travel_walk_min INTEGER, lookup_date TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        venue_lat REAL, venue_lon REAL, google_place_id TEXT,
        formatted_address TEXT)""")
    tdb2.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, date TEXT,
        venue_name TEXT, venue_postcode TEXT, description TEXT,
        travel_transit_min INTEGER, travel_cycle_min INTEGER,
        travel_walk_min INTEGER, travel_display TEXT,
        travel_lookup_failed INTEGER DEFAULT 0,
        pipeline_state TEXT DEFAULT 'pending_llm',
        end_date TEXT, tier TEXT, run_date TEXT DEFAULT '')""")

    recent_run = (date.today() - timedelta(days=10)).isoformat()
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, venue_postcode, "
        "travel_transit_min, travel_cycle_min, travel_walk_min, travel_display, "
        "pipeline_state, run_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Test Event", "2026-08-01", "Uncached Venue", "E1 6AN",
         30, 15, None, "30 min (transit) · 15 min (cycle)", "written", recent_run),
    )
    # No run_date: age is unknown, so the backfilled row must arrive already
    # TTL-expired instead of being laundered as fresh (audit P2).
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, venue_postcode, "
        "travel_transit_min, travel_cycle_min, travel_walk_min, travel_display, "
        "pipeline_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Ancient Event", "2026-08-01", "Ancient Venue", "N1 1AA",
         40, None, None, "40 min (transit)", "written"),
    )
    tdb2.commit()

    pop_count = _populate_cache_from_candidates(tdb2, dry_run=False)
    check("populate: inserts cache entries for uncached venues", pop_count, 2)
    check("populate: cache now has the venue",
          fetch_cache(tdb2, "Uncached Venue", None, 90) is not None, True)
    check("populate: lookup_date carries the candidate's run_date, not today",
          tdb2.execute("SELECT lookup_date FROM venues WHERE venue_name='Uncached Venue'")
              .fetchone()[0], recent_run)
    check("populate: unknown-age travel arrives TTL-expired",
          fetch_cache(tdb2, "Ancient Venue", None, 90) is None, True)
    check("populate: unknown-age venue row still exists",
          tdb2.execute("SELECT COUNT(*) FROM venues WHERE venue_name='Ancient Venue'")
              .fetchone()[0], 1)

    pop_count2 = _populate_cache_from_candidates(tdb2, dry_run=False)
    check("populate: no duplicate on second run", pop_count2, 0)

    # --- Cache population dry-run test ---
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, venue_postcode, "
        "travel_transit_min, travel_cycle_min, travel_walk_min, travel_display, "
        "pipeline_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Dry Pop Event", "2026-08-01", "Dry Pop Venue", "W1A 1AA",
         20, 10, None, "20 min (transit) · 10 min (cycle)", "written"),
    )
    tdb2.commit()
    dry_pop = _populate_cache_from_candidates(tdb2, dry_run=True)
    check("populate: dry-run reports count", dry_pop, 1)
    check("populate: dry-run does not insert",
          fetch_cache(tdb2, "Dry Pop Venue", None, 90) is None, True)

    # --- Cache recovery tests ---
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, venue_postcode, "
        "travel_lookup_failed, pipeline_state) VALUES (?, ?, ?, ?, ?, ?)",
        ("Failed Event", "2026-09-01", "Uncached Venue", "E1 6AN", 1, "pending_travel"),
    )
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, venue_postcode, "
        "travel_lookup_failed, pipeline_state) VALUES (?, ?, ?, ?, ?, ?)",
        ("Genuinely Failed", "2026-09-01", "Nowhere Land", None, 1, "pending_travel"),
    )
    tdb2.commit()

    rec_count = _recover_failed_from_cache(tdb2, dry_run=False)
    check("recover: recovers failed candidate with cached venue", rec_count, 1)

    recovered_row = tdb2.execute(
        "SELECT travel_display, travel_lookup_failed, pipeline_state "
        "FROM candidates WHERE name = 'Failed Event'"
    ).fetchone()
    check("recover: travel_display populated",
          recovered_row["travel_display"], "30 min (transit) · 15 min (cycle)")
    check("recover: travel_lookup_failed reset",
          recovered_row["travel_lookup_failed"], 0)
    check("recover: pipeline_state transitioned to ready_to_write",
          recovered_row["pipeline_state"], "ready_to_write")

    still_failed = tdb2.execute(
        "SELECT travel_lookup_failed, travel_display "
        "FROM candidates WHERE name = 'Genuinely Failed'"
    ).fetchone()
    check("recover: genuinely failed venue stays failed",
          still_failed["travel_lookup_failed"], 1)
    check("recover: genuinely failed has no travel_display",
          still_failed["travel_display"], None)

    # Dry-run recovery test
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, venue_postcode, "
        "travel_lookup_failed, pipeline_state) VALUES (?, ?, ?, ?, ?, ?)",
        ("Dry Run Event", "2026-09-01", "Uncached Venue", "E1 6AN",
         1, "pending_travel"),
    )
    tdb2.commit()
    dry_count = _recover_failed_from_cache(tdb2, dry_run=True)
    check("recover: dry-run reports count", dry_count, 1)
    dry_row = tdb2.execute(
        "SELECT travel_lookup_failed, travel_display "
        "FROM candidates WHERE name = 'Dry Run Event'"
    ).fetchone()
    check("recover: dry-run does not modify candidate",
          dry_row["travel_lookup_failed"], 1)
    check("recover: dry-run leaves travel_display NULL",
          dry_row["travel_display"], None)

    # --- Infinite reprocessing prevention tests ---
    # apply_result with all modes above display thresholds
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, pipeline_state) "
        "VALUES (?, ?, ?, ?)",
        ("Above Threshold", "2026-09-01", "Far Away", "pending_travel"),
    )
    tdb2.commit()
    above_id = tdb2.execute(
        "SELECT id FROM candidates WHERE name = 'Above Threshold'"
    ).fetchone()["id"]
    apply_result(tdb2, [above_id], None, 30, 25)
    above_row = tdb2.execute(
        "SELECT travel_display, travel_lookup_failed, travel_cycle_min, "
        "travel_walk_min, pipeline_state FROM candidates WHERE id = ?",
        (above_id,),
    ).fetchone()
    check("apply_result: display-None sets travel_lookup_failed=1",
          above_row["travel_lookup_failed"], 1)
    check("apply_result: display-None preserves raw cycle data",
          above_row["travel_cycle_min"], 30)
    check("apply_result: display-None preserves raw walk data",
          above_row["travel_walk_min"], 25)
    check("apply_result: display-None transitions pipeline_state",
          above_row["pipeline_state"], "ready_to_write")

    # Recovery skips venues where cached data has no displayable modes
    tdb2.execute(
        "INSERT INTO venues (venue_name, venue_postcode, travel_transit_min, "
        "travel_cycle_min, travel_walk_min, lookup_date) VALUES (?, ?, ?, ?, ?, ?)",
        ("Far Away Place", "", None, 30, 25, date.today().isoformat()),
    )
    tdb2.execute(
        "INSERT INTO candidates (name, date, venue_name, "
        "travel_lookup_failed, pipeline_state) VALUES (?, ?, ?, ?, ?)",
        ("No Display Route", "2026-09-01", "Far Away Place", 1, "pending_travel"),
    )
    tdb2.commit()
    _recover_failed_from_cache(tdb2, dry_run=False)
    no_display_row = tdb2.execute(
        "SELECT travel_lookup_failed FROM candidates WHERE name = 'No Display Route'"
    ).fetchone()
    check("recover: skips venue with no displayable modes",
          no_display_row["travel_lookup_failed"], 1)

    tdb2.close()

    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--smoke-test", action="store_true")
    p.add_argument("--backfill", action="store_true",
                   help="Backfill written rows missing travel data (clears 'Travel time pending' placeholder)")
    p.add_argument("--include-past", action="store_true",
                   help="Include past events (default: future events only)")
    args = p.parse_args()

    if args.smoke_test:
        return _smoke_tests()
    return run(args.db, dry_run=args.dry_run, limit=args.limit,
               backfill=args.backfill, include_past=args.include_past)


if __name__ == "__main__":
    sys.exit(main())
