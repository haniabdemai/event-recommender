#!/usr/bin/env python3
"""CI guard: the scheduled-task prompt is a runtime contract.

references/scheduled-task-prompt.md is read by the live weekly trigger at run
time. Every `weekly_run.sh <subcommand>` and `python3 <script>` it names must
exist, or the Tuesday run breaks silently in production. This check makes an
interface-breaking refactor fail CI instead.

Why this exists: docs/audits/2026-07-04-refactor-audit.md §compatibility
contract: the prompt depends on ~22 command interfaces with (previously)
zero test coverage protecting them.
"""
import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def subcommands_in_dispatch(sh_text: str) -> set:
    """Labels of the case-statement dispatch in weekly_run.sh, e.g. 'health-check)'."""
    return set(re.findall(r"^\s{2}([a-z][a-z0-9-]*)\)", sh_text, re.M))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", default=str(REPO / "references/scheduled-task-prompt.md"))
    args = ap.parse_args()

    prompt = Path(args.prompt_file).read_text()
    sh = (REPO / "weekly_run.sh").read_text()

    missing = []

    wanted_subs = set(re.findall(r"weekly_run\.sh\s+([a-z][a-z0-9-]+)", prompt))
    have_subs = subcommands_in_dispatch(sh)
    for sub in sorted(wanted_subs - have_subs):
        missing.append(f"weekly_run.sh subcommand '{sub}' referenced in prompt but not in dispatch")

    wanted_scripts = set(re.findall(r"python3\s+((?:scripts/|pipeline/)?[a-z_]+\.py)", prompt))
    for script in sorted(wanted_scripts):
        if not (REPO / script).exists():
            missing.append(f"script '{script}' referenced in prompt but missing from repo")

    # Every workflow filename the prompt references (dispatch-poll calls and
    # prose mentions alike) must exist in .github/workflows/, or the Tuesday
    # dispatch 404s. Matching every *.yml/*.yaml token rather than only
    # dispatch-poll lines is deliberate: it cannot miss a renamed/archived
    # workflow no matter how the prompt refers to it.
    wanted_workflows = set(re.findall(r"\b[a-z][a-z0-9-]*\.ya?ml\b", prompt))
    for workflow in sorted(wanted_workflows):
        if not (REPO / ".github/workflows" / workflow).exists():
            missing.append(
                f"workflow '{workflow}' referenced in prompt but missing from .github/workflows/"
            )

    if missing:
        print("Prompt contract FAILED:")
        print("\n".join(missing))
        return 1
    print(
        f"Prompt contract OK ({len(wanted_subs)} subcommands, "
        f"{len(wanted_scripts)} scripts, {len(wanted_workflows)} workflows checked)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
