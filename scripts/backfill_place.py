#!/usr/bin/env python3
"""
One-off backfill: add Place property to Notion pages where the DB now has
geocoded venue data but Notion was written before geocoding happened.

Usage:
    NOTION_TOKEN=ntn_... python3 scripts/backfill_place.py --db event-recommender.db [--dry-run]
    python3 scripts/backfill_place.py --db event-recommender.db --smoke-test
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.write_notion import build_place, get_venue_geo, PLACE_FROM_DATE

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.notion import NotionClient, NotionError  # noqa: E402


def place_for_patch(place: dict) -> dict:
    """Return a fresh dict for PATCH from build_place() output.

    Both CREATE and PATCH use the same keys: {name, address, lat, lon}.
    """
    return {
        "name": place["name"],
        "address": place["address"],
        "lat": place["lat"],
        "lon": place["lon"],
    }


def update_page_place(token: str, page_id: str, place: dict) -> bool:
    """PATCH a single Notion page to set the Place property."""
    try:
        NotionClient(token).request(
            "PATCH", f"/pages/{page_id}",
            {"properties": {"Place": {"place": place_for_patch(place)}}},
        )
        return True
    except NotionError as e:
        print(f"  FAILED: {e}")
        return False


def find_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Find upcoming written events eligible for Place backfill."""
    return conn.execute("""
        SELECT id, name, date, run_date, venue_name, venue_postcode, notion_page_id
        FROM candidates
        WHERE notion_status = 'active'
          AND notion_page_id IS NOT NULL
          AND place_written = 0
          AND COALESCE(end_date, date) >= date('now')
          AND COALESCE(end_date, date) >= ?
          AND venue_name IS NOT NULL
          AND TRIM(venue_name) != ''
    """, (PLACE_FROM_DATE,)).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    if args.smoke_test:
        return smoke_test(args.db)

    token = os.environ.get("NOTION_TOKEN")
    if not token and not args.dry_run:
        print("NOTION_TOKEN must be set.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    if "place_written" not in cols:
        conn.execute("ALTER TABLE candidates ADD COLUMN place_written INTEGER DEFAULT 0")
        conn.commit()
        print("MIGRATE: added place_written column")

    rows = find_candidates(conn)
    print(f"Upcoming written events with venues (date >= {PLACE_FROM_DATE}): {len(rows)}")

    updated = 0
    skipped_no_geo = 0
    skipped_no_place = 0
    failed = 0

    for row in rows:
        venue_geo = get_venue_geo(conn, row["venue_name"], row["venue_postcode"])
        place = build_place(row, venue_geo)
        if place is None:
            if venue_geo is None:
                skipped_no_geo += 1
            else:
                skipped_no_place += 1
            continue

        print(f"  [{row['id']}] {row['name'][:60]} -> {place['name']} "
              f"({place['lat']:.4f}, {place['lon']:.4f})")

        if args.dry_run:
            updated += 1
            continue

        if update_page_place(token, row["notion_page_id"], place):
            conn.execute(
                "UPDATE candidates SET place_written = 1 WHERE id = ?",
                (row["id"],),
            )
            updated += 1
            time.sleep(0.35)
        else:
            failed += 1

    if not args.dry_run and updated > 0:
        conn.commit()

    print(f"\nDone: {updated} updated, {skipped_no_geo} skipped (no geo), "
          f"{skipped_no_place} skipped (no place), {failed} failed")
    conn.close()
    return 0 if failed == 0 else 1


class _RowDict(dict):
    """Dict that supports bracket access like sqlite3.Row.
    Raises KeyError for missing keys instead of returning None."""
    def __getitem__(self, key):
        if key not in self:
            raise KeyError(f"Missing key: {key!r}")
        return super().__getitem__(key)


def smoke_test(db_path: Path) -> int:
    """Verify geo resolution and PATCH key translation for known cases."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    checks = [
        ("Tower Suite, The Chamberlain Hotel",
         "130-135 Minories, London EC3N 1NU, London", True),
        ("London Zoo",
         "Regent's Park, London, NW1 4RY, London", True),
        ("City Arts Centre Cinema 3",
         "EC1A 4EE", True),
        ("OSO Arts Centre",
         "49 Station Road, Barnes Green, London, SW13 0LF, London", True),
        ("BrewDog Waterloo",
         "BrewDog Waterloo, Unit G, Waterloo Station, 01 The Sidings, "
         "London SE1 7BH, London", True),
    ]

    passed = 0

    for venue_name, venue_postcode, should_resolve in checks:
        geo = get_venue_geo(conn, venue_name, venue_postcode)
        resolved = geo is not None and geo["venue_lat"] is not None

        place = None
        patch_place = None
        if resolved:
            row = _RowDict(date="2026-06-01", venue_name=venue_name,
                           venue_postcode=venue_postcode)
            place = build_place(row, geo)
            if place:
                patch_place = place_for_patch(place)

        ok = resolved == should_resolve
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {venue_name}: geo={'yes' if resolved else 'no'}, "
              f"place={'yes' if place is not None else 'no'}")

        if ok and place and patch_place:
            assert place["name"] == venue_name, \
                f"name mismatch: {place['name']} != {venue_name}"
            assert "lat" in patch_place and "lon" in patch_place, \
                "patch_place missing lat/lon keys"
            assert "latitude" not in patch_place, \
                "patch_place should not have latitude key"
            assert patch_place["lat"] == place["lat"], \
                "lat value mismatch after pass-through"
            assert set(patch_place.keys()) == {"name", "address", "lat", "lon"}, \
                f"patch_place key set mismatch: got {set(patch_place.keys())}"

        if ok:
            passed += 1

    print(f"\n{passed}/{len(checks)} checks passed")
    conn.close()
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
