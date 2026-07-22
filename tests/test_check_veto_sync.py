#!/usr/bin/env python3
"""Tests for scripts/check_veto_sync.py (WP6 task 6.3).

Run: python3 tests/test_check_veto_sync.py
The sync-check must pass on the current tree, and fail loudly when a
veto letter is removed from the LLM instruction, when a Python veto key
appears without a mapping entry, or when a profile marker vanishes.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def run_sync(**overrides):
    args = [sys.executable, str(REPO / "scripts/check_veto_sync.py")]
    for flag, path in overrides.items():
        args.extend([f"--{flag.replace('_', '-')}", str(path)])
    return subprocess.run(args, capture_output=True, text=True, cwd=REPO)


def main():
    # The clean-pass against the real tree lives in `make check-fast` only
    # (CI step 1 + pre-commit hook): not duplicated here (Phase 2, 2026-07-12).

    with tempfile.TemporaryDirectory() as tmp:
        # 1. Removing a veto letter from the LLM instruction fails, naming it
        src = (REPO / "pipeline" / "llm_sense_check.py").read_text()
        mutated = re.sub(r"^\s{2}G\.\s.*?(?=^\s{2}H\.)", "", src,
                         flags=re.M | re.S, count=1)
        assert mutated != src, "mutation did not remove letter G"
        llm_file = Path(tmp) / "llm_mutated.py"
        llm_file.write_text(mutated)
        r = run_sync(llm_file=llm_file)
        check("removed letter G fails", r.returncode == 1)
        check("failure names letter G", "G" in r.stdout)

        # 3. A new unmapped Python veto key fails, naming it
        ssrc = (REPO / "pipeline" / "score_candidates.py").read_text()
        mutated = ssrc.replace(
            'VETO_PATTERNS = {',
            'VETO_PATTERNS = {\n    "brand_new_unmapped_veto": ["xyzzy"],',
            1,
        )
        score_file = Path(tmp) / "score_mutated.py"
        score_file.write_text(mutated)
        r = run_sync(score_file=score_file)
        check("unmapped python key fails", r.returncode == 1)
        check("failure names the key", "brand_new_unmapped_veto" in r.stdout)

        # 4. A profile losing a veto concept fails
        psrc = (REPO / "references/taste-profile-template.md").read_text()
        mutated = psrc.replace("Tabletop", "REDACTED").replace("tabletop", "redacted")
        assert mutated != psrc
        profile_file = Path(tmp) / "profile_mutated.md"
        profile_file.write_text(mutated)
        r = run_sync(profile_file=profile_file)
        check("profile missing a veto concept fails", r.returncode == 1)

    print("OK" if FAIL == 0 else "FAILED")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
