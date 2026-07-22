#!/usr/bin/env python3
"""Tests for erlib.dates (WP3 task 3.5). Run: python3 tests/test_erlib_dates.py"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from erlib.dates import batch_label, end_date_if_different, iso_to_london  # noqa: E402


def main() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    # The Meetup fix: a non-London offset converts to London wall clock.
    # 19:00 in New York (UTC-4, July) = 00:00 next day in London (BST).
    check("non-London offset converts",
          iso_to_london("2026-07-10T19:00:00-04:00"), ("2026-07-11", "00:00"))
    # Z suffix (Luma style)
    check("Z suffix, summer (BST +1)",
          iso_to_london("2026-07-10T18:30:00.000Z"), ("2026-07-10", "19:30"))
    # Winter: GMT, no shift
    check("Z suffix, winter (GMT)",
          iso_to_london("2026-01-10T18:30:00Z"), ("2026-01-10", "18:30"))
    # London offset unchanged
    check("London offset wall clock kept",
          iso_to_london("2026-07-10T19:00:00+01:00"), ("2026-07-10", "19:00"))
    # Naive assumed local
    check("naive assumed London",
          iso_to_london("2026-07-10T19:00:00"), ("2026-07-10", "19:00"))
    check("empty", iso_to_london(""), ("", ""))
    check("None", iso_to_london(None), ("", ""))
    check("garbage", iso_to_london("not-a-date"), ("", ""))

    check("same-day end -> None",
          end_date_if_different("2026-07-10T19:00:00+01:00", "2026-07-10T22:00:00+01:00"),
          None)
    check("cross-day end -> date",
          end_date_if_different("2026-07-10T19:00:00+01:00", "2026-07-12T17:00:00+01:00"),
          "2026-07-12")
    # Cross-midnight IN LONDON even when same calendar day at source offset
    check("cross-day decided in London time",
          end_date_if_different("2026-07-10T10:00:00-04:00", "2026-07-10T20:00:00-04:00"),
          "2026-07-11")
    check("missing end -> None", end_date_if_different("2026-07-10T19:00:00Z", None), None)

    # batch_label (the Phase 2 hoist from write_notion/pipeline_summary)
    check("batch_label", batch_label("2026-04-30"), "30 Apr")
    check("batch_label single digit", batch_label("2026-05-03"), "3 May")
    try:
        batch_label("nonsense")
        check("batch_label raises on garbage", "no exception", "ValueError")
    except ValueError:
        check("batch_label raises on garbage", "ValueError", "ValueError")

    print("OK: erlib.dates tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
