#!/usr/bin/env bash
# Pre-commit gate: run `make check-fast` before every commit and block the
# commit (exit 1) when it fails. WP8 guardrail: catches banned duplication
# patterns, import breakage, contract drift, and veto desync before they
# reach CI.
#
# Invoked by scripts/git-hooks/pre-commit (core.hooksPath: installed by
# scripts/session-start-git-check.sh). Until 2026-07-12 this ran as a
# PreToolUse hook that text-matched 'git commit' in session commands, which
# both under-matched (git -C, sh -c wrappers) and over-matched (commands
# merely mentioning the words). A real git hook fires exactly once per
# actual commit, whoever makes it.
set -u

repo_dir=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$repo_dir" || exit 0

# Docs-only commits skip the gate: EXCEPT references/, whose .md files are
# contract surfaces that check-fast itself validates (scheduled-task-prompt
# via check_prompt_contract, the taste profile via check_veto_sync).
staged=$(git diff --cached --name-only)
if ! printf '%s\n' "$staged" | grep -qvE '^docs/|\.md$' \
   && ! printf '%s\n' "$staged" | grep -q '^references/'; then
  echo "pre-commit: docs-only change: check-fast skipped"
  exit 0
fi

# Sandboxes (scheduled pipeline runs) may lack make/ruff. Skip with a note
# rather than blocking data commits mid-pipeline: CI remains the backstop.
if ! command -v make >/dev/null 2>&1 || ! command -v ruff >/dev/null 2>&1; then
  echo "pre-commit: make/ruff unavailable: check-fast skipped (CI enforces)"
  exit 0
fi

if ! output=$(make check-fast 2>&1); then
  {
    echo "BLOCKED: make check-fast failed: fix the findings below before committing."
    printf '%s\n' "$output" | tail -25
  } >&2
  exit 1
fi
exit 0
