#!/usr/bin/env python3
"""
Post-run QA verification. Runs after the weekly pipeline to confirm every step
completed correctly and no data was corrupted.

Checks (in pipeline order):
1. Pipeline step audit: verifies each step completed via DB state and committed summary
2. Fetch data validation: URL patterns, tracking URLs, descriptions, source snapshot comparison
3. Scoring validation: no NULL scores, format_type, veto/score ordering
4. Sense-check validation: stuck candidates, tier distribution, reviewer completeness
5. Notion write validation: ALL written pages compared against SQLite
6. Pipeline logging: Pipeline Runs page and summary file
7. Place completeness: written candidates with geocoded venues should have place_written=1

Exit codes:
    0 = all checks passed
    1 = one or more checks failed (see output and .last_qa_diagnostic.json)

Usage:
    NOTION_TOKEN=ntn_... python3 scripts/verify_pipeline_run.py [--run-date YYYY-MM-DD]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.notion import NotionClient  # noqa: E402

TRACKING_DOMAINS = {
    "mailchimp.com", "list-manage.com", "sendgrid.net", "mailgun.org",
    "click.mailerlite.com", "track.customer.io", "email.mg.",
}
TRACKING_PATH_KEYWORDS = {"unsubscribe", "tracking", "click", "redirect", "optout", "preferences"}
MEETUP_URL_RE = re.compile(r"^https://www\.meetup\.com/.+/events/\d+/?$")
LUMA_URL_RE = re.compile(r"^https://(lu\.ma|luma\.com)/.+$")


def _parse_validator_counts(val: dict) -> tuple[int, int]:
    """Extract (hard_veto_count, cap_demotion_count) from validator summary."""
    hard_veto = val.get("hard_veto_count")
    if hard_veto is None:
        hard_veto = len(val.get("details") or [])
    cap_demotions = val.get("cap_demotion_count")
    if cap_demotions is None:
        cap_demotions = (val.get("borderline_cap_enforced") or {}).get("demoted") or 0
    return hard_veto, cap_demotions


class Checker:
    def __init__(self):
        self.results = []

    def check(self, name, condition, detail="", fixable=False):
        self.results.append({
            "name": name,
            "status": "pass" if condition else "fail",
            "detail": detail if not condition else "",
            "fixable": fixable if not condition else False,
        })

    def warn(self, name, detail=""):
        self.results.append({
            "name": name,
            "status": "warn",
            "detail": detail,
            "fixable": False,
        })

    def fail_with_ids(self, name, candidate_ids, detail="", fixable=False):
        self.results.append({
            "name": name,
            "status": "fail" if candidate_ids else "pass",
            "detail": detail,
            "fixable": fixable if candidate_ids else False,
            "candidate_ids": candidate_ids,
        })

    @property
    def passed(self):
        return [r for r in self.results if r["status"] == "pass"]

    @property
    def failed(self):
        return [r for r in self.results if r["status"] == "fail"]

    @property
    def warnings(self):
        return [r for r in self.results if r["status"] == "warn"]

    @property
    def fixable_failures(self):
        return [r for r in self.failed if r.get("fixable")]

    @property
    def ok(self):
        return len(self.failed) == 0

    def summary_text(self):
        lines = [f"PASSED: {len(self.passed)}"]
        if self.warnings:
            lines.append(f"WARNINGS: {len(self.warnings)}")
            for w in self.warnings:
                lines.append(f"  ! {w['name']}: {w['detail']}")
        if self.failed:
            lines.append(f"FAILED: {len(self.failed)} ({len(self.fixable_failures)} fixable)")
            for f in self.failed:
                fix_tag = " [FIXABLE]" if f.get("fixable") else ""
                lines.append(f"  X {f['name']}: {f['detail']}{fix_tag}")
        return "\n".join(lines)

    def to_json(self, run_date):
        return {
            "run_date": run_date,
            "checks": self.results,
            "summary": {
                "passed": len(self.passed),
                "failed": len(self.failed),
                "warnings": len(self.warnings),
                "fixable": len(self.fixable_failures),
                "needs_attention": len(self.failed) - len(self.fixable_failures),
            },
        }


def notion_get(token, path):
    return NotionClient(token).request("GET", path)


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _is_tracking_url(url):
    if not url:
        return False
    url_lower = url.lower()
    for domain in TRACKING_DOMAINS:
        if domain in url_lower:
            return True
    path = url_lower.split("?")[0]
    return any(kw in path for kw in TRACKING_PATH_KEYWORDS)


# ---------------------------------------------------------------------------
# Check 1: Pipeline step audit
# ---------------------------------------------------------------------------

def check_pipeline_steps(c, conn, run_date):
    steps = []

    # Pipeline status from the committed summary (replaces gitignored .last_run_status.json)
    summary = _read_json_file(SCRIPT_DIR / ".last_pipeline_summary.json")
    if summary:
        batch_date = summary.get("batch_date", "")
        summary_matches_run = batch_date.startswith(run_date) if batch_date else False
        status_val = summary.get("status", "Unknown")
        events_added = summary.get("events_added", "?")
        if summary_matches_run:
            steps.append(f"Pipeline status: {status_val} ({events_added} events added)")
            c.check("Pipeline summary available", True)
            c.check("Pipeline did not fail", status_val != "Failed",
                    f"Status: {status_val}, Issues: {summary.get('issues', '?')}")
        else:
            c.warn("Pipeline summary is from a different run",
                   f"Summary batch_date={batch_date}, expected run_date={run_date}")
            summary = None
    else:
        c.check("Pipeline summary available", False, "No .last_pipeline_summary.json")

    # Step completion from DB state (replaces gitignored .pipeline_run_log.json)
    fetched = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date = ?", (run_date,)
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date = ? AND score IS NOT NULL",
        (run_date,)
    ).fetchone()[0]
    llm_reviewed = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date = ? AND llm_reviewed IS NOT NULL",
        (run_date,)
    ).fetchone()[0]
    written = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE run_date = ? AND pipeline_state = 'written'",
        (run_date,)
    ).fetchone()[0]

    c.check("Fetch step completed", fetched > 0,
            f"No candidates found for run_date {run_date}")
    if fetched > 0:
        c.check(f"Score step completed ({scored}/{fetched} scored)", scored > 0,
                "No candidates were scored")
        c.check(f"LLM step completed ({llm_reviewed}/{fetched} reviewed)", llm_reviewed > 0,
                "No candidates were LLM-reviewed")
        c.check(f"Write step completed ({written} written to Notion)", written > 0,
                "No candidates were written to Notion")
        steps.append(f"Steps: {fetched} fetched → {scored} scored → "
                     f"{llm_reviewed} LLM-reviewed → {written} written")

    # Run log from pipeline summary (if embedded by cmd_pipeline_summary)
    run_log = summary.get("run_log", []) if summary else []
    if run_log:
        step_names = [e.get("step", "?") for e in run_log]
        failed_steps = [e for e in run_log if e.get("outcome") == "fail"]
        steps.append(f"Run log: {len(run_log)} entries: {', '.join(step_names)}")
        if failed_steps:
            for fs in failed_steps:
                c.check(f"Step '{fs['step']}' succeeded", False,
                        f"Failed: {fs.get('detail', 'no detail')}")

    fc = _read_json_file(SCRIPT_DIR / ".last_fetch_counts.json") or {}
    meetup = fc.get("meetup", {})
    luma = fc.get("luma", {})
    # The combined file is assembled by the orchestrator (Step 3). If that merge
    # never ran: e.g. the newsletter step rewrote the file with sender data only:
    # fall back to the per-fetcher files committed by fetch.yml, which are the
    # source of truth for fetch success.
    if not meetup:
        meetup = _read_json_file(SCRIPT_DIR / ".meetup_fetch_counts.json") or {}
    if not luma:
        luma = _read_json_file(SCRIPT_DIR / ".luma_fetch_counts.json") or {}
    if meetup or luma:
        errors = fc.get("errors", [])
        m_ok = meetup.get("ok", False)
        l_ok = luma.get("ok", False)
        steps.append(f"Fetch: Meetup={'OK' if m_ok else 'FAILED'} ({meetup.get('new', '?')} new), "
                     f"Luma={'OK' if l_ok else 'FAILED'} ({luma.get('new', '?')} new)")
        if errors:
            for err in errors:
                steps.append(f"  Fetch error: {err[:200]}")
        c.check("At least one fetcher succeeded", m_ok or l_ok,
                f"Both failed: {'; '.join(errors)}")
    else:
        c.warn("No fetch counts data", "Fetch step may not have run")

    val = _read_json_file(SCRIPT_DIR / ".last_validator_summary.json")
    if val:
        hv, cd = _parse_validator_counts(val)
        steps.append(f"Validator: {hv} hard-veto corrections, {cd} cap demotions")
        c.check("Validator ran", True)
    else:
        c.warn("No validator summary", "Validator may not have run")

    llm = _read_json_file(SCRIPT_DIR / ".last_llm_summary.json")
    if llm:
        reviewed = llm.get("reviewed", "?")
        tiers = llm.get("tier_breakdown", {})
        steps.append(f"LLM sense-check: {reviewed} reviewed, tiers={tiers}")
        c.check("LLM sense-check ran", True)
    else:
        c.warn("No LLM summary", "Sense-check may not have run")

    for line in steps:
        print(f"  {line}")


# ---------------------------------------------------------------------------
# Check 2: Fetch data validation
# ---------------------------------------------------------------------------

def check_fetch_data(c, conn, run_date):
    rows = conn.execute(
        "SELECT id, name, url, description, source, source_snapshot, pipeline_state "
        "FROM candidates WHERE run_date = ?",
        (run_date,)
    ).fetchall()

    if not rows:
        c.warn("No candidates for this run date")
        return

    # URL pattern checks
    bad_meetup = []
    bad_luma = []
    tracking_urls = []
    missing_desc = []
    missing_name = []
    snapshot_mismatches = []

    for row in rows:
        rid, name, url, desc, source, snapshot, state = (
            row["id"], row["name"], row["url"], row["description"],
            row["source"], row["source_snapshot"], row["pipeline_state"],
        )

        # Manual stubs (reconcile --recover for user-added pages) were never
        # fetched: there is no source data to validate. June 2026: stubs
        # 2979/2980 were auto-vetoed for "missing description" though stubs
        # have none by design, and their live pages sat vetoed on the board.
        if source == "manual":
            continue

        if not name and state != "vetoed":
            missing_name.append(rid)

        if not desc and state != "vetoed":
            missing_desc.append(rid)

        if url and source == "Meetup" and not MEETUP_URL_RE.match(url):
            bad_meetup.append(rid)
        if url and source == "Luma" and not LUMA_URL_RE.match(url):
            bad_luma.append(rid)
        # Newsletter URLs inherently carry tracking wrappers: that is what
        # the email contains (June false positives 2595/2597). Fabricated
        # newsletter links are caught by Pass 1 traceability and the
        # pre-write invented-URL gate, not by this shape check.
        if url and _is_tracking_url(url) and source != "Newsletter":
            tracking_urls.append(rid)

        # Source snapshot comparison (Newsletter stores message_id, not JSON)
        if snapshot and source in ("Meetup", "Luma"):
            try:
                raw = json.loads(snapshot)
                _compare_snapshot(rid, name, url, desc, source, raw, snapshot_mismatches)
            except json.JSONDecodeError:
                snapshot_mismatches.append({"id": rid, "field": "source_snapshot", "reason": "invalid JSON"})

    c.fail_with_ids("Meetup URLs match pattern", bad_meetup,
                    f"{len(bad_meetup)} bad Meetup URLs", fixable=True)
    c.fail_with_ids("Luma URLs match pattern", bad_luma,
                    f"{len(bad_luma)} bad Luma URLs", fixable=True)
    c.fail_with_ids("No tracking/redirect URLs", tracking_urls,
                    f"{len(tracking_urls)} tracking URLs found", fixable=True)
    c.fail_with_ids("All candidates have names", missing_name,
                    f"{len(missing_name)} missing names", fixable=True)
    c.fail_with_ids("All non-vetoed candidates have descriptions", missing_desc,
                    f"{len(missing_desc)} missing descriptions", fixable=True)

    if snapshot_mismatches:
        detail = "; ".join(
            f"[{m['id']}] {m['field']}: {m['reason']}" for m in snapshot_mismatches[:5]
        )
        c.check("Source snapshot matches candidate data",
                False, f"{len(snapshot_mismatches)} mismatches: {detail}", fixable=True)
    else:
        has_snapshots = any(row["source_snapshot"] for row in rows)
        if has_snapshots:
            c.check("Source snapshot matches candidate data", True)
        else:
            c.warn("No source snapshots to verify", "Candidates may predate snapshot storage")


def _luma_slug(url_str):
    """Extract the path slug from a lu.ma or luma.com URL."""
    for prefix in ("https://lu.ma/", "https://luma.com/", "http://lu.ma/", "http://luma.com/"):
        if url_str.startswith(prefix):
            return url_str[len(prefix):].rstrip("/")
    return url_str


def _compare_snapshot(rid, name, url, desc, source, raw, mismatches):
    if source == "Meetup":
        raw_name = raw.get("title", "")
        raw_url = raw.get("eventUrl", "")
    elif source == "Luma":
        event = raw.get("event") or {}
        raw_name = event.get("name", "")
        raw_url = ""
        url_slug = event.get("url")
        if url_slug:
            raw_url = f"https://lu.ma/{url_slug}"
    else:
        return

    if raw_name and name and raw_name.strip() != name.strip():
        mismatches.append({"id": rid, "field": "name",
                          "reason": f"snapshot='{raw_name[:60]}' vs candidate='{name[:60]}'"})
    if raw_url and url:
        url_match = _luma_slug(raw_url) == _luma_slug(url) if source == "Luma" else raw_url.strip() == url.strip()
        if not url_match:
            mismatches.append({"id": rid, "field": "url",
                              "reason": f"snapshot='{raw_url[:80]}' vs candidate='{url[:80]}'"})


# ---------------------------------------------------------------------------
# Check 3: Scoring validation
# ---------------------------------------------------------------------------

def check_scoring(c, conn, run_date):
    unscored = conn.execute(
        """SELECT COUNT(*) FROM candidates
           WHERE run_date = ? AND score IS NULL
           AND pipeline_state != 'vetoed'
           AND description IS NOT NULL AND description != ''""",
        (run_date,)
    ).fetchone()[0]
    c.check("All eligible candidates scored", unscored == 0,
            f"{unscored} candidates have NULL score", fixable=True)

    no_format = conn.execute(
        """SELECT COUNT(*) FROM candidates
           WHERE run_date = ? AND format_type IS NULL
           AND pipeline_state != 'vetoed'""",
        (run_date,)
    ).fetchone()[0]
    c.check("All candidates have format_type", no_format == 0,
            f"{no_format} candidates missing format_type", fixable=True)

    # Veto/score ordering: Python-vetoed candidates should not have positive scores.
    # QA vetoes (veto_reason LIKE 'QA:%') happen after scoring, so positive scores are expected.
    bad_order = conn.execute(
        """SELECT COUNT(*) FROM candidates
           WHERE run_date = ? AND pipeline_state = 'vetoed' AND score > 0
           AND (veto_reason IS NULL OR veto_reason NOT LIKE 'QA:%')""",
        (run_date,)
    ).fetchone()[0]
    c.check("No vetoed candidates with positive scores", bad_order == 0,
            f"{bad_order} vetoed candidates have score > 0 (veto/score ordering bug)")


# ---------------------------------------------------------------------------
# Check 4: LLM sense-check validation
# ---------------------------------------------------------------------------

def check_sense_check(c, conn, run_date):
    # Stuck candidates (needs valid date for window calculation)
    try:
        today = date.fromisoformat(run_date)
        window_end = (today + timedelta(days=28)).isoformat()
        stuck = conn.execute(
            """SELECT COUNT(*) FROM candidates
               WHERE pipeline_state = 'pending_llm'
               AND llm_reviewed IS NULL
               AND date >= ? AND date <= ?""",
            (run_date, window_end)
        ).fetchone()[0]
        c.check("No candidates stuck in pending_llm", stuck == 0,
                f"{stuck} candidates still waiting for LLM review")
    except ValueError:
        c.warn("Stuck candidates check skipped", f"Cannot parse run_date '{run_date}' as date")

    # Tier distribution: 30% Borderline cap
    tiers = conn.execute(
        """SELECT llm_tier, COUNT(*) as cnt FROM candidates
           WHERE run_date = ? AND llm_tier IS NOT NULL
           GROUP BY llm_tier""",
        (run_date,)
    ).fetchall()
    tier_dict = {r["llm_tier"]: r["cnt"] for r in tiers}
    total_accepted = sum(v for k, v in tier_dict.items() if k != "Not Recommended")
    borderline = tier_dict.get("Borderline", 0)

    if total_accepted > 0:
        pct = borderline * 100 // total_accepted
        c.check("Borderline below 30% cap",
                pct <= 30,
                f"{borderline}/{total_accepted} = {pct}% Borderline", fixable=True)
    else:
        c.warn("No accepted candidates to check distribution")

    # All reviewed candidates have required fields
    missing_fields = conn.execute(
        """SELECT COUNT(*) FROM candidates
           WHERE run_date = ? AND llm_reviewed IS NOT NULL
           AND (llm_tier IS NULL OR llm_reasoning IS NULL)""",
        (run_date,)
    ).fetchone()[0]
    c.check("All reviewed candidates have llm_tier and llm_reasoning",
            missing_fields == 0,
            f"{missing_fields} reviewed candidates missing tier or reasoning")

    # Validator correction rate: split hard-veto errors from Borderline cap
    val = _read_json_file(SCRIPT_DIR / ".last_validator_summary.json")
    if val and total_accepted > 0:
        hard_veto, cap_demotions = _parse_validator_counts(val)
        rate = hard_veto * 100 // total_accepted
        c.check("Validator hard-veto correction rate below 20%",
                rate <= 20,
                f"{hard_veto}/{total_accepted} = {rate}% hard-veto corrections (LLM prompt may need tuning)")
        if cap_demotions > 0:
            c.warn("Borderline cap demotions",
                   f"{cap_demotions} candidates demoted from Borderline to Not Recommended (cap enforcement, not LLM error)")


# ---------------------------------------------------------------------------
# Check 5: Notion write validation
# ---------------------------------------------------------------------------

def check_notion_integrity(c, conn, run_date, token):
    if not token:
        c.warn("Notion integrity check skipped", "No NOTION_TOKEN provided")
        return

    written = conn.execute(
        """SELECT * FROM candidates
           WHERE run_date = ? AND pipeline_state = 'written'
           AND notion_page_id IS NOT NULL""",
        (run_date,)
    ).fetchall()

    if not written:
        c.warn("No written candidates to verify against Notion")
        return

    mismatched_ids = []
    mismatch_details = []
    api_errors = 0

    for row in written:
        rid = row["id"]
        page_id = row["notion_page_id"]
        try:
            page = notion_get(token, f"/pages/{page_id}")
            if page.get("archived"):
                mismatched_ids.append(rid)
                mismatch_details.append(f"[{rid}] page archived in Notion")
                continue

            props = page.get("properties", {})
            _check_notion_field(rid, "URL", row["url"],
                               props.get("Link", {}).get("url"),
                               mismatched_ids, mismatch_details)
            _check_notion_field(rid, "date", row["date"],
                               (props.get("Date", {}).get("date") or {}).get("start", ""),
                               mismatched_ids, mismatch_details, prefix_match=True)
            _check_notion_field(rid, "score", row["score"],
                               props.get("Score", {}).get("number"),
                               mismatched_ids, mismatch_details)

            notion_title_parts = props.get("Name", {}).get("title", [])
            notion_title = "".join(p.get("plain_text", "") for p in notion_title_parts)
            if notion_title and row["name"] and notion_title.strip() != row["name"].strip():
                if rid not in mismatched_ids:
                    mismatched_ids.append(rid)
                mismatch_details.append(
                    f"[{rid}] name: DB='{row['name'][:50]}' Notion='{notion_title[:50]}'")

        except Exception:
            api_errors += 1

    checked = len(written) - api_errors
    c.fail_with_ids(f"Notion pages match SQLite ({checked} checked)",
                    mismatched_ids,
                    "; ".join(mismatch_details[:10]),
                    fixable=True)
    if api_errors:
        c.warn("Notion API errors during verification", f"{api_errors} pages unreachable")


def _check_notion_field(rid, field, db_val, notion_val, ids, details, prefix_match=False):
    if db_val is None or notion_val is None:
        return
    db_str = str(db_val).strip()
    notion_str = str(notion_val).strip()
    if not db_str or not notion_str:
        return
    match = notion_str.startswith(db_str) if prefix_match else (db_str == notion_str)
    if not match:
        if rid not in ids:
            ids.append(rid)
        details.append(f"[{rid}] {field}: DB='{db_str[:60]}' Notion='{notion_str[:60]}'")


# ---------------------------------------------------------------------------
# Check 6: Pipeline logging
# ---------------------------------------------------------------------------

def check_pipeline_logging(c, run_date):
    summary = _read_json_file(SCRIPT_DIR / ".last_pipeline_summary.json")
    if summary:
        required = ["status", "events_added", "sources", "pipeline", "tier_breakdown"]
        missing = [k for k in required if k not in summary or summary[k] is None]
        c.check("Pipeline summary has all required fields",
                len(missing) == 0,
                f"Missing or empty: {missing}")
    else:
        c.warn("No .last_pipeline_summary.json")


# ---------------------------------------------------------------------------
# Check 7: Place completeness
# ---------------------------------------------------------------------------

PLACE_FROM_DATE = "2026-05-01"

def check_place_completeness(c, conn, run_date):
    missing = conn.execute(
        """SELECT c.id, c.name, c.venue_name FROM candidates c
           WHERE c.pipeline_state = 'written'
           AND c.place_written = 0
           AND c.date >= ?
           AND c.run_date = ?
           AND c.venue_name IS NOT NULL
           AND TRIM(c.venue_name) != ''
           AND (
               EXISTS (
                   SELECT 1 FROM venues v
                   WHERE v.venue_name = c.venue_name COLLATE NOCASE
                   AND v.venue_lat IS NOT NULL
               )
               OR (
                   c.venue_postcode IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM venues v
                       WHERE v.venue_postcode = c.venue_postcode COLLATE NOCASE
                       AND v.venue_lat IS NOT NULL
                   )
               )
           )""",
        (PLACE_FROM_DATE, run_date),
    ).fetchall()

    if missing:
        samples = ", ".join(f"[{r['id']}] {r['name'][:40]}" for r in missing[:5])
        c.warn("Place not set despite geocoding available",
               f"{len(missing)} written candidates have place_written=0 "
               f"but venue geocoding exists: {samples}")
    else:
        c.check("Place completeness", True)


# ---------------------------------------------------------------------------
# Check 8: Notion visibility vs pipeline state mismatch
# ---------------------------------------------------------------------------

def check_notion_visibility_mismatch(c, conn):
    """Flag events with active Notion pages but non-written pipeline state."""
    mismatched = conn.execute("""
        SELECT id, name, pipeline_state, notion_status
        FROM candidates
        WHERE notion_status = 'active'
          AND notion_page_id IS NOT NULL
          AND pipeline_state NOT IN ('written', 'ready_to_write', 'pending_travel')
          AND COALESCE(end_date, date) >= date('now')
    """).fetchall()

    if mismatched:
        samples = ", ".join(
            f"[{r['id']}] {r['name'][:30]} ({r['pipeline_state']})"
            for r in mismatched[:5]
        )
        c.warn("Notion/pipeline state mismatch",
               f"{len(mismatched)} active Notion events have non-written pipeline state "
               f"(invisible to backfills using pipeline_state instead of notion_status): {samples}")
    else:
        c.check("Notion visibility consistency", True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    p.add_argument("--run-date", default=None,
                   help="Run date to verify (default: most recent completed run)")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.run_date:
        run_date = args.run_date
    else:
        row = conn.execute(
            "SELECT DISTINCT run_date FROM candidates ORDER BY run_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("ERROR: no completed runs found in database")
            return 1
        run_date = row["run_date"]

    print(f"QA Verification: {run_date}")
    print("=" * 60)

    token = os.environ.get("NOTION_TOKEN")
    c = Checker()

    print("\n--- Check 1: Pipeline Step Audit ---")
    check_pipeline_steps(c, conn, run_date)

    print("\n--- Check 2: Fetch Data Validation ---")
    check_fetch_data(c, conn, run_date)

    print("\n--- Check 3: Scoring Validation ---")
    check_scoring(c, conn, run_date)

    print("\n--- Check 4: LLM Sense-Check Validation ---")
    check_sense_check(c, conn, run_date)

    print("\n--- Check 5: Notion Write Validation ---")
    check_notion_integrity(c, conn, run_date, token)

    print("\n--- Check 6: Pipeline Logging ---")
    check_pipeline_logging(c, run_date)

    print("\n--- Check 7: Place Completeness ---")
    check_place_completeness(c, conn, run_date)

    print("\n--- Check 8: Notion Visibility Consistency ---")
    check_notion_visibility_mismatch(c, conn)

    conn.close()

    # Write structured output
    diagnostic = c.to_json(run_date)
    out_path = SCRIPT_DIR / ".last_qa_diagnostic.json"
    out_path.write_text(json.dumps(diagnostic, indent=2, ensure_ascii=False))
    print(f"\nDiagnostic written to {out_path}")

    print()
    print(c.summary_text())
    print()

    if c.ok:
        print("VERIFICATION PASSED")
    else:
        print("VERIFICATION FAILED")

    return 0 if c.ok else 1


if __name__ == "__main__":
    sys.exit(main())
