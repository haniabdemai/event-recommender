"""RUNS_LOG source/LLM breakdown: extracted from weekly_run.sh's twin heredocs.

Output replicates the heredoc EXACTLY (the two copies had drifted by one
message string; the more informative variant won). Additions over the
heredoc version:
- freshness check (erlib.freshness.is_fresh, data-driven): a state file
  not demonstrably written during THIS run is prefixed "stale:" instead
  of being silently reused;
- the couldn't_process count now reads the key cmd_apply actually writes
  (it was a dead key that always printed '?').

Invoked as: python3 -m erlib.run_log "$TODAY" "$DB"
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from .freshness import is_fresh


def append_runs_log(
    today: str,
    db_path: str | Path,
    fetch_counts_path: str = ".last_fetch_counts.json",
    llm_summary_path: str = ".last_llm_summary.json",
    start_time_path: str = ".pipeline_start_time",
) -> dict:
    conn = sqlite3.connect(str(db_path))
    added = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE pipeline_state='written' AND run_date=?",
        (today,),
    ).fetchone()[0]
    conn.close()

    try:
        with open(fetch_counts_path) as f:
            fc = json.load(f)
        gmail = fc.get("gmail", "?")
        m = fc.get("meetup", {})
        l = fc.get("luma", {})  # noqa: E741
        breakdown = (
            f"Source breakdown: Gmail={gmail}, "
            f"Meetup={m.get('new','?')} (2A={m.get('2a','?')}, 2B={m.get('2b','?')}), "
            f"Luma={l.get('new','?')} (listed={l.get('listed','?')}, detail_ok={l.get('detail_ok','?')})"
        )
        if not is_fresh(fetch_counts_path, start_time_path):
            breakdown = "stale: " + breakdown
        fetch_errors = fc.get("errors", [])
        if fetch_errors:
            breakdown += "; FETCH_ERRORS: " + " | ".join(fetch_errors)
    except FileNotFoundError:
        breakdown = "Source breakdown: fetch counts file not found (fetch step may have been skipped)"

    try:
        with open(llm_summary_path) as f:
            llm = json.load(f)
        tiers = llm.get("tier_breakdown", {})
        tier_str = ", ".join(f"{k}={v}" for k, v in tiers.items())
        dc = llm.get("disagreement_counts", {})
        stale_prefix = "" if is_fresh(llm_summary_path, start_time_path) else "stale: "
        breakdown += (
            f"; {stale_prefix}LLM: reviewed={llm.get('reviewed','?')}, "
            f"couldn't_process={llm.get('couldn_t_process','?')}, "
            f"tiers=[{tier_str}], "
            f"disagreements={dc.get('total',0)} "
            f"({dc.get('upgrades',0)} up, {dc.get('downgrades',0)} down)"
        )
        for d in llm.get("disagreements", []):
            py_t = d.get('python_tier', d.get('py_tier', '?'))
            breakdown += (
                f"; DISAGREE [{d['id']}] {d['name']}: "
                f"{py_t}→{d['llm_tier']}: {d['llm_reasoning'][:100]}"
            )
    except FileNotFoundError:
        pass

    print(f"RUNS_LOG: {breakdown}")
    print(f"RUNS_LOG: added={added}")
    return {"breakdown": breakdown, "added": added}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python3 -m erlib.run_log <today> <db_path>", file=sys.stderr)
        return 2
    append_runs_log(sys.argv[1], sys.argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main())
