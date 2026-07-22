#!/usr/bin/env bash
# tests/test_dispatch_poll.sh: smoke tests for dispatch-poll run ID extraction
# Tests call scripts/find_dispatched_run.py directly (same code cmd_dispatch_poll uses)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FIND_RUN="${SCRIPT_DIR}/scripts/find_dispatched_run.py"

PASS=0; FAIL=0; TOTAL=0

assert_eq() {
  TOTAL=$((TOTAL + 1))
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: ${desc}"
    echo "  expected: ${expected}"
    echo "  actual:   ${actual}"
  fi
}

# --- Run ID extraction tests (calling the real script) ---

# Test 1: Finds run by created_at >= dispatch_time (Pass 1)
RID=$(echo '{"workflow_runs":[
  {"id": 111, "created_at": "2026-06-16T18:05:00Z", "status": "queued", "conclusion": null},
  {"id": 222, "created_at": "2026-06-16T17:00:00Z", "status": "completed", "conclusion": "success"}
]}' | python3 "$FIND_RUN" "2026-06-16T18:00:00Z")
assert_eq "finds run by created_at" "111" "$RID"

# Test 2: Falls back to status != completed when no run matches by created_at (Pass 2)
RID=$(echo '{"workflow_runs":[
  {"id": 333, "created_at": "2026-06-16T18:59:50Z", "status": "in_progress", "conclusion": null},
  {"id": 444, "created_at": "2026-06-16T17:00:00Z", "status": "completed", "conclusion": "success"}
]}' | python3 "$FIND_RUN" "2026-06-16T19:00:00Z")
assert_eq "falls back to status match" "333" "$RID"

# Test 3: Prefers created_at match over stale in-progress run (two-pass priority)
RID=$(echo '{"workflow_runs":[
  {"id": 555, "created_at": "2026-06-16T19:00:05Z", "status": "queued", "conclusion": null},
  {"id": 666, "created_at": "2026-06-10T12:00:00Z", "status": "in_progress", "conclusion": null}
]}' | python3 "$FIND_RUN" "2026-06-16T19:00:00Z")
assert_eq "prefers created_at over stale in_progress" "555" "$RID"

# Test 4: All completed and old: returns nothing (triggers fallback)
RID=$(echo '{"workflow_runs":[
  {"id": 777, "created_at": "2026-06-16T17:00:00Z", "status": "completed", "conclusion": "success"}
]}' | python3 "$FIND_RUN" "2026-06-16T19:00:00Z")
assert_eq "no match returns empty" "" "${RID:-}"

# Test 5: Empty workflow_runs: returns nothing
RID=$(echo '{"workflow_runs":[]}' | python3 "$FIND_RUN" "2026-06-16T19:00:00Z")
assert_eq "empty runs returns empty" "" "${RID:-}"

# Test 6: Multiple queued runs: takes first (most recent, API returns newest first)
RID=$(echo '{"workflow_runs":[
  {"id": 888, "created_at": "2026-06-16T18:05:00Z", "status": "queued", "conclusion": null},
  {"id": 999, "created_at": "2026-06-16T18:04:00Z", "status": "queued", "conclusion": null}
]}' | python3 "$FIND_RUN" "2026-06-16T18:00:00Z")
assert_eq "multiple queued takes first" "888" "$RID"

# Test 7: Stale in-progress run is NOT matched when fresh run exists by created_at
RID=$(echo '{"workflow_runs":[
  {"id": 1010, "created_at": "2026-06-10T12:00:00Z", "status": "in_progress", "conclusion": null},
  {"id": 1011, "created_at": "2026-06-16T19:00:02Z", "status": "queued", "conclusion": null}
]}' | python3 "$FIND_RUN" "2026-06-16T19:00:00Z")
assert_eq "fresh run by created_at beats stale in_progress" "1011" "$RID"

# Test 8: Skips completed runs, finds newer queued (API newest-first ordering)
RID=$(echo '{"workflow_runs":[
  {"id": 555, "created_at": "2026-06-16T17:00:00Z", "status": "completed", "conclusion": "success"},
  {"id": 666, "created_at": "2026-06-16T19:00:05Z", "status": "queued", "conclusion": null}
]}' | python3 "$FIND_RUN" "2026-06-16T19:00:00Z")
assert_eq "skips stale completed, finds newer" "666" "$RID"

# --- Poll status parsing tests (now uses --jq, test the equivalent logic) ---

parse_status_jq() {
  echo "$1" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(r['status'], r.get('conclusion') or '')
"
}

STATUS=$(parse_status_jq '{"status":"completed","conclusion":"success"}')
assert_eq "completed success" "completed success" "$STATUS"

STATUS=$(parse_status_jq '{"status":"completed","conclusion":"failure"}')
assert_eq "completed failure" "completed failure" "$STATUS"

STATUS=$(parse_status_jq '{"status":"in_progress"}')
assert_eq "in_progress no conclusion" "in_progress " "$STATUS"

STATUS=$(parse_status_jq '{"status":"queued"}')
assert_eq "queued no conclusion" "queued " "$STATUS"

# Test: conclusion: null should produce empty string, not "None"
STATUS=$(parse_status_jq '{"status":"completed","conclusion":null}')
assert_eq "null conclusion is empty" "completed " "$STATUS"

# --- Dispatch time buffer test ---

BUFFERED=$(python3 -c "
from datetime import datetime, timedelta
now = datetime(2026, 6, 16, 19, 0, 30)
buffered = now - timedelta(seconds=10)
print(buffered.strftime('%Y-%m-%dT%H:%M:%SZ'))
")
assert_eq "10s buffer" "2026-06-16T19:00:20Z" "$BUFFERED"

# --- cmd_dispatch_poll exit-code propagation tests (regression: 2026-07-05 run) ---
# Bug: `if _dispatch_poll_gh ...; then return 0; fi` followed by
# `local gh_exit=$?` read the *if-construct's own* exit status (0, because
# bash defines a taken-no-branch if/fi with no else as status 0) instead of
# _dispatch_poll_gh's real exit code. Every gh-path failure or timeout was
# printed as "DISPATCH_WARN: gh api path failed (exit 0)" and cmd_dispatch_poll
# still returned 0. These tests run the real `bash weekly_run.sh dispatch-poll`
# end-to-end (mocked `gh` + no-op `sleep`) and assert the documented exit
# codes (0=done, 1=failed, 4=timeout) actually propagate out.

DP_WORK=$(mktemp -d)
cleanup_dp_work() { rm -rf "$DP_WORK"; }
trap cleanup_dp_work EXIT

mkdir -p "$DP_WORK/scripts" "$DP_WORK/bin"
cp "$SCRIPT_DIR/weekly_run.sh" "$DP_WORK/"
cp "$SCRIPT_DIR/scripts/find_dispatched_run.py" "$DP_WORK/scripts/"
cp -R "$SCRIPT_DIR/erlib" "$DP_WORK/erlib"

# no-op sleep: _dispatch_poll_gh does real `sleep 5` (find-run loop) and
# `sleep 10` (poll loop); without this stub these tests would burn tens of
# real wall-clock seconds per case.
cat > "$DP_WORK/bin/sleep" << 'MOCK_SLEEP'
#!/usr/bin/env bash
exit 0
MOCK_SLEEP
chmod +x "$DP_WORK/bin/sleep"

# gh mock: simulates the three `gh api` calls _dispatch_poll_gh makes:
#   1. POST .../dispatches                  -> always succeeds
#   2. GET  .../workflows/<wf>/runs?...      -> reports one freshly-created run
#   3. GET  .../actions/runs/<id> --jq ...   -> reports $MOCK_GH_POLL_STATUS
# (3) is what drives each scenario below: a fixed "completed failure" never
# lets the poll loop reach a success case (exit 1), and a fixed non-terminal
# "in_progress" never lets it complete before the timeout budget runs out
# (exit 4).
cat > "$DP_WORK/bin/gh" << 'MOCK_GH'
#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    */dispatches)
      exit 0
      ;;
    *workflows/*/runs\?*)
      NOW=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
      echo "{\"workflow_runs\":[{\"id\": 424242, \"created_at\": \"${NOW}\", \"status\": \"queued\", \"conclusion\": null}]}"
      exit 0
      ;;
    */actions/runs/*)
      echo "${MOCK_GH_POLL_STATUS:-completed success}"
      exit 0
      ;;
  esac
done
exit 1
MOCK_GH
chmod +x "$DP_WORK/bin/gh"

echo ""
echo "=== Test: gh-path success -> cmd_dispatch_poll exits 0 (happy-path regression guard) ==="
set +e
OUTPUT=$(PATH="$DP_WORK/bin:$PATH" MOCK_GH_POLL_STATUS="completed success" bash "$DP_WORK/weekly_run.sh" dispatch-poll fake-workflow.yml 30 2>&1)
EXIT=$?
set -e
TOTAL=$((TOTAL + 1))
if [ "$EXIT" -eq 0 ]; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: gh-path success should exit 0 (got ${EXIT})"
  echo "$OUTPUT"
fi
TOTAL=$((TOTAL + 1))
if echo "$OUTPUT" | grep -q "DISPATCH_DONE:"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: expected DISPATCH_DONE in output"
  echo "$OUTPUT"
fi

echo ""
echo "=== Test: workflow conclusion=failure -> cmd_dispatch_poll exits 1 ==="
set +e
OUTPUT=$(PATH="$DP_WORK/bin:$PATH" MOCK_GH_POLL_STATUS="completed failure" bash "$DP_WORK/weekly_run.sh" dispatch-poll fake-workflow.yml 30 2>&1)
EXIT=$?
set -e
TOTAL=$((TOTAL + 1))
if [ "$EXIT" -eq 1 ]; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: conclusion=failure should exit 1 (got ${EXIT})"
  echo "$OUTPUT"
fi
TOTAL=$((TOTAL + 1))
if echo "$OUTPUT" | grep -q "DISPATCH_FAILED:"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: expected DISPATCH_FAILED in output"
  echo "$OUTPUT"
fi
TOTAL=$((TOTAL + 1))
if echo "$OUTPUT" | grep -q "DISPATCH_WARN: gh api path failed (exit 0)"; then
  FAIL=$((FAIL + 1))
  echo "FAIL: exit code was swallowed to 0 again (regression!)"
  echo "$OUTPUT"
else
  PASS=$((PASS + 1))
fi

echo ""
echo "=== Test: poll timeout -> cmd_dispatch_poll exits 4 ==="
set +e
OUTPUT=$(PATH="$DP_WORK/bin:$PATH" MOCK_GH_POLL_STATUS="in_progress " bash "$DP_WORK/weekly_run.sh" dispatch-poll fake-workflow.yml 20 2>&1)
EXIT=$?
set -e
TOTAL=$((TOTAL + 1))
if [ "$EXIT" -eq 4 ]; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: timeout should exit 4 (got ${EXIT})"
  echo "$OUTPUT"
fi
TOTAL=$((TOTAL + 1))
if echo "$OUTPUT" | grep -q "DISPATCH_TIMEOUT:"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: expected DISPATCH_TIMEOUT in output"
  echo "$OUTPUT"
fi

cleanup_dp_work
trap - EXIT

# --- Summary ---
echo ""
echo "${PASS}/${TOTAL} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
