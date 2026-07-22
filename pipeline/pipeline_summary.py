#!/usr/bin/env python3
"""Build .last_pipeline_summary.json for the weekly run.

Extracted verbatim from the weekly_run.sh pipeline-summary heredoc (WP4
task 4.1); behaviour is pinned by tests/test_pipeline_summary_golden.sh
and tests/test_pipeline_status.sh. Invoked by `weekly_run.sh
pipeline-summary`: reads pipeline state files from the working
directory, the candidates funnel from SQLite, writes the summary JSON
and prints it.
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
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

# Freshness check: only read files written during THIS pipeline run.
# Data-driven (timestamp_utc in the file vs .pipeline_start_time content):
# mtimes are useless inside GitHub Actions, where checkout resets them.
from erlib.dates import batch_label
from erlib.freshness import is_fresh


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("today", help="run date, YYYY-MM-DD (UTC)")
    parser.add_argument("db", help="path to the SQLite database")
    parser.add_argument("trigger_type", help="Automatic or Manual")
    args = parser.parse_args()

    today, db, trigger_type = args.today, args.db, args.trigger_type
    conn = sqlite3.connect(db)

    # Compute duration from preflight start time
    now_utc = datetime.now(timezone.utc)
    batch_datetime = now_utc.isoformat(timespec="seconds")
    try:
        with open(".pipeline_start_time") as f:
            start_str = f.read().strip()
        start_time = datetime.fromisoformat(start_str)
        delta = now_utc - start_time
        total_min = int(delta.total_seconds() // 60)
        if total_min > 1440:
            duration_str = "Unknown"
        elif total_min < 60:
            duration_str = f"{total_min} min"
        else:
            hours, mins = divmod(total_min, 60)
            duration_str = f"{hours}h {mins}m"
    except FileNotFoundError:
        duration_str = "Unknown"

    # Pipeline funnel counts from SQLite
    total = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=?", (today,)
    ).fetchone()[0]
    python_vetoed = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='vetoed'", (today,)
    ).fetchone()[0]
    llm_rejected = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='llm_rejected'", (today,)
    ).fetchone()[0]
    written = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='written'", (today,)
    ).fetchone()[0]
    pending_llm = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='pending_llm' AND COALESCE(end_date, date) >= date('now')", (today,)
    ).fetchone()[0]
    pending_travel = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='pending_travel' AND COALESCE(end_date, date) >= date('now')", (today,)
    ).fetchone()[0]
    ready_to_write = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='ready_to_write' AND COALESCE(end_date, date) >= date('now')", (today,)
    ).fetchone()[0]
    write_failed = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='write_failed' AND COALESCE(end_date, date) >= date('now')", (today,)
    ).fetchone()[0]
    duplicated = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date=? AND pipeline_state='duplicate'", (today,)
    ).fetchone()[0]

    passed_python = total - python_vetoed

    # Validator corrections
    if is_fresh(".last_validator_summary.json"):
        with open(".last_validator_summary.json") as f:
            val = json.load(f)
        validator_hard_veto = val.get("hard_veto_count", 0)
        validator_cap_demotions = val.get("cap_demotion_count", 0)
        if validator_hard_veto or validator_cap_demotions:
            validator_corrections = validator_hard_veto + validator_cap_demotions
        else:
            validator_corrections = val.get("corrections", 0)
    else:
        validator_corrections = 0
        validator_hard_veto = 0
        validator_cap_demotions = 0

    llm_own_rejections = llm_rejected - validator_corrections

    # Enrichment count
    enriched_count = 0
    if is_fresh(".last_enrichment_summary.json"):
        with open(".last_enrichment_summary.json") as f:
            enriched_count = json.load(f).get("enriched", 0)

    enrich_note = f" ({enriched_count} descriptions enriched)" if enriched_count > 0 else ""

    pipeline_str = (
        f"{total} fetched → {python_vetoed} Python vetoed → "
        f"{duplicated} duplicates removed → "
        f"{passed_python - duplicated} to LLM{enrich_note} → {llm_own_rejections} LLM rejected → "
        f"{validator_corrections} validator corrections → {written} written"
    )

    # Stuck candidates: threshold-aware labeling
    # When adding a new pipeline state, add it to has_blocking if stuck > threshold should trigger Partial.
    PARTIAL_STUCK_THRESHOLD = 3
    stuck_parts = []
    has_blocking = False
    if pending_llm > 0:
        stuck_parts.append(f"{pending_llm} in pending_llm")
        if pending_llm > PARTIAL_STUCK_THRESHOLD:
            has_blocking = True
    if pending_travel > 0:
        stuck_parts.append(f"{pending_travel} in pending_travel")
        if pending_travel > PARTIAL_STUCK_THRESHOLD:
            has_blocking = True
    if ready_to_write > 0:
        stuck_parts.append(f"{ready_to_write} in ready_to_write")
        has_blocking = True
    if write_failed > 0:
        stuck_parts.append(f"{write_failed} write_failed")
        has_blocking = True
    pipeline_health = ", ".join(stuck_parts) if stuck_parts else ""
    if pipeline_health:
        label = "STUCK" if has_blocking else "remaining"
        pipeline_str += f" | {label}: {pipeline_health}"

    # Sources: newsletter dates from DB, Meetup/Luma from JSON
    fc = None
    if is_fresh(".last_fetch_counts.json"):
        with open(".last_fetch_counts.json") as f:
            fc = json.load(f)

    try:
        newsletter_rows = conn.execute("""
            SELECT sender, email_date, events_extracted
            FROM processed_emails
            WHERE run_date = ? AND sender NOT IN ('Meetup', 'Luma', 'Eventbrite')
            ORDER BY sender, email_date
        """, (today,)).fetchall()
    except sqlite3.OperationalError:
        newsletter_rows = []
    conn.close()

    if newsletter_rows:
        by_sender = defaultdict(list)
        for sender, edate, events in newsletter_rows:
            by_sender[sender].append((edate, events))
        sender_parts = []
        for sender in sorted(by_sender, key=lambda s: -sum(ev for _, ev in by_sender[s])):
            entries = by_sender[sender]
            total_events = sum(ev for _, ev in entries)
            dates = []
            for edate, _ in sorted(entries, key=lambda x: x[0] or ""):
                if not edate:
                    continue
                try:
                    d = datetime.strptime(edate, "%Y-%m-%d")
                    dates.append(d.strftime("%-d %b"))
                except (ValueError, TypeError):
                    pass
            date_str = ", ".join(dates) if dates else f"{len(entries)} emails"
            sender_parts.append(f"{sender}: {total_events} events ({date_str})")
        sources_str = f"{'; '.join(sender_parts)}: {len(sender_parts)} of 15 senders"
    elif fc:
        newsletter_senders = fc.get("newsletter_senders", {})
        if newsletter_senders:
            if "senders" in newsletter_senders:
                senders_list = sorted(newsletter_senders["senders"], key=lambda x: -x["events"])
                sender_parts = [f"{s['sender']}: {s['events']} events ({s['emails']} emails)" for s in senders_list]
                sources_str = f"{'; '.join(sender_parts)}: {len(senders_list)} of 15 senders"
            else:
                sender_parts = []
                for name, val in sorted(newsletter_senders.items(),
                                        key=lambda x: -(sum(e["events"] for e in x[1]) if isinstance(x[1], list) else (x[1] if isinstance(x[1], (int, float)) else 0))):
                    if isinstance(val, list):
                        total_events = sum(e["events"] for e in val)
                        dates = ", ".join(e["email_date"] for e in val)
                        sender_parts.append(f"{name}: {total_events} events ({dates})")
                    elif isinstance(val, (int, float)):
                        sender_parts.append(f"{name} ({val})")
                sources_str = f"{'; '.join(sender_parts)}: {len(sender_parts)} of 15 senders"
        else:
            gmail = fc.get("gmail", "?")
            sources_str = f"Newsletters: {gmail} total (per-sender breakdown not available)"
    else:
        sources_str = "Source data not available"

    if fc:
        m = fc.get("meetup", {})
        l = fc.get("luma", {})
        sources_str += f"; Meetup {m.get('new', '?')}; Luma {l.get('new', '?')}"
        errors = fc.get("errors", [])
    else:
        sources_str += "; Meetup ?; Luma ?"
        errors = []

    # Tier breakdown
    if is_fresh(".last_llm_summary.json"):
        with open(".last_llm_summary.json") as f:
            llm = json.load(f)
        tiers = llm.get("tier_breakdown", {})
        tier_str = ", ".join(f"{v} {k}" for k, v in tiers.items() if v > 0)
        disagreements_list = llm.get("disagreements", [])
        if disagreements_list:
            dis_parts = []
            for d in disagreements_list:
                py_t = d.get("python_tier", d.get("py_tier", "?"))
                dis_parts.append(
                    f"[{d['id']}] {d['name']}: {py_t} → {d['llm_tier']}: {d.get('llm_reasoning', '')[:80]}"
                )
            disagreements_str = "\n".join(dis_parts)
        else:
            disagreements_str = "None"
    else:
        tier_str = "LLM data not available"
        disagreements_str = "LLM data not available"

    # Issues
    if is_fresh(".last_run_status.json"):
        with open(".last_run_status.json") as f:
            status = json.load(f)
        outcome = status.get("outcome", "unknown")
    else:
        outcome = "unknown"

    issue_parts = []
    if errors:
        issue_parts.extend(errors)
    if pending_llm > 0:
        word = "stuck" if pending_llm > PARTIAL_STUCK_THRESHOLD else "remaining"
        issue_parts.append(f"{pending_llm} candidates {word} in pending_llm")
    if pending_travel > 0:
        word = "stuck" if pending_travel > PARTIAL_STUCK_THRESHOLD else "remaining"
        issue_parts.append(f"{pending_travel} candidates {word} in pending_travel")
    if ready_to_write > 0:
        issue_parts.append(f"{ready_to_write} candidates stuck in ready_to_write")
    if write_failed > 0:
        issue_parts.append(f"{write_failed} candidates failed during Notion write")
    if validator_hard_veto > 0:
        issue_parts.append(f"Validator corrected {validator_hard_veto} hard-veto LLM mistakes")
    if validator_cap_demotions > 0:
        issue_parts.append(f"Borderline cap enforced: {validator_cap_demotions} demoted")
    issues_str = "; ".join(issue_parts) if issue_parts else "No issues"

    # Status: has_blocking computed above with stuck_parts
    if outcome == "fail":
        status_val = "Failed"
    elif has_blocking:
        status_val = "Partial"
    elif outcome == "unknown":
        status_val = "Incomplete"
    else:
        status_val = "Success"

    summary = {
        "batch_date": batch_datetime,
        "batch_label": batch_label(today),
        "status": status_val,
        "events_added": written,
        "issues": issues_str,
        "sources": sources_str,
        "pipeline": pipeline_str,
        "tier_breakdown": tier_str,
        "disagreements": disagreements_str,
        "trigger": trigger_type,
        "duration": duration_str,
    }

    # Embed local run log into the summary so it persists in the committed file.
    # The raw .pipeline_run_log.json is gitignored (local debugging aid only).
    run_log_path = ".pipeline_run_log.json"
    try:
        with open(run_log_path) as f:
            run_log = json.load(f)
        if isinstance(run_log, list):
            summary["run_log"] = run_log
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    out = ".last_pipeline_summary.json"
    with open(out, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"PIPELINE_SUMMARY_OK: {out}")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
