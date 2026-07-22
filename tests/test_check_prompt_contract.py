#!/usr/bin/env python3
"""Tests for scripts/check_prompt_contract.py. Run: python3 tests/test_check_prompt_contract.py"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK = REPO / "scripts/check_prompt_contract.py"


def run_check(extra_args=None):
    cmd = [sys.executable, str(CHECK)] + (extra_args or [])
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def main():
    ok = True

    # The clean-pass against the real tree lives in `make check-fast` only
    # (CI step 1 + pre-commit hook): not duplicated here (Phase 2, 2026-07-12).

    # 1. A fabricated subcommand + missing script are caught and named
    real_prompt = (REPO / "references/scheduled-task-prompt.md").read_text()
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(real_prompt)
        f.write("\nRun `bash weekly_run.sh made-up-subcommand` then `python3 scripts/no_such_script.py`.\n")
        fake = f.name
    r = run_check(["--prompt-file", fake])
    Path(fake).unlink()
    if r.returncode == 0:
        print("FAIL: fabricated interfaces not caught")
        ok = False
    elif "made-up-subcommand" not in r.stdout or "no_such_script.py" not in r.stdout:
        print(f"FAIL: fabricated interfaces not named in output:\n{r.stdout}")
        ok = False
    else:
        print("OK  fabricated subcommand and missing script caught and named")

    print("ALL PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
