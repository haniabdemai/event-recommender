#!/usr/bin/env python3
"""Tests for erlib.config and erlib.constants (WP3 task 3.1).

Run: python3 tests/test_erlib_config.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    from erlib import config, constants

    # --- config ---
    check("REPO_ROOT is the repo root", (config.REPO_ROOT / "weekly_run.sh").is_file(), True)
    check("DB_PATH filename", config.DB_PATH.name, "event-recommender.db")
    check("DB_PATH lives at repo root", config.DB_PATH.parent, config.REPO_ROOT)
    check("REPO_SLUG", config.REPO_SLUG, "haniabdemai/event-recommender")
    check("NOTION_API", config.NOTION_API, "https://api.notion.com/v1")
    check("NOTION_VERSION", config.NOTION_VERSION, "2022-06-28")
    check("NTFY_TOPIC default is unset", config.NTFY_TOPIC, "")
    check("LONDON_TZ", str(config.LONDON_TZ), "Europe/London")
    check("LONDON coords", (config.LONDON_LAT, config.LONDON_LON), (51.5074, -0.1278))
    check("FETCH_WINDOW", config.FETCH_WINDOW, timedelta(weeks=6))
    check("TRAVEL_ORIGIN_POSTCODE", config.TRAVEL_ORIGIN_POSTCODE, "SW1A 1AA")
    check("TRAVEL_ORIGIN_LABEL", config.TRAVEL_ORIGIN_LABEL, "Central London (SW1A 1AA)")

    # NOTION_DATABASE_ID: default without env, env-first when set
    env_clear = {k: v for k, v in os.environ.items() if k != "NOTION_DATABASE_ID"}
    r = subprocess.run(
        [sys.executable, "-c", "from erlib import config; print(config.NOTION_DATABASE_ID)"],
        capture_output=True, text=True, cwd=REPO, env=env_clear,
    )
    check("NOTION_DATABASE_ID default",
          r.stdout.strip(), "")
    r = subprocess.run(
        [sys.executable, "-c", "from erlib import config; print(config.NOTION_DATABASE_ID)"],
        capture_output=True, text=True, cwd=REPO,
        env={**env_clear, "NOTION_DATABASE_ID": "env-override-id"},
    )
    check("NOTION_DATABASE_ID env-first", r.stdout.strip(), "env-override-id")

    # --- constants ---
    check("TIERS", constants.TIERS,
          ("Top Picks", "Recommended", "Borderline", "Not Recommended"))
    check("SOURCES", constants.SOURCES,
          ("Meetup", "Luma", "Newsletter", "Venue", "manual"))

    print("OK: erlib config/constants tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
