#!/usr/bin/env bash
# Tests for pipeline-summary status classification.
# Verifies: severity-based status logic replaces string-matching.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp "$SCRIPT_DIR/weekly_run.sh" "$WORK/"
mkdir -p "$WORK/pipeline"
cp "$SCRIPT_DIR/pipeline/pipeline_summary.py" "$WORK/pipeline/"
cp -R "$SCRIPT_DIR/erlib" "$WORK/erlib"
cd "$WORK"

# ntfy stub: tests must stay offline (no push notifications from test runs)
mkdir -p .shims
printf '#!/bin/bash\nexit 0\n' > .shims/curl
chmod +x .shims/curl
export PATH="$WORK/.shims:$PATH"

TODAY=$(date -u +%Y-%m-%d)
FUTURE_DATE=$(date -u -v+14d +%Y-%m-%d 2>/dev/null || date -u -d "+14 days" +%Y-%m-%d)

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

setup_db() {
  rm -f event-recommender.db
  sqlite3 event-recommender.db "CREATE TABLE candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    name TEXT NOT NULL,
    date TEXT NOT NULL,
    end_date TEXT,
    pipeline_state TEXT DEFAULT 'pending_llm',
    llm_tier TEXT,
    llm_reviewed TEXT,
    llm_reasoning TEXT,
    score INTEGER DEFAULT 0,
    tier TEXT DEFAULT 'Borderline',
    description TEXT DEFAULT 'test',
    source TEXT DEFAULT 'test',
    url TEXT DEFAULT 'http://test',
    venue_name TEXT DEFAULT 'Test Venue',
    venue_postcode TEXT DEFAULT 'W1A 1AA',
    format_type TEXT DEFAULT 'Social meetup',
    veto_reason TEXT,
    notion_page_id TEXT,
    notion_status TEXT,
    travel_display TEXT,
    travel_lookup_failed INTEGER DEFAULT 0,
    place_written INTEGER DEFAULT 0,
    needs_enrichment INTEGER DEFAULT 0,
    user_attended INTEGER DEFAULT 0,
    source_snapshot TEXT,
    description_source TEXT,
    created_at TEXT DEFAULT (datetime('now'))
  );"
}

setup_mock_files() {
  local outcome="${1:-success}"
  # Freshness is data-driven (erlib.freshness): marker CONTENT backdated 2s,
  # mock state files stamped with timestamp_utc = now (strictly newer)
  python3 -c "from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat())" > .pipeline_start_time
  NOW_UTC=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())")
  echo "{\"outcome\": \"$outcome\", \"timestamp_utc\": \"$NOW_UTC\"}" > .last_run_status.json
  echo "{\"meetup\": {\"new\": 10}, \"luma\": {\"new\": 5}, \"errors\": [], \"timestamp_utc\": \"$NOW_UTC\"}" > .last_fetch_counts.json
  rm -f .last_validator_summary.json .last_llm_summary.json .last_enrichment_summary.json
}

get_status() {
  python3 -c "import json; print(json.load(open('.last_pipeline_summary.json'))['status'])"
}

get_issues() {
  python3 -c "import json; print(json.load(open('.last_pipeline_summary.json'))['issues'])"
}

insert_candidates() {
  local count="$1" state="$2"
  for i in $(seq 1 "$count"); do
    sqlite3 event-recommender.db "INSERT INTO candidates (run_date, name, date, pipeline_state)
      VALUES ('$TODAY', 'Event $state $i', '$FUTURE_DATE', '$state');"
  done
}

# --- Tests ---

echo "=== Test 1: Clean run: all written, no stuck ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Success" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "=== Test 2: 1 pending_llm (below threshold): should be Success ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 1 "pending_llm"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Success" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "=== Test 3: 3 pending_llm (at threshold): should be Success ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 3 "pending_llm"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Success" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "=== Test 4: 4 pending_llm (above threshold): should be Partial ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 4 "pending_llm"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Partial" "$([ "$(get_status)" = "Partial" ] && echo true || echo false)"

echo ""
echo "=== Test 5: 33 pending_llm (LLM step crash): should be Partial ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 33 "pending_llm"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Partial" "$([ "$(get_status)" = "Partial" ] && echo true || echo false)"

echo ""
echo "=== Test 6: write_failed > 0: should be Partial (BUG FIX) ==="
setup_db
setup_mock_files "success"
insert_candidates 18 "written"
insert_candidates 2 "write_failed"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Partial" "$([ "$(get_status)" = "Partial" ] && echo true || echo false)"

echo ""
echo "=== Test 7: ready_to_write > 0: should be Partial ==="
setup_db
setup_mock_files "success"
insert_candidates 18 "written"
insert_candidates 2 "ready_to_write"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Partial" "$([ "$(get_status)" = "Partial" ] && echo true || echo false)"

echo ""
echo "=== Test 8: pending_travel below threshold: should be Success ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 2 "pending_travel"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Success" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "=== Test 9: 3 pending_travel (at threshold): should be Success ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 3 "pending_travel"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Success" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "=== Test 10: 4 pending_travel (above threshold): should be Partial ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 4 "pending_travel"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Partial" "$([ "$(get_status)" = "Partial" ] && echo true || echo false)"

echo ""
echo "=== Test 11: 5 pending_travel (well above threshold): should be Partial ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 5 "pending_travel"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Partial" "$([ "$(get_status)" = "Partial" ] && echo true || echo false)"

echo ""
echo "=== Test 12: outcome=fail overrides everything: should be Failed ==="
setup_db
setup_mock_files "fail"
insert_candidates 20 "written"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Failed" "$([ "$(get_status)" = "Failed" ] && echo true || echo false)"

echo ""
echo "=== Test 13: outcome=unknown, no blocking: should be Incomplete ==="
setup_db
setup_mock_files "unknown"
insert_candidates 20 "written"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Incomplete" "$([ "$(get_status)" = "Incomplete" ] && echo true || echo false)"

echo ""
echo "=== Test 14: outcome=unknown WITH blocking: should be Partial ==="
setup_db
setup_mock_files "unknown"
insert_candidates 20 "written"
insert_candidates 5 "write_failed"
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Partial" "$([ "$(get_status)" = "Partial" ] && echo true || echo false)"

echo ""
echo "=== Test 15: validator corrections alone: should be Success ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
echo '{"corrections": 7, "hard_veto_count": 3, "cap_demotion_count": 4, "details": [{"id":1,"name":"test","was":"Borderline","rule":"wrong sport"}]}' > .last_validator_summary.json
touch -r .pipeline_start_time .last_validator_summary.json 2>/dev/null || true
sleep 1
touch .last_validator_summary.json
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Success" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "=== Test 16: Issues field shows 'remaining' for below-threshold stuck ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
insert_candidates 2 "pending_llm"
bash weekly_run.sh pipeline-summary 2>/dev/null
ISSUES=$(get_issues)
check "Issues mentions pending_llm" "$(echo "$ISSUES" | grep -q "pending_llm" && echo true || echo false)"
check "Issues says 'remaining' not 'stuck'" "$(echo "$ISSUES" | grep -q "remaining" && echo true || echo false)"
check "Status is still Success" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "=== Test 17: past-date stuck candidates don't count ==="
setup_db
setup_mock_files "success"
insert_candidates 20 "written"
for i in $(seq 1 10); do
  sqlite3 event-recommender.db "INSERT INTO candidates (run_date, name, date, pipeline_state)
    VALUES ('$TODAY', 'Past event $i', '2026-01-01', 'pending_llm');"
done
bash weekly_run.sh pipeline-summary 2>/dev/null
check "Status is Success (past dates ignored)" "$([ "$(get_status)" = "Success" ] && echo true || echo false)"

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"
[ "$FAIL" -eq 0 ] || exit 1
