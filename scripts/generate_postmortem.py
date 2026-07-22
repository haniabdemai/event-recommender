#!/usr/bin/env python3
"""Assemble the evidence file for the per-run post-mortem (Step 8C).

Deterministic layer only: gathers every artefact the run already produced
(run log, pipeline summary, QA diagnostic, newsletter QA, fetch counts,
error files, git commits, DB state counts), pre-computes deviations from
the expected step list, and embeds the instructions for the narrating
subagent. No LLM here: the narrative is written by ONE isolated Sonnet
subagent that reads the evidence file and writes postmortems/<date>.md.

Design notes:
- Runs inside the scheduled-task sandbox: local files + git + sqlite only
  (no gh, no api.github.com). Workflow outcomes are inferred from dispatch
  .done files and git commits, and the subagent is told exactly which
  evidence sources were unavailable: absence must be reported, never
  papered over.
- Origin: Phase 2 card "Every pipeline run writes its own post-mortem"
  (user idea 2026-07-05). The 2026-07-05 run card said "Success / No
  issues" while four workflows had failed their signal step; the
  2026-07-07 post-mortem had to be reconstructed by hand two days later.

Usage:
    python3 scripts/generate_postmortem.py [--run-date YYYY-MM-DD] [--root DIR]
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.config import REPO_SLUG  # noqa: E402

REPO_BLOB_URL = f"https://github.com/{REPO_SLUG}/blob/main"

# (step name, required): names as they appear in the run log. Required steps
# missing from the log are deviations; optional ones are only noted.
EXPECTED_STEPS = [
    ("preflight", True),
    ("newsletter-discovery", False),
    ("newsletter-sweep", True),
    ("fetch-action", True),
    ("apply-findings", False),
    ("enrich-descriptions", False),
    ("score-travel", True),
    ("dedup", False),
    ("llm-sense-check", True),
    ("validate-write-ready", False),
    ("write-notion-action", True),
    ("postmortem", False),
]

SUBAGENT_INSTRUCTIONS = """\
You are writing the post-mortem for one run of the Event Recommender weekly
pipeline. This file contains ALL the evidence you may use: do not read any
other file except to WRITE your output, and do not invent facts. Where the
evidence is marked unavailable, say so explicitly; absence of evidence is a
finding, not a gap to fill from imagination.

Write a markdown post-mortem to the path given in output.md_path (use the
Write tool), with exactly these sections:

# Pipeline post-mortem: <run_date>
## Verdict
One paragraph: did the run deliver, and how cleanly? Lead with the outcome.
## Timeline
Table from evidence.run_log (step, outcome, detail, time). Note gaps.
## Deviations and root causes
One subsection per entry in analysis.deviations, plus anything you can see
in the evidence that the deterministic checks missed. For each: what
happened, root cause (from the evidence only), impact on the user-facing
output.
## What went right
Brief: safeguards that fired correctly, steps that ran clean.
## Proposed fixes
At most 5, ranked. Each: a title of at most 10 plain-English words (no
jargon, it becomes a task card), one paragraph of detail, and which
evidence entry motivates it. Propose only what the evidence supports.
## Evidence index
Which sources were present/absent for this run.

Then return, as your ENTIRE final message, ONLY this JSON object (no
fencing, no commentary):
{"md_written": true, "md_path": "<the path you wrote>",
 "proposed_fixes": [{"title": "...", "detail": "..."}, ...]}
"""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _git_log_today(root: Path, run_date: str):
    try:
        out = subprocess.run(
            ["git", "log", f"--since={run_date} 00:00", "--format=%h|%cI|%an|%s"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return [line for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return None


def _db_counts(db_path: Path, run_date: str):
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """SELECT COUNT(*),
                      SUM(score IS NOT NULL),
                      SUM(llm_reviewed IS NOT NULL),
                      SUM(pipeline_state = 'written'),
                      SUM(pipeline_state = 'vetoed'),
                      SUM(pipeline_state = 'duplicate'),
                      SUM(pipeline_state = 'llm_rejected')
               FROM candidates WHERE run_date = ?""",
            (run_date,),
        ).fetchone()
        conn.close()
        keys = ["fetched", "scored", "llm_reviewed", "written", "vetoed",
                "duplicate", "llm_rejected"]
        return {k: (v or 0) for k, v in zip(keys, row, strict=False)}
    except sqlite3.Error:
        return None


def _dispatch_state(root: Path):
    d = root / ".dispatch"
    if not d.is_dir():
        return None
    state = {}
    for f in sorted(d.iterdir()):
        if f.suffix in (".done", ".trigger"):
            try:
                state[f.name] = f.read_text().strip()[:80]
            except OSError:
                state[f.name] = "(unreadable)"
    return state


def build_evidence(root: Path, db_path: Path, run_date: str) -> dict:
    summary = _read_json(root / ".last_pipeline_summary.json")
    run_log = []
    if summary and isinstance(summary.get("run_log"), list):
        run_log = summary["run_log"]
    local_log = _read_json(root / ".pipeline_run_log.json")
    if isinstance(local_log, list) and len(local_log) > len(run_log):
        run_log = local_log

    diagnostic = _read_json(root / ".last_qa_diagnostic.json")
    newsletter = _read_json(root / ".last_qa_newsletter.json")
    newsletter_slim = None
    if newsletter:
        p1 = newsletter.get("pass1", {})
        p2 = newsletter.get("pass2", {})
        newsletter_slim = {
            "candidates_checked": newsletter.get("candidates_checked"),
            "pass1_mismatches": p1.get("mismatches"),
            "pass1_mismatch_findings": [
                f for f in p1.get("findings", []) if f.get("status") == "mismatch"
            ],
            "pass2_findings_present": bool(p2.get("findings")),
            "pass2_findings": p2.get("findings") or [],
        }

    evidence = {
        "run_date": run_date,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pipeline_summary": summary,
        "run_log": run_log,
        "qa_diagnostic": diagnostic,
        "qa_newsletter": newsletter_slim,
        "llm_summary": _read_json(root / ".last_llm_summary.json"),
        "validator_summary": _read_json(root / ".last_validator_summary.json"),
        "fetch_counts": {
            "combined": _read_json(root / ".last_fetch_counts.json"),
            "meetup": _read_json(root / ".meetup_fetch_counts.json"),
            "luma": _read_json(root / ".luma_fetch_counts.json"),
        },
        "error_files": {
            name: _read_json(root / name)
            for name in (".last_write_error.json", ".last_label_error.json",
                         ".last_split_state_report.json")
            if (root / name).exists()
        },
        "dispatch_state": _dispatch_state(root),
        "git_commits_today": _git_log_today(root, run_date),
        "db_counts": _db_counts(db_path, run_date),
    }
    evidence["analysis"] = analyse(evidence)
    evidence["subagent_instructions"] = SUBAGENT_INSTRUCTIONS
    evidence["output"] = {
        "md_path": f"postmortems/{run_date}.md",
        "md_url_after_merge": f"{REPO_BLOB_URL}/postmortems/{run_date}.md",
    }
    return evidence


def analyse(evidence: dict) -> dict:
    """Deterministic pre-analysis. Deviations only claim what the local
    artefacts can show: the narrating subagent reconciles against git
    commits (steps can run without logging, e.g. in a continuation session
    or inside a GitHub Action's own clone)."""
    deviations = []
    notes = []
    logged = {e.get("step") for e in evidence["run_log"]}

    for step, required in EXPECTED_STEPS:
        if step in logged:
            continue
        msg = f"No run-log entry for step '{step}'"
        if required:
            deviations.append({"kind": "missing-step-log", "step": step,
                               "detail": msg + ": either it did not run or it ran without logging (check git_commits_today)"})
        else:
            notes.append(msg + " (optional step)")

    for e in evidence["run_log"]:
        if e.get("outcome") == "fail":
            deviations.append({"kind": "step-failed", "step": e.get("step"),
                               "detail": e.get("detail", "")})

    diag = evidence.get("qa_diagnostic") or {}
    for check in diag.get("checks", []):
        if check.get("status") == "fail":
            deviations.append({"kind": "qa-check-failed", "step": check.get("name"),
                               "detail": check.get("detail", "")})
        elif check.get("status") == "warn":
            # Warn-level findings must reach the narrative too: proposed by
            # the first generated post-mortem (2026-07-07): the state-mismatch
            # warning was the only trace of three wrongly-vetoed live events.
            notes.append(f"QA warn: {check.get('name')}: {check.get('detail', '')}")

    nl = evidence.get("qa_newsletter")
    if nl is None:
        deviations.append({"kind": "evidence-missing", "step": "newsletter-qa",
                           "detail": "No .last_qa_newsletter.json: Step 8 may not have run"})
    else:
        if nl.get("pass1_mismatches"):
            notes.append(f"Newsletter QA Pass 1: {nl['pass1_mismatches']} mismatches (see findings)")
        if not nl.get("pass2_findings_present"):
            deviations.append({"kind": "qa-incomplete", "step": "newsletter-qa-pass2",
                               "detail": "pass2.findings absent: subagent verification never persisted"})

    for name in evidence.get("error_files", {}):
        deviations.append({"kind": "error-file", "step": name,
                           "detail": f"{name} present: a workflow committed an error diagnostic"})

    summary = evidence.get("pipeline_summary") or {}
    db = evidence.get("db_counts") or {}
    if (summary.get("events_added") and db and db.get("written") is not None
            and db["written"] < summary["events_added"]):
        deviations.append({
            "kind": "summary-db-drift", "step": "pipeline-summary",
            "detail": (f"Summary says {summary['events_added']} written but DB shows "
                       f"{db['written']} in state 'written' for this run_date: "
                       f"post-write state changes (e.g. QA vetoes) not reflected in the summary"),
        })

    if evidence.get("git_commits_today") is None:
        notes.append("git log unavailable: commit evidence missing")
    if evidence.get("db_counts") is None:
        notes.append("DB unavailable: state counts missing")

    return {"deviations": deviations, "notes": notes}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-date",
                    default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--root", default=str(_REPO), type=Path,
                    help="Repo root (tests point this at a fixture dir)")
    ap.add_argument("--db", default=None, type=Path)
    args = ap.parse_args()

    root = args.root
    db_path = args.db if args.db else (
        root / DEFAULT_DB.name if root != _REPO else DEFAULT_DB)

    evidence = build_evidence(root, db_path, args.run_date)

    out_dir = root / "postmortems"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{args.run_date}-evidence.json"
    out_path.write_text(json.dumps(evidence, indent=2, default=str))

    n_dev = len(evidence["analysis"]["deviations"])
    print(f"POSTMORTEM_EVIDENCE: {out_path}")
    print(f"POSTMORTEM_DEVIATIONS: {n_dev}")
    for d in evidence["analysis"]["deviations"]:
        print(f"  - [{d['kind']}] {d['step']}: {d['detail'][:120]}")
    print(f"POSTMORTEM_MD_TARGET: {evidence['output']['md_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
