#!/usr/bin/env python3
"""Tests for erlib.run_log + erlib.google_auth (WP3 task 3.7).

Run: python3 tests/test_erlib_run_log.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from erlib.freshness import stamp  # noqa: E402
from erlib.google_auth import refresh_access_token  # noqa: E402
from erlib.run_log import append_runs_log  # noqa: E402

ok = True


def check(label, got, want):
    global ok
    if got == want:
        print(f"  OK  {label}")
    else:
        ok = False
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        db = "t.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, "
                     "pipeline_state TEXT, run_date TEXT)")
        conn.execute("INSERT INTO candidates (pipeline_state, run_date) "
                     "VALUES ('written', '2026-07-04')")
        conn.execute("INSERT INTO candidates (pipeline_state, run_date) "
                     "VALUES ('written', '2026-07-04')")
        conn.commit()
        conn.close()

        # Fresh files: marker content is the run start; state files carry a
        # later timestamp_utc stamp (freshness is data-driven, never mtime)
        from datetime import datetime, timedelta, timezone
        run_start = datetime.now(timezone.utc) - timedelta(seconds=100)
        Path(".pipeline_start_time").write_text(run_start.isoformat())
        Path(".last_fetch_counts.json").write_text(json.dumps(stamp(
            {"gmail": 3, "meetup": {"new": 10, "2a": 6, "2b": 4},
             "luma": {"new": 5, "listed": 20, "detail_ok": 18}, "errors": []})))
        Path(".last_llm_summary.json").write_text(json.dumps(stamp(
            {"reviewed": 15, "couldn_t_process": 2,
             "tier_breakdown": {"Top Picks": 1},
             "disagreement_counts": {"total": 1, "upgrades": 1, "downgrades": 0},
             "disagreements": [{"id": 7, "name": "X", "python_tier": "Borderline",
                                "llm_tier": "Recommended", "llm_reasoning": "why"}]})))

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = append_runs_log("2026-07-04", db)
        out = buf.getvalue()
        check("added counts written rows", result["added"], 2)
        check("breakdown exact fetch text",
              "Source breakdown: Gmail=3, Meetup=10 (2A=6, 2B=4), "
              "Luma=5 (listed=20, detail_ok=18)" in result["breakdown"], True)
        check("couldn_t_process key read (was dead '?')",
              "couldn't_process=2," in result["breakdown"], True)
        check("disagree line format",
              "; DISAGREE [7] X: Borderline→Recommended: why" in result["breakdown"], True)
        check("stdout RUNS_LOG lines", out.count("RUNS_LOG:"), 2)
        check("no stale marker when fresh", "stale:" in result["breakdown"], False)

        # Stale fetch counts: stamped before the pipeline start marker
        stale_counts = json.loads(Path(".last_fetch_counts.json").read_text())
        stale_counts["timestamp_utc"] = (run_start - timedelta(seconds=1000)).isoformat()
        Path(".last_fetch_counts.json").write_text(json.dumps(stale_counts))
        with redirect_stdout(io.StringIO()):
            result2 = append_runs_log("2026-07-04", db)
        check("stale fetch counts labelled",
              result2["breakdown"].startswith("stale: Source breakdown:"), True)

        # Unstamped file (pre-migration format) is treated as stale too
        del stale_counts["timestamp_utc"]
        Path(".last_fetch_counts.json").write_text(json.dumps(stale_counts))
        with redirect_stdout(io.StringIO()):
            result2b = append_runs_log("2026-07-04", db)
        check("unstamped file labelled stale",
              result2b["breakdown"].startswith("stale: Source breakdown:"), True)

        # Missing files
        os.unlink(".last_fetch_counts.json")
        os.unlink(".last_llm_summary.json")
        with redirect_stdout(io.StringIO()):
            result3 = append_runs_log("2026-07-04", db)
        check("missing fetch counts message",
              result3["breakdown"],
              "Source breakdown: fetch counts file not found "
              "(fetch step may have been skipped)")

    # --- google_auth offline ---
    captured = {}

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_transport(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode()
        return FakeResp(b'{"access_token": "tok-123"}')

    tok = refresh_access_token("cid", "csec", "rtok", transport=fake_transport)
    check("access token returned", tok, "tok-123")
    check("token endpoint", captured["url"], "https://oauth2.googleapis.com/token")
    check("grant type posted", "grant_type=refresh_token" in captured["body"], True)
    check("refresh token posted", "refresh_token=rtok" in captured["body"], True)

    print("OK: run_log + google_auth tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
