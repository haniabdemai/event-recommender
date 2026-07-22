"""Pytest wrappers around every offline suite (WP6 task 6.1).

Each module's --smoke-test stays invocable directly (that interface is
part of the compatibility contract); these wrappers give `make test` a
single pytest entry point with per-suite pass/fail reporting.

Run: pytest tests/ -x -q
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Modules exposing --smoke-test. If you add a module with a smoke flag,
# add it here: CI runs this file on every PR.
SMOKE_MODULES = [
    "pipeline/score_candidates.py",
    "pipeline/fetch_meetup.py",
    "pipeline/fetch_luma.py",
    "pipeline/llm_sense_check.py",
    "pipeline/validate_llm_output.py",
    "pipeline/dedup_candidates.py",
    "pipeline/enrich_descriptions.py",
    "pipeline/merge_multiday.py",
    "pipeline/verify_event_dates.py",
    "pipeline/write_notion.py",
    "pipeline/reconcile_notion.py",
    "pipeline/travel_time.py",
    "pipeline/sync_to_gcal.py",
    "pipeline/feedback_digest.py",
    "pipeline/sync_verdicts.py",
]

# Script-style test runners (main() + sys.exit, no pytest functions).
SCRIPT_TESTS = [
    "tests/test_validate_llm_output.py",
    "tests/test_check_no_duplication.py",
    "tests/test_check_prompt_contract.py",
    "tests/test_newsletter_tracker.py",
    "tests/test_check_veto_sync.py",
    "tests/test_qa_autofix.py",
] + sorted(
    str(p.relative_to(REPO)) for p in (REPO / "tests").glob("test_erlib_*.py")
)


def _run(args):
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, (
        f"{' '.join(args)} exited {r.returncode}\n"
        f"--- stdout (tail) ---\n{r.stdout[-3000:]}\n"
        f"--- stderr (tail) ---\n{r.stderr[-3000:]}"
    )


@pytest.mark.parametrize("module", SMOKE_MODULES)
def test_smoke_suite(module):
    _run([module, "--smoke-test"])


@pytest.mark.parametrize("script", SCRIPT_TESTS)
def test_script_runner(script):
    _run([script])
