#!/usr/bin/env bash
# Session start: check git state and pull latest
cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || exit 0

# Install the real pre-commit hook (idempotent). Replaces the old PreToolUse
# hook that text-matched 'git commit': see scripts/pre-commit-check.sh.
git config core.hooksPath scripts/git-hooks 2>/dev/null

WARNINGS=""

# Check for other Claude Code CLI sessions. Uses ps instead of pgrep:
# pgrep -x doesn't work from within Claude Code's hook context on macOS.
# grep -cw matches only "claude" (lowercase), ignoring Desktop app.
PEER_COUNT=$(ps -eo comm= 2>/dev/null | grep -cw claude)
if [ "$PEER_COUNT" -gt 1 ]; then
  WARNINGS="${WARNINGS}⚠️ CONCURRENT SESSION RISK: ${PEER_COUNT} Claude Code sessions detected on this machine.\\nBEFORE ANY REPO WORK: confirm no other session is working in this clone. If one is, coordinate with it or move this session to a separate git worktree. Concurrent sessions on the same clone cause data loss: the DB is a single binary file and two writers silently lose one side's work (confirmed 2026-05-29).\\n\\n"
fi

# Check for uncommitted changes
DIRTY=$(git status --porcelain 2>/dev/null)
if [ -n "$DIRTY" ]; then
  WARNINGS="${WARNINGS}UNCOMMITTED CHANGES from a previous session:\n${DIRTY}\n\nThese must be committed and pushed before starting new work, or deliberately discarded.\n\n"
fi

# Check for unpushed commits
UNPUSHED=$(git log origin/main..HEAD --oneline 2>/dev/null)
if [ -n "$UNPUSHED" ]; then
  WARNINGS="${WARNINGS}UNPUSHED COMMITS: exist locally but not on GitHub:\n${UNPUSHED}\n\nPush these before starting new work.\n\n"
fi

# Pull latest
PULL_OUTPUT=$(git pull --ff-only 2>&1)
PULL_EXIT=$?
if [ $PULL_EXIT -ne 0 ]; then
  WARNINGS="${WARNINGS}git pull --ff-only FAILED:\n${PULL_OUTPUT}\n\nLocal branch has diverged from remote. Needs manual resolution.\n\n"
fi

if [ -n "$WARNINGS" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"EVENT RECOMMENDER GIT STATE WARNING:\\n${WARNINGS}Address these before starting new work.\"}}"
else
  echo "Event Recommender: repo clean, up to date with GitHub."
fi
