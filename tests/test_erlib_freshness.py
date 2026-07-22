#!/usr/bin/env python3
"""Tests for erlib.freshness (Phase 2: one source of truth for run freshness).

Run: python3 tests/test_erlib_freshness.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from erlib import freshness  # noqa: E402


def main() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    tmp = Path(tempfile.mkdtemp(prefix="freshness_"))
    marker = tmp / ".pipeline_start_time"
    state = tmp / ".last_test_state.json"
    now = datetime.now(timezone.utc)

    # stamp()
    payload = freshness.stamp({"a": 1})
    check("stamp adds timestamp_utc", freshness.WRITTEN_AT in payload, True)
    check("stamp is parseable and tz-aware",
          datetime.fromisoformat(payload[freshness.WRITTEN_AT]).tzinfo is not None, True)
    check("stamp mutates in place and returns it", payload["a"], 1)

    # fresh: stamped after run start
    marker.write_text((now - timedelta(minutes=10)).isoformat())
    state.write_text(json.dumps(freshness.stamp({"data": "x"})))
    check("stamped after start is fresh", freshness.is_fresh(state, marker), True)

    # stale: stamped before run start
    old = {"data": "x", freshness.WRITTEN_AT: (now - timedelta(hours=2)).isoformat()}
    state.write_text(json.dumps(old))
    check("stamped before start is stale", freshness.is_fresh(state, marker), False)

    # unknowable -> not fresh
    state.write_text(json.dumps({"data": "no stamp"}))
    check("missing stamp is not fresh", freshness.is_fresh(state, marker), False)
    state.write_text("{not json")
    check("garbled state file is not fresh", freshness.is_fresh(state, marker), False)
    check("missing state file is not fresh",
          freshness.is_fresh(tmp / "nope.json", marker), False)

    state.write_text(json.dumps(freshness.stamp({})))
    check("missing marker is not fresh",
          freshness.is_fresh(state, tmp / "no_marker"), False)
    marker.write_text("not a timestamp")
    check("garbled marker is not fresh", freshness.is_fresh(state, marker), False)

    # naive marker timestamps are assumed UTC (older marker format)
    marker.write_text(now.replace(tzinfo=None).isoformat())
    state.write_text(json.dumps(freshness.stamp({})))
    check("naive marker parses as UTC", freshness.is_fresh(state, marker), True)

    # run_start()
    marker.write_text(now.isoformat())
    check("run_start parses the marker", freshness.run_start(marker), now)
    check("run_start None when missing", freshness.run_start(tmp / "no_marker"), None)

    print("OK: erlib freshness tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
