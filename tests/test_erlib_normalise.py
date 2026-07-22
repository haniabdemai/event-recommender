#!/usr/bin/env python3
"""Tests for erlib.normalise (WP3 task 3.4). Run: python3 tests/test_erlib_normalise.py"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from erlib.normalise import (  # noqa: E402
    normalise_name,
    normalise_luma_url,
    slug_from_luma_url,
)


def main() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    # Curly quotes, dashes, NBSP variants normalise to the same string
    check("curly apostrophe", normalise_name("Ada’s Night"), "ada's night")
    check("straight matches curly",
          normalise_name("Ada's Night"), normalise_name("Ada’s Night"))
    check("em dash + double space",
          normalise_name("Art — Life  Drawing"), "art - life drawing")
    check("case + whitespace", normalise_name("  MIXED Case \n"), "mixed case")
    check("empty", normalise_name(""), "")
    check("None", normalise_name(None), "")

    # Luma URL canonicalisation
    check("luma.com with params",
          normalise_luma_url("https://luma.com/hjk9nc4i?lm_api_id=abc&lm_medium=email"),
          "https://lu.ma/hjk9nc4i")
    check("lu.ma with params",
          normalise_luma_url("https://lu.ma/hjk9nc4i?t=123"), "https://lu.ma/hjk9nc4i")
    check("non-luma untouched",
          normalise_luma_url("https://meetup.com/event/123"), "https://meetup.com/event/123")
    check("None passes through", normalise_luma_url(None), None)

    check("slug from luma.com", slug_from_luma_url("https://luma.com/hjk9nc4i?x=1"), "hjk9nc4i")
    check("slug from lu.ma", slug_from_luma_url("https://lu.ma/bvvrlgam"), "bvvrlgam")
    check("no slug from other", slug_from_luma_url("https://meetup.com/e/1"), None)

    print("OK: erlib.normalise tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
