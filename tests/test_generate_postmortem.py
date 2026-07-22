#!/usr/bin/env python3
"""Tests for scripts/generate_postmortem.py: deterministic evidence assembly.

Fixture-driven: builds a fake repo root in a temp dir, runs build_evidence,
and pins the deviation analysis. No network, no LLM, no real DB required.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.generate_postmortem import build_evidence  # noqa: E402

FAILURES = 0


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  PASS: {name}")
    else:
        FAILURES += 1
        print(f"  FAIL: {name} {detail}")


def make_db(path, run_date, written=5):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY, run_date TEXT, score REAL,
        llm_reviewed TEXT, pipeline_state TEXT)""")
    for i in range(written):
        conn.execute("INSERT INTO candidates VALUES (?, ?, 1.0, 'x', 'written')",
                     (i + 1, run_date))
    conn.execute("INSERT INTO candidates VALUES (99, ?, 1.0, 'x', 'vetoed')",
                 (run_date,))
    conn.commit()
    conn.close()


def broken_run_fixture(root, run_date):
    """A run that lost its second half: no LLM/write log entries, one failed
    step, a failed QA check, no newsletter QA file, an error file present,
    and a summary claiming more written events than the DB shows."""
    (root / ".last_pipeline_summary.json").write_text(json.dumps({
        "batch_date": f"{run_date}T22:00:00+00:00",
        "status": "Success",
        "events_added": 22,
        "run_log": [
            {"step": "preflight", "outcome": "continue", "detail": "ok"},
            {"step": "newsletter-sweep", "outcome": "success", "detail": "21 candidates"},
            {"step": "fetch-action", "outcome": "success", "detail": "Meetup: 102 new"},
            {"step": "score-travel", "outcome": "fail", "detail": "travel_time.py crashed"},
        ],
    }))
    (root / ".last_qa_diagnostic.json").write_text(json.dumps({
        "run_date": run_date,
        "checks": [
            {"name": "At least one fetcher succeeded", "status": "fail", "detail": "Both failed: "},
            {"name": "All candidates have names", "status": "pass", "detail": ""},
            {"name": "Notion/pipeline state mismatch", "status": "warn", "detail": "3 active events vetoed"},
        ],
        "summary": {"failed": 1},
    }))
    (root / ".last_write_error.json").write_text(json.dumps({"workflow": "write-notion.yml"}))
    make_db(root / "test.db", run_date, written=5)


def clean_run_fixture(root, run_date):
    steps = ["preflight", "newsletter-sweep", "fetch-action", "score-travel",
             "llm-sense-check", "write-notion-action"]
    (root / ".last_pipeline_summary.json").write_text(json.dumps({
        "batch_date": f"{run_date}T22:00:00+00:00",
        "status": "Success",
        "events_added": 5,
        "run_log": [{"step": s, "outcome": "success", "detail": "ok"} for s in steps],
    }))
    (root / ".last_qa_diagnostic.json").write_text(json.dumps({
        "run_date": run_date, "checks": [], "summary": {"failed": 0},
    }))
    (root / ".last_qa_newsletter.json").write_text(json.dumps({
        "candidates_checked": 3,
        "pass1": {"mismatches": 0, "findings": []},
        "pass2": {"findings": [{"id": 1}]},
    }))
    make_db(root / "test.db", run_date, written=5)


def kinds(evidence):
    return {(d["kind"], d["step"]) for d in evidence["analysis"]["deviations"]}


def main() -> int:
    print("=== Test 1: broken run: every deviation class detected ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        broken_run_fixture(root, "2026-07-07")
        ev = build_evidence(root, root / "test.db", "2026-07-07")
        got = kinds(ev)
        check("missing required step flagged", ("missing-step-log", "llm-sense-check") in got, str(got))
        check("missing write step flagged", ("missing-step-log", "write-notion-action") in got)
        check("failed step flagged", ("step-failed", "score-travel") in got)
        check("failed QA check flagged", ("qa-check-failed", "At least one fetcher succeeded") in got)
        check("missing newsletter QA flagged", ("evidence-missing", "newsletter-qa") in got)
        check("error file flagged", ("error-file", ".last_write_error.json") in got)
        check("summary/DB drift flagged", ("summary-db-drift", "pipeline-summary") in got)
        check("optional steps are notes not deviations",
              not any(k == "missing-step-log" and s == "dedup" for k, s in got))
        check("db counts assembled", ev["db_counts"]["written"] == 5 and ev["db_counts"]["vetoed"] == 1)
        check("subagent instructions embedded", "Proposed fixes" in ev["subagent_instructions"])
        check("md target path set", ev["output"]["md_path"] == "postmortems/2026-07-07.md")
        check("warn-level QA check surfaces as a note",
              any("Notion/pipeline state mismatch" in n for n in ev["analysis"]["notes"]))

    print("=== Test 2: clean run: no deviations ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        clean_run_fixture(root, "2026-07-14")
        ev = build_evidence(root, root / "test.db", "2026-07-14")
        check("zero deviations", ev["analysis"]["deviations"] == [],
              str(ev["analysis"]["deviations"]))

    print("=== Test 3: evidence file round-trips via CLI ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        clean_run_fixture(root, "2026-07-14")
        out = subprocess.run(
            [sys.executable,
             str(Path(__file__).resolve().parent.parent / "scripts/generate_postmortem.py"),
             "--run-date", "2026-07-14", "--root", str(root), "--db", str(root / "test.db")],
            capture_output=True, text=True)
        check("CLI exits 0", out.returncode == 0, out.stderr[:200])
        check("CLI prints evidence path", "POSTMORTEM_EVIDENCE:" in out.stdout)
        ev_path = root / "postmortems/2026-07-14-evidence.json"
        check("evidence file written", ev_path.exists())
        if ev_path.exists():
            loaded = json.loads(ev_path.read_text())
            check("evidence JSON parses with analysis", "analysis" in loaded)

    print("=" * 40)
    if FAILURES:
        print(f"FAILED: {FAILURES}")
        return 1
    print("All generate_postmortem tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
