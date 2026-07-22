#!/usr/bin/env python3
"""First tests for newsletter_tracker.py (WP4 task 4.2).

Run: python3 tests/test_newsletter_tracker.py
Covers: --check exit codes, record insert vs update, discovered senders,
and the record_batch function (metadata + batch precedence, idempotency).
All in-process against temp SQLite; no network.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import newsletter_tracker as nt  # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}: got {got!r}, want {want!r}")
        FAIL += 1


def temp_conn(tmp):
    conn = sqlite3.connect(Path(tmp) / "test.db")
    nt.ensure_schema(conn)
    return conn


def main():
    with tempfile.TemporaryDirectory() as tmp:
        conn = temp_conn(tmp)

        # is_processed / record_processed: insert then update
        check("unknown id not processed", nt.is_processed(conn, "m1"), False)
        nt.record_processed(conn, "m1", "The Roundup", "Subject", "2026-07-01", 5, "2026-07-04")
        check("recorded id is processed", nt.is_processed(conn, "m1"), True)
        row = conn.execute(
            "SELECT sender, events_extracted, run_date FROM processed_emails WHERE gmail_message_id='m1'"
        ).fetchone()
        check("insert stored fields", row, ("The Roundup", 5, "2026-07-04"))

        nt.record_processed(conn, "m1", "The Roundup", "Subject", "2026-07-01", 9, "2026-07-05")
        row = conn.execute(
            "SELECT events_extracted, run_date, COUNT(*) OVER () FROM processed_emails WHERE gmail_message_id='m1'"
        ).fetchone()
        check("re-record updates in place (no duplicate row)", row, (9, "2026-07-05", 1))

        # update_body: repair a corrupted stored body (scratch-file race, 2026-07-05)
        conn.execute(
            "UPDATE processed_emails SET body_text='WRONG email body', qa_status='flagged' "
            "WHERE gmail_message_id='m1'"
        )
        conn.commit()
        check("update_body repairs known id", nt.update_body(conn, "m1", "the REAL body"), True)
        row = conn.execute(
            "SELECT body_text, qa_status, sender, events_extracted "
            "FROM processed_emails WHERE gmail_message_id='m1'"
        ).fetchone()
        check("body overwritten", row[0], "the REAL body")
        check("qa_status cleared", row[1], None)
        check("metadata untouched", (row[2], row[3]), ("The Roundup", 9))
        check("update_body unknown id refuses", nt.update_body(conn, "nope", "x"), False)

        # discovered senders
        nt.add_discovered_sender(conn, "news@venue.com", "Venue News", events_found=3)
        nt.add_discovered_sender(conn, "news@venue.com", "Venue News", events_found=2)
        row = conn.execute(
            "SELECT events_found, emails_processed, active FROM discovered_senders WHERE sender_query='news@venue.com'"
        ).fetchone()
        check("sender accumulates events/emails", row, (5, 2, 1))
        check("deactivate existing sender", nt.deactivate_sender(conn, "news@venue.com"), True)
        check("deactivate unknown sender", nt.deactivate_sender(conn, "nope@x.com"), False)
        conn.close()

    # --check exit codes via the CLI (the contract the prompt relies on)
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "cli.db")
        r = subprocess.run(
            [sys.executable, str(REPO / "pipeline" / "newsletter_tracker.py"), "--db", db, "--check", "mX"],
            capture_output=True, text=True,
        )
        check("--check unknown id exits 1", r.returncode, 1)
        subprocess.run(
            [sys.executable, str(REPO / "pipeline" / "newsletter_tracker.py"), "--db", db, "--record", "mX",
             "--sender", "S", "--events", "2"],
            capture_output=True, text=True,
        )
        r = subprocess.run(
            [sys.executable, str(REPO / "pipeline" / "newsletter_tracker.py"), "--db", db, "--check", "mX"],
            capture_output=True, text=True,
        )
        check("--check recorded id exits 0", r.returncode, 0)

    # record_batch: metadata + batch precedence, idempotent re-run
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            conn = temp_conn(tmp)
            Path(".newsletter_emails_processed.json").write_text(json.dumps([
                {"message_id": "b1", "sender": "The Roundup", "subject": "s", "email_date": "2026-07-01"},
                {"message_id": "b2", "sender": "City Weekly", "subject": "s", "email_date": "2026-07-02",
                 "events_extracted": 7},
            ]))
            Path(".newsletter_candidates_batch.json").write_text(json.dumps([
                {"message_id": "b1", "organiser": "The Roundup", "name": "A"},
                {"message_id": "b1", "organiser": "The Roundup", "name": "B"},
            ]))
            os.environ["RUN_DATE"] = "2026-07-04"
            nt.record_batch(conn)
            rows = dict(conn.execute(
                "SELECT gmail_message_id, events_extracted FROM processed_emails"
            ).fetchall())
            check("batch recorded both emails", rows, {"b1": 2, "b2": 7})
            run_date = conn.execute(
                "SELECT run_date FROM processed_emails WHERE gmail_message_id='b1'"
            ).fetchone()[0]
            check("batch run_date from env", run_date, "2026-07-04")
            nt.record_batch(conn)
            count = conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]
            check("re-run is idempotent", count, 2)
            conn.close()
        finally:
            os.environ.pop("RUN_DATE", None)
            os.chdir(cwd)

    print("OK" if FAIL == 0 else "FAILED")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
