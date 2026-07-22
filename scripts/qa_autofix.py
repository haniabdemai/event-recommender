#!/usr/bin/env python3
"""
QA Phase 3: Auto-fix. Reads diagnostic output from Phase 1 and Phase 2,
applies deterministic fixes for each fixable failure.

Fixes applied:
- Bad/tracking URLs → veto candidate
- Missing descriptions → veto candidate
- Notion page mismatches → trigger remediate_notion.py
- NULL scores → re-run score_candidates.py
- Missing format_type → re-run score_candidates.py (assigns format_type)
- Borderline > 30% → re-run validate_llm_output.py
- Fabricated newsletter extraction → veto candidate + flag email in processed_emails
- Source snapshot mismatches → veto candidate

Does NOT fix (informational only):
- Stuck pending_llm (next run picks them up)
- Pipeline step failures (needs full re-run)

Exit codes:
    0 = no fixes needed or all fixes applied successfully
    1 = fix failed (see output)

Usage:
    python3 scripts/qa_autofix.py [--run-date YYYY-MM-DD] [--dry-run]
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.notion import NotionClient, NotionError  # noqa: E402


def load_diagnostic():
    path = SCRIPT_DIR / ".last_qa_diagnostic.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_newsletter_results():
    path = SCRIPT_DIR / ".last_qa_newsletter.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def flag_notion_pages(conn, candidate_ids, dry_run=False):
    """Flag vetoed candidates' live Notion pages 'To delete'.

    Pipeline state and the user-facing board must never diverge silently:
    on 2026-07-05 QA vetoed 36 already-written events in SQLite while their
    pages stayed live, and the supervising session had to flag them by hand.
    Soft-fails per page: a Notion hiccup must not abort the QA pass.
    """
    if not candidate_ids:
        return
    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(
        f"""SELECT id, notion_page_id FROM candidates
            WHERE id IN ({placeholders})
              AND notion_page_id IS NOT NULL
              AND (notion_status IS NULL OR notion_status = 'active')""",
        list(candidate_ids),
    ).fetchall()
    if not rows:
        return
    if dry_run:
        print(f"  DRY RUN: would flag {len(rows)} live Notion pages 'To delete'")
        return
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        ids = ", ".join(str(r[0]) for r in rows)
        print(f"  WARN: {len(rows)} vetoed events have LIVE Notion pages but "
              f"NOTION_TOKEN is not set: flag 'To delete' manually: ids {ids}")
        return
    client = NotionClient(token)
    for rid, page_id in rows:
        try:
            client.request("PATCH", f"/pages/{page_id}",
                           {"properties": {"To delete": {"checkbox": True}}})
            print(f"  FLAGGED 'To delete': Notion page for candidate {rid}")
        except NotionError as e:
            print(f"  WARN: could not flag Notion page for candidate {rid}: {e}")


def veto_candidates(conn, candidate_ids, reason, dry_run=False):
    if not candidate_ids:
        return 0
    if dry_run:
        print(f"  DRY RUN: would veto {len(candidate_ids)} candidates: {reason}")
        flag_notion_pages(conn, candidate_ids, dry_run=True)
        return len(candidate_ids)
    for cid in candidate_ids:
        conn.execute(
            "UPDATE candidates SET pipeline_state = 'vetoed', veto_reason = ? WHERE id = ?",
            (reason, cid),
        )
    conn.commit()
    print(f"  FIXED: vetoed {len(candidate_ids)} candidates: {reason}")
    flag_notion_pages(conn, candidate_ids)
    return len(candidate_ids)


def flag_email(conn, message_id, dry_run=False):
    if dry_run:
        print(f"  DRY RUN: would flag email {message_id}")
        return
    current = conn.execute(
        "SELECT qa_status FROM processed_emails WHERE gmail_message_id = ?",
        (message_id,),
    ).fetchone()
    if current and current[0] == "flagged":
        conn.execute(
            "UPDATE processed_emails SET qa_status = 'permanently_skipped' WHERE gmail_message_id = ?",
            (message_id,),
        )
        print(f"  ESCALATED: email {message_id[:20]}... → permanently_skipped (flagged twice)")
    else:
        conn.execute(
            "UPDATE processed_emails SET qa_status = 'flagged' WHERE gmail_message_id = ?",
            (message_id,),
        )
        print(f"  FLAGGED: email {message_id[:20]}... for careful re-extraction next run")
    conn.commit()


def run_script(script_name, args=None, dry_run=False):
    cmd = ["python3", str(SCRIPT_DIR / script_name)]
    if args:
        cmd.extend(args)
    if dry_run:
        print(f"  DRY RUN: would run {' '.join(cmd)}")
        return True
    print(f"  RUNNING: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  OK: {script_name} completed")
        return True
    else:
        print(f"  ERROR: {script_name} failed (exit {result.returncode})")
        if result.stderr:
            print(f"    {result.stderr[:300]}")
        return False


def fix_from_diagnostic(diagnostic, conn, dry_run=False):
    fixes_applied = 0
    run_date = diagnostic.get("run_date", "")

    needs_rescore = False
    needs_revalidate = False
    needs_remediate = False
    remediate_ids = []

    for check in diagnostic.get("checks", []):
        if check["status"] != "fail" or not check.get("fixable"):
            continue

        name = check["name"]
        cand_ids = check.get("candidate_ids", [])

        if "tracking" in name.lower() or "URLs match pattern" in name or "descriptions" in name.lower() or "names" in name.lower():
            fixes_applied += veto_candidates(conn, cand_ids, f"QA: {name}", dry_run)

        elif "snapshot" in name.lower():
            fixes_applied += veto_candidates(conn, cand_ids, "QA: source snapshot mismatch", dry_run)

        elif "scored" in name.lower() or "format_type" in name.lower():
            needs_rescore = True

        elif "borderline" in name.lower():
            needs_revalidate = True

        elif "notion" in name.lower() and "match" in name.lower():
            needs_remediate = True
            remediate_ids.extend(cand_ids)

    if needs_rescore and run_script("score_candidates.py", dry_run=dry_run):
        fixes_applied += 1

    if needs_revalidate and run_script("validate_llm_output.py", dry_run=dry_run):
        fixes_applied += 1

    if needs_remediate:
        notion_token = os.environ.get("NOTION_TOKEN")
        if notion_token:
            # Remediate ONLY the mismatched pages. Rewriting the whole
            # run_date took 12m41s of verify-run's 13m on 2026-07-05.
            # --run-date always passed: remediate uses it as today_iso for
            # the Added/Batch properties (its default is a 2026-05-03 relic).
            if remediate_ids:
                args = ["--ids", ",".join(str(i) for i in remediate_ids),
                        "--run-date", run_date]
            else:
                args = ["--run-date", run_date]
            if run_script("scripts/remediate_notion.py", args, dry_run=dry_run):
                fixes_applied += 1
        else:
            print("  SKIP: Notion remediation requires NOTION_TOKEN")

    return fixes_applied


def fix_from_newsletter(newsletter, conn, dry_run=False):
    fixes_applied = 0

    if not newsletter:
        return 0

    p1 = newsletter.get("pass1", {})
    for finding in p1.get("findings", []):
        if finding["status"] != "mismatch":
            continue

        cid = finding.get("candidate_id")
        msg_id = finding.get("message_id")
        not_found = [k for k, v in finding.get("fields", {}).items() if v == "NOT_FOUND"]

        if "name" in not_found:
            fixes_applied += veto_candidates(
                conn, [cid], f"QA: name not found in source email ({', '.join(not_found)})", dry_run)
            if msg_id:
                flag_email(conn, msg_id, dry_run)
        elif "url" in not_found and len(not_found) == 1:
            fixes_applied += veto_candidates(
                conn, [cid], "QA: URL not found in source email", dry_run)

    return fixes_applied


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    p.add_argument("--dry-run", action="store_true", help="Show what would be fixed without applying")
    p.add_argument("--run-date", default=None, help="Override run date (default: from diagnostic)")
    args = p.parse_args()

    diagnostic = load_diagnostic()
    newsletter = load_newsletter_results()

    if not diagnostic:
        print("No diagnostic file found (.last_qa_diagnostic.json). Run verify_pipeline_run.py first.")
        return 1

    run_date = args.run_date or diagnostic.get("run_date", "unknown")
    print(f"QA Auto-fix: {run_date}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)

    summary = diagnostic.get("summary", {})
    fixable = summary.get("fixable", 0)
    if fixable == 0 and not newsletter:
        print("No fixable failures found. Nothing to do.")
        return 0

    print(f"Fixable failures from diagnostic: {fixable}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    total_fixes = 0

    print("\n--- Diagnostic fixes ---")
    total_fixes += fix_from_diagnostic(diagnostic, conn, args.dry_run)

    if newsletter:
        mismatches = newsletter.get("pass1", {}).get("mismatches", 0)
        if mismatches > 0:
            print(f"\n--- Newsletter extraction fixes ({mismatches} mismatches) ---")
            total_fixes += fix_from_newsletter(newsletter, conn, args.dry_run)

    conn.close()

    print(f"\n{'=' * 60}")
    print(f"Total fixes applied: {total_fixes}")

    if total_fixes > 0 and not args.dry_run:
        print("\nDB modified. Commit and push should be handled by the calling workflow.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
