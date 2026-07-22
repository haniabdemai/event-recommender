#!/usr/bin/env python3
"""Point the pipeline at your city: one command, no manual lookups.

Ask it your city and it resolves everything the pipeline needs and writes
it to .env: coordinates, timezone, the geocoding suffix/region, and your
Luma discovery area. All from free, keyless public services:

  - OpenStreetMap Nominatim   city name  -> lat/lon, country
  - timeapi.io                lat/lon     -> IANA timezone
  - lu.ma/<city>              city        -> discplace-* discovery id

Usage:
    python3 scripts/setup_location.py                 # interactive prompt
    python3 scripts/setup_location.py "Berlin"        # print the .env block
    python3 scripts/setup_location.py "Berlin" --write # write/update .env
    python3 scripts/setup_location.py "New York" --luma-slug nyc --write

Meetup needs no place id (it discovers by the coordinates this resolves).
If Luma has no page for your city, that one value is left at its default
and everything else still works: Luma is optional.
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
ENV_PATH = _REPO / ".env"

# Nominatim's usage policy asks for a genuine identifying User-Agent.
_UA = "event-recommender-setup/1.0 (https://github.com/haniabdemai/event-recommender)"
_DISCPLACE_RE = re.compile(r"discplace-[A-Za-z0-9]+")

# ER_* keys this script manages in .env.
_MANAGED_KEYS = [
    "ER_CITY_NAME", "ER_TIMEZONE", "ER_CITY_LAT", "ER_CITY_LON",
    "ER_GEOCODE_SUFFIX", "ER_GEOCODE_REGION", "ER_LUMA_PLACE_ID",
]


# --- pure helpers (offline-testable) --------------------------------------

def luma_slug(city: str) -> str:
    """Best-effort Luma URL slug from a city name (lu.ma/<slug>)."""
    s = city.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def extract_discplace(html: str) -> str | None:
    """First discplace-* id embedded in a Luma city page, if any."""
    m = _DISCPLACE_RE.search(html or "")
    return m.group(0) if m else None


def geocode_suffix(city: str, country: str | None) -> str:
    """The address suffix appended to geocoding queries."""
    return f"{city}, {country}" if country else city


def render_env_block(values: dict) -> str:
    """The ER_* lines, in canonical order."""
    return "\n".join(f"{k}={values[k]}" for k in _MANAGED_KEYS if k in values)


def merge_env(existing: str, values: dict) -> str:
    """Return .env text with the managed keys set to values.

    Existing managed keys are replaced in place; missing ones are appended
    under a header. All other lines are preserved verbatim and in order.
    """
    lines = existing.splitlines()
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in values:
            key = m.group(1)
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k in _MANAGED_KEYS if k in values and k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- Location (written by scripts/setup_location.py) ---")
        out.extend(f"{k}={values[k]}" for k in missing)
    return "\n".join(out) + "\n"


# --- network resolvers -----------------------------------------------------

def _get_json(url: str):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def resolve_geo(city: str) -> dict:
    """city -> {lat, lon, country, country_code}. Raises on no match."""
    q = urllib.parse.urlencode({
        "q": city, "format": "json", "limit": 1,
        "addressdetails": 1, "accept-language": "en",
    })
    results = _get_json(f"https://nominatim.openstreetmap.org/search?{q}")
    if not results:
        raise LookupError(f"no geocoding match for {city!r}")
    g = results[0]
    addr = g.get("address", {})
    return {
        "lat": g["lat"], "lon": g["lon"],
        "country": addr.get("country"),
        "country_code": (addr.get("country_code") or "").lower(),
    }


def resolve_timezone(lat: str, lon: str) -> str | None:
    try:
        tz = _get_json(
            f"https://timeapi.io/api/timezone/coordinate?latitude={lat}&longitude={lon}")
        return tz.get("timeZone")
    except Exception:
        return None


def resolve_luma(slug: str) -> str | None:
    """Fetch lu.ma/<slug>, extract + validate a discplace id. None if unusable."""
    try:
        html = _get_text(f"https://lu.ma/{slug}")
    except Exception:
        return None
    pid = extract_discplace(html)
    if not pid:
        return None
    # Validate: the id must actually return events from the fetch endpoint.
    try:
        params = urllib.parse.urlencode(
            {"discover_place_api_id": pid, "pagination_limit": 1})
        data = _get_json(f"https://api.lu.ma/discover/get-paginated-events?{params}")
        if (data.get("entries") or data.get("events")):
            return pid
    except Exception:
        return None
    return None


# --- main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("city", nargs="?", help="City name, e.g. \"Berlin\"")
    ap.add_argument("--luma-slug",
                    help="Override the lu.ma/<slug> used to find your Luma area "
                         "(e.g. nyc for New York)")
    ap.add_argument("--write", action="store_true",
                    help="Write/update .env (default: print the block to review)")
    args = ap.parse_args()

    city = args.city or input("What city are you in? ").strip()
    if not city:
        print("No city given.", file=sys.stderr)
        return 1

    print(f"Resolving {city} …", file=sys.stderr)
    try:
        geo = resolve_geo(city)
    except Exception as e:
        print(f"ERROR: could not geocode {city!r}: {e}", file=sys.stderr)
        return 1
    print(f"  coordinates: {geo['lat']}, {geo['lon']} ({geo['country'] or '?'})",
          file=sys.stderr)

    tz = resolve_timezone(geo["lat"], geo["lon"])
    if tz:
        print(f"  timezone:    {tz}", file=sys.stderr)
    else:
        print("  timezone:    could not resolve: set ER_TIMEZONE yourself "
              "(IANA name, e.g. Europe/Berlin)", file=sys.stderr)

    slug = args.luma_slug or luma_slug(city)
    luma_id = resolve_luma(slug)
    if luma_id:
        print(f"  Luma area:   {luma_id} (from lu.ma/{slug})", file=sys.stderr)
    else:
        print(f"  Luma area:   no usable lu.ma/{slug} page: leaving the default. "
              "If Luma covers your city under a different slug, re-run with "
              "--luma-slug <slug>. Meetup still works from your coordinates.",
              file=sys.stderr)

    values = {
        "ER_CITY_NAME": city,
        "ER_CITY_LAT": geo["lat"],
        "ER_CITY_LON": geo["lon"],
        "ER_GEOCODE_SUFFIX": geocode_suffix(city, geo["country"]),
        "ER_GEOCODE_REGION": geo["country_code"],
    }
    if tz:
        values["ER_TIMEZONE"] = tz
    if luma_id:
        values["ER_LUMA_PLACE_ID"] = luma_id

    if args.write:
        existing = ENV_PATH.read_text() if ENV_PATH.exists() else ""
        ENV_PATH.write_text(merge_env(existing, values))
        print(f"\nWrote {len(values)} location values to {ENV_PATH}.", file=sys.stderr)
    else:
        print("\n# Add these to your .env (or re-run with --write):")
        print(render_env_block(values))
    return 0


def _self_test() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    check("slug basic", luma_slug("Berlin"), "berlin")
    check("slug spaces", luma_slug("New York"), "new-york")
    check("slug punctuation", luma_slug("San Francisco, CA"), "san-francisco-ca")
    check("extract discplace",
          extract_discplace('...id":"discplace-gCfX0s3E9Hgo3rG","x'),
          "discplace-gCfX0s3E9Hgo3rG")
    check("extract none", extract_discplace("<html>no id here</html>"), None)
    check("suffix with country", geocode_suffix("Berlin", "Germany"), "Berlin, Germany")
    check("suffix no country", geocode_suffix("Atlantis", None), "Atlantis")

    vals = {"ER_CITY_NAME": "Berlin", "ER_CITY_LAT": "52.5", "ER_CITY_LON": "13.4",
            "ER_TIMEZONE": "Europe/Berlin", "ER_GEOCODE_SUFFIX": "Berlin, Germany",
            "ER_GEOCODE_REGION": "de", "ER_LUMA_PLACE_ID": "discplace-X"}
    # merge replaces an existing key in place and preserves neighbours
    merged = merge_env("NOTION_TOKEN=abc\nER_CITY_NAME=London\nNTFY_TOPIC=t\n", vals)
    check("merge replaces in place", "ER_CITY_NAME=Berlin" in merged, True)
    check("merge preserves others", "NOTION_TOKEN=abc" in merged and "NTFY_TOPIC=t" in merged, True)
    check("merge no duplicate city", merged.count("ER_CITY_NAME="), 1)
    check("merge appends missing", "ER_LUMA_PLACE_ID=discplace-X" in merged, True)
    check("merge appends only missing once", merged.count("ER_TIMEZONE="), 1)
    # empty existing file
    fresh = merge_env("", vals)
    check("fresh has all keys", all(f"{k}=" in fresh for k in _MANAGED_KEYS), True)

    print("All passed" if ok else "FAILURES", file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
