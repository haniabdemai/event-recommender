#!/usr/bin/env python3
"""Validate ready_to_write candidates before the Notion write.

Extracted from the weekly_run.sh validate-write-ready heredoc (WP4 task
4.2); behaviour pinned by tests/test_write_ready.sh. Demotes rows with
missing required fields to pipeline_state='incomplete' and truncates
descriptions to Notion's 2000 UTF-16-unit limit. The commit/push step
stays in weekly_run.sh.

Also runs the invented-URL gate: a Newsletter candidate's URL must trace
to its stored source email (same url_check QA Pass 1 uses) BEFORE the
write. 2026-07-05: extraction subagents fabricated tracking-style URLs for
7 candidates; 5 reached Notion before post-write QA caught them.
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
import sqlite3
import sys

from erlib.notion import NOTION_TEXT_LIMIT, truncate_with_ellipsis
from scripts.verify_newsletter_extraction import url_check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="path to the SQLite database")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = list(conn.execute(
        "SELECT * FROM candidates WHERE pipeline_state = 'ready_to_write'"
    ))
    if not rows:
        print("VALIDATE_WRITE_READY: 0 candidates to validate")
        return 0

    _body_cache: dict = {}

    def email_body(msg_id):
        """Stored source-email body, or None when unavailable (minimal DBs
        without processed_emails, or body never captured). Memoised: one
        newsletter email routinely backs many candidates."""
        if msg_id in _body_cache:
            return _body_cache[msg_id]
        try:
            r = conn.execute(
                "SELECT body_text FROM processed_emails WHERE gmail_message_id = ?",
                (msg_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            r = None
        body = r["body_text"] if r and r["body_text"] else None
        _body_cache[msg_id] = body
        return body

    truncated = 0
    demoted = 0

    for row in rows:
        rid = row["id"]
        issues = []

        # Required fields
        if not row["name"] or not row["name"].strip():
            issues.append("missing name")
        if not row["date"] or not row["date"].strip():
            issues.append("missing date")
        if not row["llm_tier"]:
            issues.append("missing llm_tier")

        if issues:
            conn.execute(
                "UPDATE candidates SET pipeline_state='incomplete' WHERE id=?",
                (rid,),
            )
            print(f"  DEMOTED [id={rid}] {row['name']!r}: {', '.join(issues)}")
            demoted += 1
            continue

        # Invented-URL gate: fabricated links are worse than no link. Only
        # fires when the check has something to compare (URL present, stored
        # body present, body carries links): url_check's skip semantics.
        cols = row.keys()
        if ("source" in cols and "url" in cols and "source_snapshot" in cols
                and row["source"] == "Newsletter"
                and row["url"] and row["source_snapshot"]):
            body = email_body(row["source_snapshot"])
            if body and url_check(row["url"], body) == "NOT_FOUND":
                conn.execute(
                    "UPDATE candidates SET pipeline_state='incomplete' WHERE id=?",
                    (rid,),
                )
                print(f"  DEMOTED [id={rid}] {row['name']!r}: "
                      "URL not found in source email (invented-link gate)")
                demoted += 1
                continue

        # Description truncation (scoring needs full text, Notion API caps at 2000)
        desc = row["description"]
        if desc:
            truncated_desc = truncate_with_ellipsis(desc)
            if truncated_desc != desc:
                conn.execute(
                    "UPDATE candidates SET description=? WHERE id=?",
                    (truncated_desc, rid),
                )
                print(f"  TRUNCATED [id={rid}] {row['name']!r}: {len(desc)} -> {NOTION_TEXT_LIMIT} chars")
                truncated += 1

    conn.commit()
    conn.close()

    total = len(rows)
    ok = total - truncated - demoted
    print(f"VALIDATE_WRITE_READY: {total} checked, {ok} ok, {truncated} truncated, {demoted} demoted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
