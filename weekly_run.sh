#!/usr/bin/env bash
# Thin-shell orchestrator for the weekly Event Recommender run.
#
# Called from the Claude Code scheduled task prompt. All deterministic plumbing
# lives here; only LLM-judgement steps (Gmail parsing, sense-check) stay in the
# scheduled task prompt. This is how we prevent prompt/repo drift: pipeline
# changes land in this file in git, not in a frozen prompt snapshot.
#
# Usage:
#   bash weekly_run.sh preflight    # schema check, dedup check, emits env
#   bash weekly_run.sh score-travel # score candidates + travel time lookup
#   bash weekly_run.sh write-commit # Notion write, log run, commit, push
#   bash weekly_run.sh status       # prints the last run's status JSON
#
# preflight exit codes:
#   0 = CONTINUE  (safe to run; logs a note if a previous run exists this week)
#   1 = FAIL      (schema drift or DB missing: alert and exit)
#
# score-travel exit codes:
#   0 = success (scoring + travel time complete)
#   1 = failure (see .last_run_status.json)
#
# write-commit exit codes:
#   0 = success (Notion write + commit + push done)
#   1 = failure (see .last_run_status.json)
#
# The scheduled task sends a status email via Gmail MCP after write-commit.
# It reads .last_run_status.json and .last_llm_summary.json.

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Scheduled tasks write secrets to .env (shell state doesn't persist between calls)
[ -f .env ] && source .env

DB="event-recommender.db"
STATUS_FILE=".last_run_status.json"
# Topic lives in erlib.config.NTFY_TOPIC (set NTFY_TOPIC in .env): the
# single source of truth. Empty topic = notifications disabled; ntfy_send
# below no-ops rather than posting to the bare host.
NTFY_TOPIC="${NTFY_TOPIC:-$(python3 -c 'from erlib.config import NTFY_TOPIC; print(NTFY_TOPIC)' 2>/dev/null)}" || NTFY_TOPIC=""
if [ -z "$NTFY_TOPIC" ]; then
  echo "NOTE: NTFY_TOPIC not configured: phone alerts disabled for this run" >&2
fi
NTFY_URL="https://ntfy.sh/${NTFY_TOPIC}"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

week_start() {
  python3 -c "from datetime import date, timedelta; t=date.today(); print((t - timedelta(days=t.isoweekday()-1)).isoformat())"
}

today_utc() {
  date -u +%Y-%m-%d
}

current_branch() {
  git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main"
}

write_status() {
  # write_status <phase> <outcome> <detail>
  # 1. Overwrites .last_run_status.json (existing behaviour: all consumers unchanged)
  # 2. Appends to .pipeline_run_log.json (chronological step log for postmortems/debugging)
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys, datetime, os

phase, outcome, detail = sys.argv[1], sys.argv[2], sys.argv[3]
ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

payload = {
  "phase": phase,
  "outcome": outcome,
  "detail": detail,
  "timestamp_utc": ts,
}
with open(".last_run_status.json", "w") as f:
    json.dump(payload, f, indent=2)

entry = {"step": phase, "outcome": outcome, "detail": detail, "timestamp_utc": ts}
log_path = ".pipeline_run_log.json"
try:
    with open(log_path) as f:
        log = json.load(f)
    if not isinstance(log, list):
        log = []
except (FileNotFoundError, json.JSONDecodeError):
    log = []
log.append(entry)
with open(log_path, "w") as f:
    json.dump(log, f, indent=2)
PY
}

validate_db_before_push() {
  # Guard against DB regression: verify no run_dates vanish and row count doesn't drop.
  # Compares local DB against the remote version on GitHub.
  python3 - "$DB" <<'PY'
import sqlite3, subprocess, json, sys, tempfile, os

local_db = sys.argv[1]

# Get remote DB download URL
result = subprocess.run(
    ["gh", "api", f"repos/{os.environ.get('ER_REPO_SLUG', 'haniabdemai/event-recommender')}/contents/event-recommender.db", "--jq", ".download_url"],
    capture_output=True, text=True
)
if result.returncode != 0:
    print("VALIDATE_DB: FAIL: could not fetch remote DB URL (check GH_TOKEN)", file=sys.stderr)
    sys.exit(1)

remote_url = result.stdout.strip()
fd, remote_path = tempfile.mkstemp(prefix="remote_db_check_", suffix=".db")
os.close(fd)
dl = subprocess.run(["curl", "-sL", "-o", remote_path, remote_url], capture_output=True)
# mkstemp guarantees the file exists, so an empty file is the download-failure signal
if dl.returncode != 0 or os.path.getsize(remote_path) == 0:
    print("VALIDATE_DB: WARNING: could not download remote DB, skipping check")
    os.remove(remote_path)
    sys.exit(0)

try:
    local_conn = sqlite3.connect(local_db)
    remote_conn = sqlite3.connect(remote_path)

    local_count = local_conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    remote_count = remote_conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    local_dates = set(r[0] for r in local_conn.execute("SELECT DISTINCT run_date FROM candidates").fetchall())
    remote_dates = set(r[0] for r in remote_conn.execute("SELECT DISTINCT run_date FROM candidates").fetchall())

    local_conn.close()
    remote_conn.close()

    missing_dates = remote_dates - local_dates
    if missing_dates:
        print(f"VALIDATE_DB: ABORT: local DB is missing run_dates present in remote: {sorted(missing_dates)}")
        print(f"  Local has {local_count} rows ({sorted(local_dates)})")
        print(f"  Remote has {remote_count} rows ({sorted(remote_dates)})")
        sys.exit(1)

    if local_count < remote_count:
        print(f"VALIDATE_DB: ABORT: local DB has {local_count} rows but remote has {remote_count}. Refusing to push a regression.")
        sys.exit(1)

    print(f"VALIDATE_DB: OK: {local_count} rows (remote: {remote_count}), no run_dates lost")
finally:
    os.remove(remote_path)
PY
}

commit_and_push() {
  # Commit staged changes and push to origin.
  # Caller must stage files (git add/rm) before calling.
  # Callers own error reporting: this function is silent on failure.
  #
  # Exit codes:
  #   0 = committed and pushed successfully
  #   1 = nothing staged (no-op, not an error)
  #   2 = git commit failed
  #   3 = git push failed
  local msg="$1"

  if git diff --cached --quiet 2>/dev/null; then
    return 1
  fi

  if ! git commit -m "$msg"; then
    return 2
  fi

  if ! git push origin HEAD; then
    return 3
  fi

  return 0
}


schema_check() {
  # Schema gate lives in erlib/db.py (extracted from a heredoc in WP4;
  # tested by tests/test_erlib_db.py). Prints SCHEMA_OK / SCHEMA_FAIL.
  python3 -m erlib.db "$DB"
}

# Warn if any script uses pipeline_state for Notion visibility queries.
# Catches both direct equality (= 'written') and IN clauses, plus proxy
# patterns like != 'vetoed' used to approximate "in Notion".
# Excludes: pipeline-state management (pipeline/write_notion.py, pipeline/score_candidates.py,
# pipeline/llm_sense_check.py, pipeline/validate_llm_output.py, pipeline/dedup_candidates.py),
# pipeline verification (verify_pipeline_run.py), and fetch scripts
# (fetch_*.py set initial state).
backfill_query_check() {
  local hits
  hits=$(grep -rnE "pipeline_state.*(=|IN|!=).*'(written|vetoed|llm_rejected)'" \
    pipeline/enrich_descriptions.py pipeline/travel_time.py \
    scripts/*.py 2>/dev/null \
    | grep -v "verify_pipeline_run.py\|qa_autofix.py\|verify_newsletter_extraction.py" \
    | grep -v "NOT IN" \
    || true)
  if [ -n "$hits" ]; then
    echo "QUERY_WARN: scripts use pipeline_state instead of notion_status:" >&2
    echo "$hits" >&2
    echo "  Fix: use notion_status = 'active' for Notion visibility queries" >&2
    echo "  See CLAUDE.md for the full rule" >&2
  fi
}

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

cmd_preflight() {
  # Clear the run log from any previous pipeline run
  rm -f .pipeline_run_log.json

  # 1) DB exists
  if [ ! -f "$DB" ]; then
    write_status preflight fail "Database file $DB not found in repo."
    echo "PREFLIGHT_FAIL: no database" >&2
    return 1
  fi

  # 2) Schema drift check
  if ! schema_check; then
    write_status preflight fail "Schema drift detected: see stderr."
    return 1
  fi

  # 3) Code hygiene: backfill queries should use notion_status, not pipeline_state
  backfill_query_check

  # 3) Expire stale pending_llm candidates
  EXPIRED=$(python3 - "$DB" <<'EXPIRY_PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
past = conn.execute("""
    UPDATE candidates SET pipeline_state = 'expired'
    WHERE pipeline_state IN ('pending_llm', 'pending_travel', 'ready_to_write', 'write_failed', 'incomplete')
      AND COALESCE(end_date, date) < date('now')
""").rowcount
conn.commit()
conn.close()
print(past)
EXPIRY_PY
)
  if [ "$EXPIRED" -gt 0 ]; then
    echo "PREFLIGHT_EXPIRE: $EXPIRED past-date candidates expired"
  fi

  # 4) Compute date vars
  WEEK_START=$(week_start)
  TODAY=$(today_utc)
  SCORE_CUTOFF=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=1)).isoformat())")

  # Record pipeline start time for duration tracking
  python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())" > .pipeline_start_time

  write_status preflight continue "Week $WEEK_START pending; proceeding."
  echo "PREFLIGHT_CONTINUE"
  echo "WEEK_START=$WEEK_START"
  echo "SCORE_CUTOFF=$SCORE_CUTOFF"
  echo "TODAY=$TODAY"
  return 0
}

# ---------------------------------------------------------------------------
# dispatch-poll  (dispatch a GitHub Action and poll by run ID)
# ---------------------------------------------------------------------------
# Usage: cmd_dispatch_poll <workflow.yml> [timeout_seconds]
# Exit codes: 0=success, 1=completed-failure, 2=dispatch-failed, 3=run-not-found, 4=timeout
# Auth: gh api handles auth automatically (GITHUB_TOKEN env or gh auth store)

cmd_dispatch_poll() {
  local workflow="$1"
  local timeout="${2:-600}"
  local name="${workflow%.yml}"
  local current_branch
  current_branch=$(current_branch)

  # Try gh CLI first (works locally and in environments where gh is installed)
  if command -v gh &>/dev/null; then
    echo "DISPATCHING (gh api): ${workflow} on ${current_branch} ..."
    # NOTE: call _dispatch_poll_gh as its own statement and capture $? immediately.
    # Do NOT wrap it in `if _dispatch_poll_gh ...; then return 0; fi`: when the
    # condition is false and there is no else, bash defines the if-construct's own
    # exit status as 0, so a later `local gh_exit=$?` would read 0 (success) even
    # though _dispatch_poll_gh actually failed. That swallowed every non-zero exit
    # code (DISPATCH_FAILED=1, DISPATCH_TIMEOUT=4, etc.) and made this function
    # always return 0 on the gh path.
    _dispatch_poll_gh "$workflow" "$timeout" "$current_branch"
    local gh_exit=$?
    if [ "$gh_exit" -eq 0 ]; then
      return 0
    fi
    echo "DISPATCH_WARN: gh api path failed (exit $gh_exit), no fallback available" >&2
    return $gh_exit
  fi

  # Fallback: push-trigger approach (for sandboxes where api.github.com is blocked)
  echo "DISPATCHING (push-trigger): ${name} on ${current_branch} ..."
  _dispatch_poll_push "$name" "$timeout" "$current_branch"
}

_dispatch_poll_gh() {
  local workflow="$1" timeout="$2" current_branch="$3"
  local repo="${ER_REPO_SLUG:-haniabdemai/event-recommender}"

  local dispatch_time
  dispatch_time=$(python3 -c "
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(seconds=10)).strftime('%Y-%m-%dT%H:%M:%SZ'))
")

  if ! gh api -X POST \
    "repos/${repo}/actions/workflows/${workflow}/dispatches" \
    -f ref="${current_branch}" --silent 2>/dev/null; then
    echo "DISPATCH_FAILED: gh api returned error" >&2
    return 2
  fi
  echo "DISPATCH_OK"

  echo "FINDING_RUN: waiting for run to appear ..."
  local run_id=""
  local find_elapsed=0
  while [ $find_elapsed -lt 60 ] && [ -z "$run_id" ]; do
    sleep 5
    find_elapsed=$((find_elapsed + 5))
    run_id=$(gh api \
      "repos/${repo}/actions/workflows/${workflow}/runs?per_page=5&branch=${current_branch}" \
      | python3 "${SCRIPT_DIR}/scripts/find_dispatched_run.py" "${dispatch_time}" 2>/dev/null)
    [ -z "$run_id" ] && echo "FINDING_RUN: retry (${find_elapsed}s) ..."
  done

  if [ -z "$run_id" ]; then
    echo "FINDING_RUN: fallback to most-recent-run"
    run_id=$(gh api \
      "repos/${repo}/actions/workflows/${workflow}/runs?per_page=1&branch=${current_branch}" \
      --jq '.workflow_runs[0].id // empty' 2>/dev/null)
  fi

  if [ -z "$run_id" ]; then
    echo "DISPATCH_ERROR: no runs found for ${workflow}" >&2
    return 3
  fi
  echo "RUN_FOUND: ${run_id}"

  local elapsed=0
  local poll_failures=0
  while [ $elapsed -lt "$timeout" ]; do
    sleep 10
    elapsed=$((elapsed + 10))
    local status_line
    status_line=$(gh api "repos/${repo}/actions/runs/${run_id}" \
      --jq '[.status, (.conclusion // "")] | join(" ")' 2>/dev/null) || true
    if [ -z "$status_line" ]; then
      poll_failures=$((poll_failures + 1))
      echo "POLL: empty response (${elapsed}s, failure ${poll_failures}/3)" >&2
      [ "$poll_failures" -ge 3 ] && return 3
      continue
    fi
    poll_failures=0
    echo "POLL: ${status_line} (${elapsed}s)"
    case "$status_line" in
      "completed success") echo "DISPATCH_DONE: ${workflow} completed in ${elapsed}s"; return 0 ;;
      "completed "*) echo "DISPATCH_FAILED: ${workflow} ${status_line}" >&2; return 1 ;;
    esac
  done

  echo "DISPATCH_TIMEOUT: ${workflow} after ${timeout}s" >&2
  return 4
}

_dispatch_poll_push() {
  local name="$1" timeout="$2" current_branch="$3"

  mkdir -p .dispatch

  # Clean up stale dispatch files (commit only: bundled into the next push if it succeeds)
  git rm -f ".dispatch/${name}.trigger" ".dispatch/${name}.done" 2>/dev/null || true
  if ! git diff --cached --quiet 2>/dev/null; then
    git commit -m "cleanup: stale ${name} dispatch files" 2>/dev/null
  fi

  # Create trigger file and push
  date -u +%Y-%m-%dT%H:%M:%SZ > ".dispatch/${name}.trigger"
  git add ".dispatch/${name}.trigger"
  commit_and_push "dispatch: ${name}"
  local cp_exit=$?
  if [ "$cp_exit" -eq 1 ]; then
    echo "DISPATCH_FAILED: nothing staged to commit" >&2
    return 2
  elif [ "$cp_exit" -eq 2 ]; then
    echo "DISPATCH_FAILED: git commit failed" >&2
    return 2
  elif [ "$cp_exit" -eq 3 ]; then
    echo "DISPATCH_FAILED: git push failed" >&2
    return 2
  fi
  echo "DISPATCH_OK: pushed .dispatch/${name}.trigger"

  # Poll for .done file on remote
  local elapsed=0
  while [ $elapsed -lt "$timeout" ]; do
    sleep 15
    elapsed=$((elapsed + 15))

    git fetch origin "$current_branch" --quiet 2>/dev/null || true
    local done_content
    done_content=$(git show "origin/${current_branch}:.dispatch/${name}.done" 2>/dev/null) || true

    if [ -n "$done_content" ]; then
      echo "DISPATCH_SIGNAL: ${name} → ${done_content}"
      git pull --rebase origin "$current_branch" --quiet 2>/dev/null || true

      # Clean up dispatch files
      git rm -f ".dispatch/${name}.trigger" ".dispatch/${name}.done" 2>/dev/null || true
      commit_and_push "cleanup: ${name} dispatch" 2>/dev/null || true

      case "$done_content" in
        success) echo "DISPATCH_DONE: ${name} completed in ${elapsed}s"; return 0 ;;
        *) echo "DISPATCH_FAILED: ${name} ${done_content}" >&2; return 1 ;;
      esac
    fi

    echo "POLL: waiting for .dispatch/${name}.done (${elapsed}s/${timeout}s)"
  done

  echo "DISPATCH_TIMEOUT: ${name} after ${timeout}s" >&2
  return 4
}

# ---------------------------------------------------------------------------
# apply-findings  (dispatches apply-findings.yml: runs BEFORE scoring)
# ---------------------------------------------------------------------------

cmd_apply_findings() {
  write_status apply-findings running "Checking for approved scoring findings"

  if cmd_dispatch_poll apply-findings.yml 120; then
    local current_branch
    current_branch=$(current_branch)
    git pull --rebase origin "$current_branch" 2>/dev/null
    write_status apply-findings done "Applied approved findings"
  else
    # Single-line: $? expands before local executes, so value is captured
    local exit_code=$?
    if [ "$exit_code" -eq 2 ]; then
      write_status apply-findings skipped "Could not dispatch apply-findings action"
    else
      write_status apply-findings warning "Apply action failed (exit ${exit_code})"
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# enrich-descriptions
# ---------------------------------------------------------------------------

cmd_enrich_descriptions() {
  if ! python3 pipeline/enrich_descriptions.py --prepare; then
    write_status enrich-descriptions fail "pipeline/enrich_descriptions.py --prepare failed."
    return 1
  fi

  ENRICH_TOTAL=$(python3 -c "import json; print(json.loads(open('.enrich_batches.json').read()).get('total', 0))")
  if [ "$ENRICH_TOTAL" -eq 0 ]; then
    write_status enrich-descriptions success "No candidates need description enrichment."
    echo "ENRICH_SKIP"
    return 0
  fi

  write_status enrich-descriptions success "$ENRICH_TOTAL candidates need description enrichment."
  echo "ENRICH_READY: $ENRICH_TOTAL candidates in .enrich_batches.json"
  return 0
}

# ---------------------------------------------------------------------------
# score-travel
# ---------------------------------------------------------------------------

cmd_score_travel() {
  WEEK_START=$(week_start)
  TODAY=$(today_utc)

  # Required env: GOOGLE_MAPS_API_KEY
  for v in GOOGLE_MAPS_API_KEY; do
    if [ -z "${!v:-}" ]; then
      write_status score-travel fail "Missing env var $v."
      echo "SCORE_TRAVEL_FAIL: missing $v" >&2
  
      return 1
    fi
  done

  # Step 0: merge any multi-day splits from newsletter extraction
  python3 pipeline/merge_multiday.py --apply || true

  # Step A: score
  if ! python3 pipeline/score_candidates.py; then
    write_status score-travel fail "pipeline/score_candidates.py failed."

    return 1
  fi

  # Step B: travel time
  if ! python3 pipeline/travel_time.py; then
    write_status score-travel fail "pipeline/travel_time.py failed."

    return 1
  fi

  write_status score-travel success "Scoring and travel time complete."
  echo "SCORE_TRAVEL_OK"
  return 0
}

# ---------------------------------------------------------------------------
# dedup  (cross-batch duplicate detection: Stage 1 only)
# ---------------------------------------------------------------------------
# Runs the deterministic pre-filter. If potential duplicates are found,
# the scheduled task prompt spawns a subagent for Stage 2 (LLM confirmation).

cmd_dedup() {
  if ! python3 pipeline/dedup_candidates.py --prepare; then
    write_status dedup fail "pipeline/dedup_candidates.py --prepare failed."
    return 1
  fi

  PAIR_COUNT=$(python3 -c "import json; print(len(json.loads(open('.dedup_pairs.json').read())))")
  if [ "$PAIR_COUNT" -eq 0 ]; then
    write_status dedup success "No potential duplicates found."
    echo "DEDUP_SKIP: no similar-name date matches"
    return 0
  fi

  write_status dedup success "$PAIR_COUNT candidate groups have potential duplicates."
  echo "DEDUP_READY: $PAIR_COUNT groups in .dedup_pairs.json"
  return 0
}

# ---------------------------------------------------------------------------
# validate-write-ready
# ---------------------------------------------------------------------------

cmd_validate_write_ready() {
  # Validation logic lives in pipeline/write_ready_check.py (extracted from a heredoc
  # in WP4; behaviour pinned by tests/test_write_ready.sh).
  python3 pipeline/write_ready_check.py "$DB"
  local py_exit=$?
  if [ "$py_exit" -ne 0 ]; then
    echo "VALIDATE_WRITE_READY: Python validation failed (exit $py_exit)" >&2
    return 1
  fi

  git add "$DB"
  commit_and_push "Step 5B: validate-write-ready applied"
  local cp_exit=$?
  if [ "$cp_exit" -eq 2 ]; then
    echo "VALIDATE_WRITE_READY: git commit failed" >&2
    return 1
  elif [ "$cp_exit" -eq 3 ]; then
    echo "VALIDATE_WRITE_READY: push failed: Step 6 must not proceed with stale DB" >&2
    return 1
  fi
}

# write-commit
# ---------------------------------------------------------------------------

cmd_write_commit() {
  # Clean stale error diagnostic from a previous failed run
  git rm -f --ignore-unmatch .last_write_error.json 2>/dev/null || true

  WEEK_START=$(week_start)
  TODAY=$(today_utc)

  # Required env: NOTION_TOKEN, NOTION_DATABASE_ID
  for v in NOTION_TOKEN NOTION_DATABASE_ID; do
    if [ -z "${!v:-}" ]; then
      write_status write-commit fail "Missing env var $v."
      echo "WRITE_COMMIT_FAIL: missing $v" >&2
  
      return 1
    fi
  done

  # Validate LLM output: auto-corrects obvious LLM mistakes before Notion write.
  # Candidates the LLM wrongly passed are set to "Not Recommended" and excluded
  # from the Notion write. Pipeline continues with the corrected data.
  # Exit 0 = clean. Exit 3 = travel gaps: retry pipeline/travel_time.py then re-validate
  # (max 1 retry; persistent gaps are tolerated). ANY other exit = validator
  # crash: the corrections never ran, so writing would bypass the gate. Abort.
  python3 pipeline/validate_llm_output.py
  VALIDATOR_EXIT=$?
  if [ "$VALIDATOR_EXIT" -eq 3 ]; then
    echo "RETRY: Validator found travel gaps. Re-running pipeline/travel_time.py..."
    python3 pipeline/travel_time.py
    python3 pipeline/validate_llm_output.py
    VALIDATOR_EXIT=$?
    if [ "$VALIDATOR_EXIT" -eq 3 ]; then
      echo "WARN: Travel gaps remain after retry: proceeding to write (genuinely unresolvable venues)."
      VALIDATOR_EXIT=0
    fi
  fi
  if [ "$VALIDATOR_EXIT" -ne 0 ]; then
    write_status write-commit fail "pipeline/validate_llm_output.py crashed (exit $VALIDATOR_EXIT): aborting before Notion write. LLM corrections were NOT applied."
    echo "WRITE_COMMIT_FAIL: validator crashed exit=$VALIDATOR_EXIT" >&2
    return 1
  fi

  # Pre-write split-state check: detect and recover orphan Notion pages.
  # Must run before pipeline/write_notion.py to prevent writing on top of dirty state.
  # Exit 0 = clean or all recovered. Exit 1 = network error (soft-fail).
  # Exit 4 = unrecoverable orphans (hard-fail). Any other exit = crash: abort.
  python3 pipeline/reconcile_notion.py --recover
  RECOVER_EXIT=$?
  if [ "$RECOVER_EXIT" -eq 4 ]; then
    write_status write-commit fail "Unrecoverable split state: orphan Notion pages could not be matched to candidates. See .last_split_state_report.json."
    return 1
  elif [ "$RECOVER_EXIT" -eq 1 ]; then
    echo "WARNING: Could not reach Notion for pre-write split-state check (exit $RECOVER_EXIT): proceeding" >&2
  elif [ "$RECOVER_EXIT" -eq 0 ]; then
    echo "PRE_WRITE_CHECK: clean (no orphans, or all recovered)"
  else
    write_status write-commit fail "pipeline/reconcile_notion.py --recover returned unexpected exit $RECOVER_EXIT: aborting before Notion write."
    echo "WRITE_COMMIT_FAIL: reconcile --recover unexpected exit=$RECOVER_EXIT" >&2
    return 1
  fi

  # Notion write
  if ! python3 pipeline/write_notion.py; then
    write_status write-commit fail "pipeline/write_notion.py failed."

    return 1
  fi

  # Reconcile Notion page statuses.
  # Exit 0 = clean, exit 1 = network error (soft-fail), exit 2 = orphans found
  # (hard-fail). Any other exit = crash: abort so the operator investigates
  # before the DB is pushed on top of an unverified write.
  python3 pipeline/reconcile_notion.py
  RECONCILE_EXIT=$?
  if [ "$RECONCILE_EXIT" -eq 2 ]; then
    write_status write-commit fail "pipeline/reconcile_notion.py found orphan Notion pages: DB/Notion out of sync. Investigate."

    return 1
  elif [ "$RECONCILE_EXIT" -eq 1 ]; then
    echo "WARNING: pipeline/reconcile_notion.py could not reach Notion (exit $RECONCILE_EXIT): continuing" >&2
  elif [ "$RECONCILE_EXIT" -ne 0 ]; then
    write_status write-commit fail "pipeline/reconcile_notion.py returned unexpected exit $RECONCILE_EXIT after the Notion write: investigate before pushing."
    echo "WRITE_COMMIT_FAIL: post-write reconcile unexpected exit=$RECONCILE_EXIT" >&2
    return 1
  fi

  # Log run with source breakdown (erlib.run_log, was an inline heredoc)
  python3 -m erlib.run_log "$TODAY" "$DB"

  ADDED=$(python3 -c "import sqlite3; print(sqlite3.connect('$DB').execute(\"SELECT COUNT(*) FROM candidates WHERE pipeline_state='written' AND run_date='$TODAY'\").fetchone()[0])")

  # Validate DB before push: catches regressions (lost run_dates, row count drops)
  if ! validate_db_before_push; then
    write_status write-commit fail "DB validation failed: refusing to push. See output above."

    return 1
  fi

  # Commit and push
  git add "$DB"
  commit_and_push "Weekly run $TODAY: $ADDED events added to Notion"
  local cp_exit=$?
  if [ "$cp_exit" -eq 2 ]; then
    write_status write-commit fail "git commit failed."

    return 1
  elif [ "$cp_exit" -eq 3 ]; then
    write_status write-commit fail "git push failed. DB updated locally but not in remote."

    return 1
  fi

  write_status write-commit success "Added $ADDED events to Notion for week $WEEK_START."

  echo "WRITE_COMMIT_OK: $ADDED events added"
  return 0
}

# ---------------------------------------------------------------------------
# pipeline-summary  (generates .last_pipeline_summary.json for Notion run log)
# ---------------------------------------------------------------------------

cmd_pipeline_summary() {
  TODAY=$(today_utc)
  TRIGGER_TYPE="${TRIGGER_TYPE:-Automatic}"

  # Summary logic lives in pipeline/pipeline_summary.py (extracted from a heredoc in
  # WP4; golden-master pinned by tests/test_pipeline_summary_golden.sh).
  python3 pipeline/pipeline_summary.py "$TODAY" "$DB" "$TRIGGER_TYPE"

  # Send ntfy push notification (always fires, regardless of outcome)
  if [ -f ".last_pipeline_summary.json" ]; then
    NTFY_STATUS=$(python3 -c "import json; d=json.load(open('.last_pipeline_summary.json')); print(d.get('status','?'))")
    NTFY_EVENTS=$(python3 -c "import json; d=json.load(open('.last_pipeline_summary.json')); print(d.get('events_added',0))")
    NTFY_ISSUES=$(python3 -c "import json; d=json.load(open('.last_pipeline_summary.json')); print(d.get('issues','?'))")
    NTFY_TITLE="Event Recommender: ${NTFY_STATUS}"
    NTFY_BODY="${NTFY_EVENTS} events added"
    if [ "$NTFY_ISSUES" != "No issues" ]; then
      NTFY_BODY="${NTFY_BODY} | Issues: ${NTFY_ISSUES}"
    fi
    [ -n "$NTFY_TOPIC" ] && curl -s -o /dev/null -H "Title: ${NTFY_TITLE}" -d "${NTFY_BODY}" "${NTFY_URL}" || true
    echo "NTFY_SENT: ${NTFY_TITLE}"
  fi

}

# ---------------------------------------------------------------------------
# postmortem: assemble the evidence file for the per-run post-mortem (8C).
# Deterministic only; the narrative is written by a subagent that reads the
# evidence file (see references/scheduled-task-prompt.md Step 8C).
# ---------------------------------------------------------------------------

cmd_postmortem() {
  TODAY=$(today_utc)
  python3 scripts/generate_postmortem.py --run-date "$TODAY" --db "$DB"
}

# ---------------------------------------------------------------------------
# retry-failed: reset write_failed candidates back to ready_to_write
# ---------------------------------------------------------------------------

cmd_retry_failed() {
  python3 - "$DB" <<'PY'
import sqlite3, sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

failed = list(conn.execute(
    "SELECT id, name, date FROM candidates WHERE pipeline_state = 'write_failed'"
))

if not failed:
    print("RETRY_FAILED: 0 candidates in write_failed: nothing to retry")
    sys.exit(0)

for row in failed:
    print(f"  RESET [id={row['id']}] {row['name']!r} ({row['date']})")

conn.execute("UPDATE candidates SET pipeline_state = 'ready_to_write' WHERE pipeline_state = 'write_failed'")
conn.commit()
conn.close()
print(f"RETRY_FAILED: {len(failed)} candidates reset to ready_to_write")
PY
}

# ---------------------------------------------------------------------------
# record-newsletters: backfill processed_emails for newsletter dedup/Sources
# ---------------------------------------------------------------------------
# Safety net for when subagents don't call pipeline/newsletter_tracker.py --record.
# Uses --check before --record to avoid overwriting body_text from subagents.
# Reads metadata file first (has email_date/subject); falls back to batch JSON.

cmd_record_newsletters() {
  # Batch logic lives in pipeline/newsletter_tracker.py --record-batch (extracted from
  # a heredoc in WP4; behaviour pinned by tests/test_record_newsletters.sh).
  python3 pipeline/newsletter_tracker.py --record-batch --db "$DB"
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

cmd_status() {
  if [ -f "$STATUS_FILE" ]; then
    cat "$STATUS_FILE"
  else
    echo '{"phase":"none","outcome":"unknown","detail":"No status file found."}'
  fi
}

# ---------------------------------------------------------------------------
# health-check  (Step 0: validate credentials before pipeline starts)
# ---------------------------------------------------------------------------
# Tests GitHub PAT (auth + scopes) and Maps API key (Routes API connectivity).
# Notion token is tested at write time in GitHub Actions (sandbox blocks api.notion.com).
# Gmail MCP is tested by the orchestrator in the prompt (not accessible from bash).

cmd_health_check() {
  local failures=0
  local details=""

  # 1. GitHub PAT: verify authentication via git ls-remote
  #    The sandbox proxy blocks api.github.com and gh CLI isn't installed.
  #    git ls-remote authenticates over HTTPS to github.com (allowed by proxy).
  if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "HEALTH_FAIL: GITHUB_TOKEN not set"
    details="${details}GITHUB_TOKEN not set. "
    failures=$((failures + 1))
  else
    local gh_ok=false
    local gh_attempt=0
    local gh_max_attempts=3
    local gh_retry_delay="${HEALTH_CHECK_RETRY_DELAY:-5}"
    local gh_err=""

    while [ $gh_attempt -lt $gh_max_attempts ]; do
      gh_attempt=$((gh_attempt + 1))
      # Token goes in an Authorization header, never in the URL: URLs leak
      # into error output and process listings.
      if gh_err=$(git -c http.extraHeader="Authorization: Bearer ${GITHUB_TOKEN}" \
        ls-remote --exit-code \
        "https://github.com/${ER_REPO_SLUG:-haniabdemai/event-recommender}.git" \
        HEAD 2>&1 >/dev/null); then
        gh_ok=true
        break
      fi
      if [ $gh_attempt -lt $gh_max_attempts ]; then
        echo "HEALTH_RETRY: git ls-remote failed, retrying in ${gh_retry_delay}s (attempt $gh_attempt/$gh_max_attempts)"
        sleep "$gh_retry_delay"
      fi
    done

    if [ "$gh_ok" = true ]; then
      echo "HEALTH_OK: GitHub PAT valid (git auth, attempt $gh_attempt/$gh_max_attempts)"
    else
      echo "HEALTH_FAIL: GitHub PAT rejected by git ls-remote after $gh_max_attempts attempts"
      details="${details}GitHub PAT auth failed (git). "
      failures=$((failures + 1))
    fi
  fi

  # 2. Google Maps API: minimal Routes API request to verify key works
  if [ -z "${GOOGLE_MAPS_API_KEY:-}" ]; then
    echo "HEALTH_FAIL: GOOGLE_MAPS_API_KEY not set"
    details="${details}GOOGLE_MAPS_API_KEY not set. "
    failures=$((failures + 1))
  else
    local maps_http
    maps_http=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
      "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix" \
      -H "X-Goog-Api-Key: ${GOOGLE_MAPS_API_KEY}" \
      -H "Content-Type: application/json" \
      -H "X-Goog-FieldMask: originIndex" \
      -d '{"origins":[{"waypoint":{"address":"London"}}],"destinations":[{"waypoint":{"address":"London"}}],"travelMode":"DRIVE"}' 2>/dev/null)

    if [ "$maps_http" = "200" ]; then
      echo "HEALTH_OK: Google Maps API key valid"
    elif [ "$maps_http" = "429" ]; then
      echo "HEALTH_OK: Google Maps API key valid (rate-limited, will retry in pipeline)"
    else
      echo "HEALTH_FAIL: Google Maps API returned HTTP $maps_http (expected 200)"
      details="${details}Maps API HTTP ${maps_http}. "
      failures=$((failures + 1))
    fi
  fi

  # 3. Credential probes (WP9.2): PAT scopes, Notion token, Google refresh
  #    token, Gmail token. Exit 0=OK, 1=BAD credential, 2=SKIP (creds not in
  #    env, or the endpoint is proxy-blocked in this sandbox: never a
  #    failure). Each BAD credential fires its own ntfy naming what to fix.
  local probe probe_out probe_exit
  if [ -f "${SCRIPT_DIR}/scripts/health_probes.py" ]; then
    for probe in github-scopes notion google gmail; do
      probe_out=$(python3 "${SCRIPT_DIR}/scripts/health_probes.py" "$probe" 2>&1) && probe_exit=0 || probe_exit=$?
      echo "HEALTH_PROBE(${probe}): ${probe_out}"
      if [ "$probe_exit" -eq 1 ]; then
        details="${details}${probe}: ${probe_out#FAIL: }. "
        failures=$((failures + 1))
        [ -n "$NTFY_TOPIC" ] && curl -s -o /dev/null \
          -H "Title: Pipeline credential FAILED: ${probe}" \
          -H "Priority: high" \
          -d "${probe_out#FAIL: }" \
          "${NTFY_URL}" || true
      fi
    done
  else
    echo "HEALTH_PROBE: scripts/health_probes.py not present: probes skipped"
  fi

  # Summary
  if [ "$failures" -gt 0 ]; then
    write_status health-check fail "$failures credential(s) failed: ${details}"
    [ -n "$NTFY_TOPIC" ] && curl -s -o /dev/null \
      -H "Title: Pipeline: credential check FAILED" \
      -d "${failures} failed: ${details}" \
      "${NTFY_URL}" || true
    echo "HEALTH_CHECK_FAIL: $failures credential(s) failed"
    return 1
  fi

  write_status health-check ok "All testable credentials valid"
  echo "HEALTH_CHECK_OK: all credentials valid"
  return 0
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "${1:-}" in
  health-check)      cmd_health_check ;;
  preflight)         cmd_preflight ;;
  dispatch-poll)     shift; cmd_dispatch_poll "$@" ;;
  apply-findings)    cmd_apply_findings ;;
  enrich-descriptions) cmd_enrich_descriptions ;;
  score-travel)      cmd_score_travel ;;
  dedup)             cmd_dedup ;;
  validate-write-ready) cmd_validate_write_ready ;;
  write-commit)      cmd_write_commit ;;
  pipeline-summary)  cmd_pipeline_summary ;;
  postmortem)        cmd_postmortem ;;
  retry-failed)      cmd_retry_failed ;;  # operator-only, not in the scheduled prompt
  record-newsletters) cmd_record_newsletters ;;
  status)            cmd_status ;;  # operator-only, not in the scheduled prompt
  log-step)
    shift
    write_status "${1:-unknown}" "${2:-unknown}" "${3:-}"
    echo "LOG_STEP_OK: ${1:-unknown} → ${2:-unknown}"
    ;;
  *)
    echo "Usage: bash weekly_run.sh {health-check|preflight|dispatch-poll|apply-findings|enrich-descriptions|score-travel|dedup|validate-write-ready|write-commit|pipeline-summary|retry-failed|record-newsletters|status|log-step}" >&2
    exit 64
    ;;
esac
