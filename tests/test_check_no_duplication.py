#!/usr/bin/env python3
"""Tests for scripts/check_no_duplication.py. Run: python3 tests/test_check_no_duplication.py"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts/check_no_duplication.py"


def run_guard():
    return subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True, cwd=REPO)


def main():
    ok = True

    # The clean-pass of the guard against the real tree is NOT tested here:
    # `make check-fast` is the single clean-pass authority (CI step 1 and the
    # pre-commit hook). Running it again in the pytest wrappers doubled every
    # guard execution per CI pass (Phase 2 card, 2026-07-12).

    # 1. A planted violation fails and is named
    scratch = REPO / "scratch_violation_test.py"
    scratch.write_text("DUPLICATION_GUARD_SELF_TEST_MARKER = 1\n")
    try:
        r = run_guard()
        if r.returncode == 0:
            print("FAIL: guard did not catch planted marker")
            ok = False
        elif "scratch_violation_test.py" not in r.stdout:
            print(f"FAIL: guard output does not name the offending file:\n{r.stdout}")
            ok = False
        else:
            print("OK  planted violation caught and named")
    finally:
        scratch.unlink()

    # 3. The same marker inside erlib/ is allowed
    erlib = REPO / "erlib"
    created_erlib = not erlib.exists()
    if created_erlib:
        erlib.mkdir()
    allowed = erlib / "scratch_allowed_test.py"
    allowed.write_text("DUPLICATION_GUARD_SELF_TEST_MARKER = 1\n")
    try:
        r = run_guard()
        if r.returncode != 0:
            print(f"FAIL: marker inside erlib/ should be allowed:\n{r.stdout}")
            ok = False
        else:
            print("OK  erlib/ allowlist works")
    finally:
        allowed.unlink()
        if created_erlib:
            erlib.rmdir()

    print("ALL PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
