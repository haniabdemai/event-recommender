#!/usr/bin/env python3
"""Tests for erlib.db (WP3 task 3.2). Run: python3 tests/test_erlib_db.py"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from erlib import db  # noqa: E402


def main() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = db.connect(path)
        check("connect returns sqlite3.Connection",
              isinstance(conn, sqlite3.Connection), True)
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute("INSERT INTO t VALUES ('x')")
        row = conn.execute("SELECT a FROM t").fetchone()
        check("row_factory gives name access", row["a"], "x")

        added = db.ensure_columns(conn, "t", {"b": "INTEGER DEFAULT 0", "a": "TEXT"})
        check("ensure_columns adds only missing", added, ["b"])
        cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
        check("column exists after add", "b" in cols, True)

        added2 = db.ensure_columns(conn, "t", {"b": "INTEGER DEFAULT 0"})
        check("ensure_columns idempotent", added2, [])
        conn.close()
    finally:
        os.unlink(path)

    today = db.today_london()
    check("today_london is ISO format", bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", today)), True)
    check("today_london matches Europe/London",
          today, datetime.now(ZoneInfo("Europe/London")).date().isoformat())

    # schema_check (extracted from the weekly_run.sh heredoc in WP4)
    def fresh_db(tmp, with_tables=True, extra_cols="", skip_migrate=False):
        p = Path(tmp) / "schema.db"
        conn = sqlite3.connect(p)
        if with_tables:
            base = ("id INTEGER PRIMARY KEY, run_date TEXT, name TEXT, date TEXT, time TEXT,"
                    " tier TEXT, score INTEGER, signals_fired TEXT, veto_reason TEXT,"
                    " venue_name TEXT, venue_postcode TEXT, area TEXT, organiser TEXT, cost TEXT,"
                    " description TEXT, format_type TEXT, url TEXT, source TEXT,"
                    " travel_transit_min INTEGER, travel_cycle_min INTEGER, travel_walk_min INTEGER,"
                    " travel_display TEXT, travel_lookup_failed INTEGER,"
                    " pipeline_state TEXT, notion_page_id TEXT")
            if not skip_migrate:
                base += (", llm_tier TEXT, llm_reasoning TEXT, llm_reviewed TEXT, source_snapshot TEXT,"
                         " needs_enrichment INTEGER DEFAULT 0, place_written INTEGER DEFAULT 0,"
                         " description_source TEXT")
            if extra_cols:
                base += ", " + extra_cols
            conn.execute(f"CREATE TABLE candidates ({base})")
            conn.execute("CREATE TABLE venues (venue_name TEXT)")
            conn.execute("CREATE TABLE processed_emails (gmail_message_id TEXT PRIMARY KEY,"
                         " sender TEXT, body_text TEXT, qa_status TEXT)")
        conn.commit()
        conn.close()
        return p

    with tempfile.TemporaryDirectory() as tmp:
        check("schema_check ok on full schema", db.schema_check(fresh_db(tmp)), 0)

    with tempfile.TemporaryDirectory() as tmp:
        check("schema_check fails on missing tables",
              db.schema_check(fresh_db(tmp, with_tables=False)), 1)

    with tempfile.TemporaryDirectory() as tmp:
        p = fresh_db(tmp, skip_migrate=True)
        check("schema_check auto-migrates newer columns", db.schema_check(p), 0)
        cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(candidates)")}
        check("migrated column present", "place_written" in cols, True)

    with tempfile.TemporaryDirectory() as tmp:
        p = fresh_db(tmp, extra_cols="why_youd_like_it TEXT")
        check("schema_check fails on forbidden straggler column", db.schema_check(p), 1)

    with tempfile.TemporaryDirectory() as tmp:
        p = fresh_db(tmp)
        check("schema_check installs regression trigger", db.schema_check(p), 0)
        conn = sqlite3.connect(p)
        conn.execute("INSERT INTO candidates (run_date, name, date, pipeline_state, notion_page_id)"
                     " VALUES ('2026-07-04', 'E', '2026-08-01', 'written', 'pageid')")
        conn.commit()
        try:
            conn.execute("UPDATE candidates SET pipeline_state='pending_llm' WHERE name='E'")
            regressed = True
        except sqlite3.IntegrityError:
            regressed = False
        check("trigger blocks written->pending regression", regressed, False)
        conn.close()

    print("OK: erlib.db tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
