"""Central configuration: paths, IDs, endpoints, city constants.

Values are env-first: every deployment-specific value can be overridden
with an environment variable (see .env.example), and the defaults are
neutral examples. Import from here: never redefine these in scripts.

The pipeline is city-agnostic: every location assumption: timezone, city
coordinates, the geocoding suffix, the Luma discovery area, the travel
origin: is driven by the ER_* environment variables below. The defaults
describe London (the city this was first built against); point them at
any city and the same pipeline works. Nothing about the taste engine,
the sources, or the scoring is London-specific.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "event-recommender.db"

REPO_SLUG = os.environ.get("ER_REPO_SLUG", "haniabdemai/event-recommender")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# The Notion database that receives recommendations. No default: set it
# in your environment (see .env.example and the setup guide).
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

# Optional: an ntfy.sh topic for run notifications on your phone.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

CITY_TZ = ZoneInfo(os.environ.get("ER_TIMEZONE", "Europe/London"))
CITY_LAT = float(os.environ.get("ER_CITY_LAT", "51.5074"))
CITY_LON = float(os.environ.get("ER_CITY_LON", "-0.1278"))
# Human-readable city name: used as the fallback area label when a source
# doesn't give one.
CITY_NAME = os.environ.get("ER_CITY_NAME", "London")
# Appended to every geocoding query to disambiguate the city/country
# (e.g. "Berlin, Germany", "Brooklyn, NY, USA"). Google Maps handles the
# redundant context well and it stops a bare postcode resolving elsewhere.
GEOCODE_SUFFIX = os.environ.get("ER_GEOCODE_SUFFIX", "London, UK")
# Optional ISO-3166-1 region code (e.g. "gb", "de", "us") biasing Google
# Geocoding toward your country. Empty = no bias.
GEOCODE_REGION = os.environ.get("ER_GEOCODE_REGION", "")
# Luma discovery area for your city: the discplace-* id from
# api.lu.ma/discover (see the setup guide for how to find yours). Default
# is London; Source 3A fetches events for this area.
LUMA_PLACE_ID = os.environ.get("ER_LUMA_PLACE_ID", "discplace-QCcNk3HXowOR97j")

# Backwards-compatible aliases (the codebase grew up in London).
LONDON_TZ = CITY_TZ
LONDON_LAT, LONDON_LON = CITY_LAT, CITY_LON

# How far ahead the fetchers look for events (the natural pipeline cap).
FETCH_WINDOW = timedelta(weeks=6)

# Where journeys start when computing travel times: your home/base
# postcode and a human-readable label for reports.
TRAVEL_ORIGIN_POSTCODE = os.environ.get("ER_HOME_POSTCODE", "SW1A 1AA")
TRAVEL_ORIGIN_LABEL = os.environ.get(
    "ER_HOME_LABEL", f"Central London ({TRAVEL_ORIGIN_POSTCODE})"
)

# Meetup groups you belong to: fetched as a priority source (Source 2A)
# ahead of the recommendation feed. Comma-separated URL slugs (the part
# after meetup.com/) in ER_MEETUP_GROUPS; the defaults are fictional
# examples matching the shipped taste-profile template.
MEETUP_GROUPS = [
    g.strip()
    for g in os.environ.get(
        "ER_MEETUP_GROUPS",
        "ai-builders-club,creative-tech-collective,"
        "city-bouldering-social,synth-makers-meetup",
    ).split(",")
    if g.strip()
]
