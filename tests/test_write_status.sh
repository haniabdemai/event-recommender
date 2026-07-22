#!/usr/bin/env bash
# Tests for write_status() and log-step subcommand.
# Verifies: .last_run_status.json overwrite behaviour (existing),
#           .pipeline_run_log.json append behaviour (new).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp "$SCRIPT_DIR/weekly_run.sh" "$WORK/"
# weekly_run.sh resolves NTFY_TOPIC from erlib at startup
cp -R "$SCRIPT_DIR/erlib" "$WORK/erlib"
cd "$WORK"

PASS=0
FAIL=0

check() {
  local name="$1" result="$2"
  if [ "$result" = "true" ]; then
    echo "  PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Test 1: log-step creates .last_run_status.json with correct format ==="
bash weekly_run.sh log-step "preflight" "continue" "Week 2026-05-25 pending"
check ".last_run_status.json exists" "$([ -f .last_run_status.json ] && echo true || echo false)"
PHASE=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['phase'])")
check "phase is 'preflight'" "$([ "$PHASE" = "preflight" ] && echo true || echo false)"
OUTCOME=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['outcome'])")
check "outcome is 'continue'" "$([ "$OUTCOME" = "continue" ] && echo true || echo false)"
HAS_TS=$(python3 -c "import json; print('timestamp_utc' in json.load(open('.last_run_status.json')))")
check "has timestamp_utc" "$([ "$HAS_TS" = "True" ] && echo true || echo false)"

echo ""
echo "=== Test 2: log-step creates .pipeline_run_log.json as array with 1 entry ==="
check ".pipeline_run_log.json exists" "$([ -f .pipeline_run_log.json ] && echo true || echo false)"
LOG_LEN=$(python3 -c "import json; print(len(json.load(open('.pipeline_run_log.json'))))")
check "log has 1 entry" "$([ "$LOG_LEN" = "1" ] && echo true || echo false)"

echo ""
echo "=== Test 3: second call OVERWRITES status but APPENDS to log ==="
bash weekly_run.sh log-step "score-travel" "success" "Scoring complete"
PHASE=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['phase'])")
check "status overwritten to 'score-travel'" "$([ "$PHASE" = "score-travel" ] && echo true || echo false)"
LOG_LEN=$(python3 -c "import json; print(len(json.load(open('.pipeline_run_log.json'))))")
check "log has 2 entries" "$([ "$LOG_LEN" = "2" ] && echo true || echo false)"

echo ""
echo "=== Test 4: log entries have all required fields in correct order ==="
FIELDS_OK=$(python3 -c "
import json
log = json.load(open('.pipeline_run_log.json'))
ok = all(
    all(k in e for k in ('step','outcome','detail','timestamp_utc'))
    for e in log
) and log[0]['step']=='preflight' and log[1]['step']=='score-travel'
print('true' if ok else 'false')
")
check "all entries have correct fields and order" "$FIELDS_OK"

echo ""
echo "=== Test 5: corrupt log file doesn't break write_status ==="
echo "NOT JSON" > .pipeline_run_log.json
bash weekly_run.sh log-step "dedup" "success" "No duplicates"
PHASE=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['phase'])")
check "status still works after corrupt log" "$([ "$PHASE" = "dedup" ] && echo true || echo false)"
LOG_LEN=$(python3 -c "import json; print(len(json.load(open('.pipeline_run_log.json'))))")
check "corrupt log replaced with fresh array (1 entry)" "$([ "$LOG_LEN" = "1" ] && echo true || echo false)"

echo ""
echo "=== Test 6: missing log file doesn't break write_status ==="
rm -f .pipeline_run_log.json
bash weekly_run.sh log-step "write-commit" "fail" "write_notion.py failed"
check ".pipeline_run_log.json created from scratch" "$([ -f .pipeline_run_log.json ] && echo true || echo false)"
LOG_LEN=$(python3 -c "import json; print(len(json.load(open('.pipeline_run_log.json'))))")
check "fresh log has 1 entry" "$([ "$LOG_LEN" = "1" ] && echo true || echo false)"
STEP=$(python3 -c "import json; print(json.load(open('.pipeline_run_log.json'))[0]['step'])")
check "entry step is 'write-commit'" "$([ "$STEP" = "write-commit" ] && echo true || echo false)"
OUTCOME=$(python3 -c "import json; print(json.load(open('.pipeline_run_log.json'))[0]['outcome'])")
check "entry outcome is 'fail'" "$([ "$OUTCOME" = "fail" ] && echo true || echo false)"

echo ""
echo "=== Test 7: empty args handled gracefully ==="
bash weekly_run.sh log-step
check "log-step with no args doesn't crash" "true"
STEP=$(python3 -c "import json; log=json.load(open('.pipeline_run_log.json')); print(log[-1]['step'])")
check "default step name is 'unknown'" "$([ "$STEP" = "unknown" ] && echo true || echo false)"

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"
[ "$FAIL" -eq 0 ] || exit 1
