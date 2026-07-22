#!/usr/bin/env python3
"""Tests for scripts/qa_autofix.py (Phase 2: board sync on QA veto).

Run: python3 tests/test_qa_autofix.py

Pins: vetoing a written candidate flags its live Notion page 'To delete'
in the same pass (2026-07-05: 36 written events were vetoed in SQLite
while their pages stayed live); soft-fail on Notion errors; a clear WARN
naming ids when NOTION_TOKEN is absent; archived/unwritten rows untouched.
"""
from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import qa_autofix  # noqa: E402

FAIL = 0


def check(name, got, want):
    global FAIL
    if got == want:
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


class FakeClient:
    """Records PATCH calls; raises for page ids listed in `broken`."""

    calls = []
    broken = set()

    def __init__(self, token):
        pass

    def request(self, method, path, payload=None):
        page_id = path.rsplit("/", 1)[-1]
        if page_id in FakeClient.broken:
            raise qa_autofix.NotionError(f"Notion PATCH {path} -> HTTP 500: boom")
        FakeClient.calls.append((method, path, payload))
        return {}


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, name TEXT,
            pipeline_state TEXT, veto_reason TEXT,
            notion_page_id TEXT, notion_status TEXT
        )"""
    )
    return conn


def main() -> int:
    qa_autofix.NotionClient = FakeClient

    # --- veto flags live pages, skips archived/unwritten ---
    conn = make_db()
    conn.executemany(
        "INSERT INTO candidates VALUES (?, ?, 'written', NULL, ?, ?)",
        [
            (1, "Live page", "page-1", "active"),
            (2, "Archived page", "page-2", "archived"),
            (3, "Never written", None, None),
            (4, "NULL status live", "page-4", None),
        ],
    )
    os.environ["NOTION" "_TOKEN"] = "test-stub"
    FakeClient.calls, FakeClient.broken = [], set()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = qa_autofix.veto_candidates(conn, [1, 2, 3, 4], "QA: test veto")
    out = buf.getvalue()
    check("all four vetoed in SQLite", n, 4)
    states = {r[0] for r in conn.execute("SELECT DISTINCT pipeline_state FROM candidates")}
    check("pipeline_state flipped", states, {"vetoed"})
    patched = sorted(p.rsplit("/", 1)[-1] for _, p, _ in FakeClient.calls)
    check("only live pages flagged", patched, ["page-1", "page-4"])
    check("payload is the To delete checkbox",
          FakeClient.calls[0][2], {"properties": {"To delete": {"checkbox": True}}})
    check("flag lines printed", out.count("FLAGGED 'To delete'"), 2)

    # --- soft-fail: one broken page doesn't abort the rest ---
    conn2 = make_db()
    conn2.executemany(
        "INSERT INTO candidates VALUES (?, ?, 'written', NULL, ?, 'active')",
        [(1, "A", "page-a"), (2, "B", "page-b")],
    )
    FakeClient.calls, FakeClient.broken = [], {"page-a"}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        qa_autofix.veto_candidates(conn2, [1, 2], "QA: test veto")
    out = buf.getvalue()
    check("broken page warns, run continues", "WARN: could not flag" in out, True)
    check("healthy page still flagged",
          [p.rsplit("/", 1)[-1] for _, p, _ in FakeClient.calls], ["page-b"])

    # --- no token: WARN names the ids, nothing patched ---
    conn3 = make_db()
    conn3.execute("INSERT INTO candidates VALUES (7, 'X', 'written', NULL, 'page-7', 'active')")
    os.environ.pop("NOTION" "_TOKEN")
    FakeClient.calls, FakeClient.broken = [], set()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        qa_autofix.veto_candidates(conn3, [7], "QA: test veto")
    out = buf.getvalue()
    check("tokenless WARN names ids", "ids 7" in out and "WARN" in out, True)
    check("tokenless makes no calls", FakeClient.calls, [])
    check("veto still applied without token",
          conn3.execute("SELECT pipeline_state FROM candidates WHERE id=7").fetchone()[0],
          "vetoed")

    # --- dry run: no DB change, no calls ---
    conn4 = make_db()
    conn4.execute("INSERT INTO candidates VALUES (9, 'Y', 'written', NULL, 'page-9', 'active')")
    FakeClient.calls = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        qa_autofix.veto_candidates(conn4, [9], "QA: test veto", dry_run=True)
    check("dry run leaves state", conn4.execute(
        "SELECT pipeline_state FROM candidates WHERE id=9").fetchone()[0], "written")
    check("dry run makes no calls", FakeClient.calls, [])
    check("dry run announces the flags", "would flag 1 live Notion pages" in buf.getvalue(), True)

    print("OK: qa_autofix tests passed" if FAIL == 0 else "FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
