#!/usr/bin/env python3
"""Tests for erlib.dedup (WP3 task 3.6). Run: python3 tests/test_erlib_dedup.py

Ports both fetchers' dedup smoke semantics plus the cross-domain Luma case.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from erlib.dedup import is_duplicate, load_existing  # noqa: E402


def make_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, name TEXT, "
                 "date TEXT, url TEXT, pipeline_state TEXT, notion_page_id TEXT)")
    for name, date, url in rows:
        conn.execute("INSERT INTO candidates (name, date, url) VALUES (?,?,?)",
                     (name, date, url))
    return conn


def main() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    conn = make_db([
        ("London Fintech Breakfast", "2026-05-20",
         "https://luma.com/hjk9nc4i?lm_api_id=x&lm_medium=email"),
        ("Ada’s Print Workshop", "2026-06-01", "https://meetup.com/e/999"),
    ])
    ex = load_existing(conn)

    check("exact URL match", is_duplicate(
        {"name": "Whatever", "date": "2099-01-01", "url": "https://meetup.com/e/999"}, ex), True)
    check("cross-domain Luma URL (lu.ma vs stored luma.com?params)", is_duplicate(
        {"name": "Other", "date": "2099-01-01", "url": "https://lu.ma/hjk9nc4i"}, ex), True)
    check("different Luma slug no match", is_duplicate(
        {"name": "Unrelated", "date": "2099-01-01", "url": "https://lu.ma/zzz999"}, ex), False)
    check("exact name+date", is_duplicate(
        {"name": "London Fintech Breakfast", "date": "2026-05-20", "url": None}, ex), True)
    check("normalised name+date (straight vs curly quote)", is_duplicate(
        {"name": "Ada's Print Workshop", "date": "2026-06-01", "url": ""}, ex), True)
    check("same name different date no match", is_duplicate(
        {"name": "London Fintech Breakfast", "date": "2026-05-21", "url": ""}, ex), False)
    check("new event no match", is_duplicate(
        {"name": "Brand New", "date": "2026-09-09", "url": "https://x.com/1"}, ex), False)

    # url_details carries canonical keys too (rescheduling lookups)
    check("url_details has canonical Luma key",
          "https://lu.ma/hjk9nc4i" in ex.url_details, True)

    # intra-run add(): later candidates dedup against just-inserted ones
    ex.add({"name": "Añadido — Nuevo", "date": "2026-10-10",
            "url": "https://luma.com/newslug?utm=1"})
    check("add() registers canonical URL", is_duplicate(
        {"name": "X", "date": "2099-01-01", "url": "https://lu.ma/newslug"}, ex), True)
    check("add() registers normalised name", is_duplicate(
        {"name": "añadido - nuevo", "date": "2026-10-10", "url": ""}, ex), True)

    print("OK: erlib.dedup tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
