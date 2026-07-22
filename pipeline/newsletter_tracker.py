#!/usr/bin/env python3
"""
Track which newsletter emails have been processed to prevent re-extraction,
and manage discovered newsletter senders for the discovery search.

Usage:
    python3 newsletter_tracker.py --check <message_id>
    python3 newsletter_tracker.py --record <message_id> --sender <s> --subject <s> --email-date <d> --events <n>
    echo "$BODY" | python3 newsletter_tracker.py --record <message_id> --sender <s> --subject <s> --email-date <d> --events <n> --body-stdin
    python3 newsletter_tracker.py --record-batch
    python3 newsletter_tracker.py --list-recent [--days N]
    python3 newsletter_tracker.py --add-sender <email_or_domain> --sender-name <name>
    python3 newsletter_tracker.py --list-senders
    python3 newsletter_tracker.py --deactivate-sender <email_or_domain>

Exit codes for --check:
    0 = already processed (skip this email)
    1 = not processed (proceed with extraction)

--record-batch replaces the record-newsletters heredoc that lived in
weekly_run.sh (WP4 task 4.2; behaviour pinned by
tests/test_record_newsletters.sh): it reads the pipeline's newsletter
JSON files from the working directory and records every email in one
process instead of shelling out per email.
"""
# Runnable as `python3 pipeline/<name>.py` or importable as a module:
# put the repo root (for erlib) and this package dir (for sibling
# modules) on sys.path before the repo imports below.
import sys as _sys
import pathlib as _pl
_r = _pl.Path(__file__).resolve().parent.parent
for _p in (str(_r / "pipeline"), str(_r)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_emails (
            gmail_message_id TEXT PRIMARY KEY,
            sender TEXT NOT NULL,
            subject TEXT,
            email_date TEXT,
            events_extracted INTEGER DEFAULT 0,
            run_date TEXT,
            processed_at TEXT DEFAULT (datetime('now')),
            body_text TEXT,
            qa_status TEXT,
            discovery INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovered_senders (
            sender_query TEXT PRIMARY KEY,
            sender_name TEXT NOT NULL,
            first_seen TEXT DEFAULT (date('now')),
            last_seen TEXT DEFAULT (date('now')),
            emails_processed INTEGER DEFAULT 0,
            events_found INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        )
    """)
    _migrate_add_columns(conn)
    conn.commit()


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    for col, ddl in [
        ("body_text", "ALTER TABLE processed_emails ADD COLUMN body_text TEXT"),
        ("qa_status", "ALTER TABLE processed_emails ADD COLUMN qa_status TEXT"),
        ("discovery", "ALTER TABLE processed_emails ADD COLUMN discovery INTEGER DEFAULT 0"),
    ]:
        if col not in existing:
            conn.execute(ddl)


def is_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_emails WHERE gmail_message_id = ?",
        (message_id,),
    ).fetchone()
    return row is not None


def record_processed(
    conn: sqlite3.Connection,
    message_id: str,
    sender: str,
    subject: str,
    email_date: str,
    events_extracted: int,
    run_date: str,
    body_text: str = None,
    discovery: bool = False,
) -> None:
    existing = conn.execute(
        "SELECT 1 FROM processed_emails WHERE gmail_message_id = ?", (message_id,)
    ).fetchone()
    if existing:
        updates = ["sender = ?", "subject = ?", "email_date = ?",
                    "events_extracted = ?", "run_date = ?", "processed_at = datetime('now')",
                    "discovery = ?"]
        params = [sender, subject, email_date, events_extracted, run_date, 1 if discovery else 0]
        if body_text is not None:
            updates.append("body_text = ?")
            params.append(body_text)
        params.append(message_id)
        conn.execute(
            f"UPDATE processed_emails SET {', '.join(updates)} WHERE gmail_message_id = ?",
            params,
        )
    else:
        conn.execute(
            """INSERT INTO processed_emails
               (gmail_message_id, sender, subject, email_date, events_extracted, run_date, processed_at, body_text, discovery)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
            (message_id, sender, subject, email_date, events_extracted, run_date, body_text, 1 if discovery else 0),
        )
    conn.commit()


def update_body(conn: sqlite3.Connection, message_id: str, body_text: str) -> bool:
    """Repair a corrupted stored body: overwrite body_text ONLY and clear
    qa_status. Metadata (sender, dates, counts, processed_at) is untouched.

    Returns False when the message was never recorded. Exists because
    2026-07-05 subagents raced on shared scratch files and message
    19f33467ad6a6c5e got another email's body stored: which made QA flag
    all 16 of its candidates. A corrupted body poisons QA verification and
    dedup for that email permanently unless repaired.
    """
    existing = conn.execute(
        "SELECT qa_status FROM processed_emails WHERE gmail_message_id = ?",
        (message_id,),
    ).fetchone()
    if not existing:
        return False
    conn.execute(
        "UPDATE processed_emails SET body_text = ?, qa_status = NULL "
        "WHERE gmail_message_id = ?",
        (body_text, message_id),
    )
    conn.commit()
    if existing[0]:
        print(f"  qa_status {existing[0]!r} cleared (was set against the corrupted body)")
    return True


def add_discovered_sender(
    conn: sqlite3.Connection,
    sender_query: str,
    sender_name: str,
    events_found: int = 0,
) -> None:
    existing = conn.execute(
        "SELECT events_found, emails_processed FROM discovered_senders WHERE sender_query = ?",
        (sender_query,),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE discovered_senders
               SET last_seen = date('now'), events_found = events_found + ?,
                   emails_processed = emails_processed + 1, active = 1
               WHERE sender_query = ?""",
            (events_found, sender_query),
        )
    else:
        conn.execute(
            """INSERT INTO discovered_senders
               (sender_query, sender_name, events_found, emails_processed)
               VALUES (?, ?, ?, 1)""",
            (sender_query, sender_name, events_found),
        )
    conn.commit()


def list_discovered_senders(conn: sqlite3.Connection, active_only: bool = True) -> list:
    query = "SELECT sender_query, sender_name, first_seen, last_seen, emails_processed, events_found, active FROM discovered_senders"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY events_found DESC"
    return conn.execute(query).fetchall()


def deactivate_sender(conn: sqlite3.Connection, sender_query: str) -> bool:
    cursor = conn.execute(
        "UPDATE discovered_senders SET active = 0 WHERE sender_query = ?",
        (sender_query,),
    )
    conn.commit()
    return cursor.rowcount > 0


def record_batch(conn: sqlite3.Connection) -> None:
    """Record every newsletter email from the pipeline's JSON files.

    Sources, in precedence order (matching the original heredoc):
    .newsletter_emails_processed.json (metadata), with per-email event
    counts filled from .newsletter_candidates_batch.json; the batch file
    alone as fallback; .emails_to_label.json ids appended last.
    """
    metadata_file = ".newsletter_emails_processed.json"
    batch_file = ".newsletter_candidates_batch.json"
    label_file = ".emails_to_label.json"
    fetch_counts_file = ".last_fetch_counts.json"

    def read_json(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  RECORD_WARN: could not read {path}: {e}", file=sys.stderr)
            return None

    run_date = os.environ.get("RUN_DATE", "")
    if not run_date and Path(fetch_counts_file).exists():
        fc = read_json(fetch_counts_file)
        if fc:
            run_date = fc.get("newsletter_senders", {}).get("run_date", "")
    if not run_date:
        # Same default the per-email CLI applies when --run-date is omitted
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    batch_counts = {}
    if Path(batch_file).exists():
        candidates = read_json(batch_file)
        if isinstance(candidates, list):
            for c in candidates:
                mid = c.get("message_id") or c.get("source_snapshot")
                if not mid:
                    continue
                if mid not in batch_counts:
                    batch_counts[mid] = {"sender": c.get("organiser", "Unknown"), "count": 0}
                batch_counts[mid]["count"] += 1

    emails = []

    if Path(metadata_file).exists():
        raw = read_json(metadata_file)
        if isinstance(raw, list) and raw:
            for email in raw:
                mid = email.get("message_id")
                if mid and mid in batch_counts:
                    email.setdefault("events_extracted", batch_counts[mid]["count"])
                    email.setdefault("sender", batch_counts[mid]["sender"])
                else:
                    email.setdefault("events_extracted", 0)
            emails = raw
            print(f"RECORD_SOURCE: {metadata_file} ({len(emails)} emails)")

    if not emails and batch_counts:
        emails = [
            {"message_id": mid, "sender": info["sender"],
             "events_extracted": info["count"]}
            for mid, info in batch_counts.items()
        ]
        print(f"RECORD_SOURCE: {batch_file} (fallback, {len(emails)} emails, no email_date)")

    if not emails and not Path(label_file).exists():
        print("RECORD_SKIP: no metadata, batch, or label file found")
        return

    known_ids = {e.get("message_id") for e in emails if e.get("message_id")}

    if Path(label_file).exists():
        label_ids = read_json(label_file)
        if isinstance(label_ids, list):
            extra_ids = [mid for mid in label_ids if mid not in known_ids]
            if extra_ids:
                for mid in extra_ids:
                    if mid in batch_counts:
                        emails.append({"message_id": mid, "sender": batch_counts[mid]["sender"],
                                       "events_extracted": batch_counts[mid]["count"]})
                    else:
                        emails.append({"message_id": mid, "sender": "Unknown", "events_extracted": 0})
                print(f"RECORD_LABEL_FILL: {len(extra_ids)} additional emails from {label_file}")

    recorded = 0
    skipped = 0

    for email in emails:
        mid = email.get("message_id")
        if not mid:
            continue
        if is_processed(conn, mid):
            skipped += 1
            continue
        try:
            record_processed(
                conn, mid,
                email.get("sender", "Unknown"),
                email.get("subject", ""),
                str(email.get("email_date", "")),
                email.get("events_extracted", 0),
                run_date,
            )
            recorded += 1
        except sqlite3.Error as e:
            print(f"  RECORD_ERROR: {mid}: {e}", file=sys.stderr)

    print(f"RECORD_NEWSLETTERS: {recorded} recorded, {skipped} already existed")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument("--check", metavar="MSG_ID", help="Check if a message ID has been processed")
    sub.add_argument("--record", metavar="MSG_ID", help="Record a message ID as processed")
    sub.add_argument("--record-batch", action="store_true",
                     help="Record all emails from the pipeline's newsletter JSON files (cwd)")
    sub.add_argument("--update-body", metavar="MSG_ID",
                     help="Repair a corrupted stored body (requires --body-stdin); "
                          "overwrites body_text only and clears qa_status")
    sub.add_argument("--list-recent", action="store_true", help="List recently processed emails")
    sub.add_argument("--add-sender", metavar="QUERY", help="Add a discovered sender (email address or domain)")
    sub.add_argument("--list-senders", action="store_true", help="List discovered senders")
    sub.add_argument("--deactivate-sender", metavar="QUERY", help="Deactivate a discovered sender")
    sub.add_argument("--get-sender-queries", action="store_true",
                     help="Output active discovered sender queries, one per line (for building Gmail search)")
    p.add_argument("--sender", default="")
    p.add_argument("--sender-name", default="", help="Display name for --add-sender")
    p.add_argument("--subject", default="")
    p.add_argument("--email-date", default="")
    p.add_argument("--events", type=int, default=0)
    p.add_argument("--run-date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--days", type=int, default=14, help="Days to look back for --list-recent")
    p.add_argument("--body-stdin", action="store_true",
                   help="Read email body text from stdin (for --record)")
    p.add_argument("--discovery", action="store_true",
                   help="Mark this email as from a discovered (not known) sender")
    p.add_argument("--all", action="store_true",
                   help="Include inactive senders in --list-senders")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_schema(conn)

    if args.check:
        if is_processed(conn, args.check):
            print(f"SKIP: {args.check} already processed")
            sys.exit(0)
        else:
            print(f"NEW: {args.check} not yet processed")
            sys.exit(1)

    elif args.record_batch:
        record_batch(conn)

    elif args.record:
        body_text = sys.stdin.read() if args.body_stdin else None
        record_processed(
            conn, args.record, args.sender, args.subject, args.email_date,
            args.events, args.run_date, body_text, discovery=args.discovery,
        )
        print(f"RECORDED: {args.record} ({args.events} events{', discovery' if args.discovery else ''}{', with body' if body_text else ''})")

    elif args.update_body:
        if not args.body_stdin:
            print("ERROR: --update-body requires --body-stdin", file=sys.stderr)
            sys.exit(2)
        if update_body(conn, args.update_body, sys.stdin.read()):
            print(f"BODY_UPDATED: {args.update_body}")
        else:
            print(f"ERROR: {args.update_body} was never recorded: use --record", file=sys.stderr)
            sys.exit(1)

    elif args.list_recent:
        rows = conn.execute(
            "SELECT gmail_message_id, sender, subject, email_date, events_extracted, processed_at, discovery "
            "FROM processed_emails WHERE processed_at >= date('now', ?) ORDER BY processed_at DESC",
            (f"-{args.days} days",),
        ).fetchall()
        print(f"Processed emails (last {args.days} days): {len(rows)}")
        for r in rows:
            disc = " [discovery]" if r[6] else ""
            print(f"  {r[0][:12]}... | {r[1]} | {r[3]} | {r[4]} events | {r[5]}{disc}")

    elif args.add_sender:
        name = args.sender_name or args.add_sender
        add_discovered_sender(conn, args.add_sender, name, events_found=args.events)
        print(f"ADDED: {args.add_sender} ({name})")

    elif args.list_senders:
        rows = list_discovered_senders(conn, active_only=not args.all)
        label = "All" if args.all else "Active"
        print(f"{label} discovered senders: {len(rows)}")
        for r in rows:
            status = "" if r[6] else " [inactive]"
            print(f"  {r[0]} | {r[1]} | first: {r[2]} | last: {r[3]} | emails: {r[4]} | events: {r[5]}{status}")

    elif args.deactivate_sender:
        if deactivate_sender(conn, args.deactivate_sender):
            print(f"DEACTIVATED: {args.deactivate_sender}")
        else:
            print(f"NOT FOUND: {args.deactivate_sender}")
            sys.exit(1)

    elif args.get_sender_queries:
        rows = list_discovered_senders(conn, active_only=True)
        for r in rows:
            print(r[0])

    conn.close()


if __name__ == "__main__":
    main()
