#!/usr/bin/env bash
# Regression tests for the validator/reconcile exit-code gate (WP2 task 2.1, audit P0-1).
# A validator crash (any exit code other than 0/3) must abort the write path BEFORE
# write_notion.py runs. Unexpected reconcile_notion.py exit codes must abort instead
# of falling through silently. Runs fully offline (gh is stubbed).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

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

# Fresh sandbox per case: copied weekly_run.sh + stub pipeline scripts + stub gh.
setup_case() {
  WORK=$(mktemp -d)
  cp "$SCRIPT_DIR/weekly_run.sh" "$WORK/"
  cp -R "$SCRIPT_DIR/erlib" "$WORK/erlib"
  mkdir -p "$WORK/bin" "$WORK/pipeline"

  # Offline stub: validate_db_before_push probes gh; fail fast, no network.
  printf '#!/usr/bin/env bash\nexit 1\n' > "$WORK/bin/gh"
  chmod +x "$WORK/bin/gh"

  # Validator stub: exits per STUB_VALIDATOR_EXITS (comma list, one per call).
  cat > "$WORK/pipeline/validate_llm_output.py" <<'PY'
#!/usr/bin/env python3
import os, sys
n = 0
if os.path.exists("validator_calls"):
    n = int(open("validator_calls").read())
open("validator_calls", "w").write(str(n + 1))
codes = os.environ.get("STUB_VALIDATOR_EXITS", "0").split(",")
sys.exit(int(codes[min(n, len(codes) - 1)]))
PY

  cat > "$WORK/pipeline/travel_time.py" <<'PY'
#!/usr/bin/env python3
open("travel_time_called", "w").write("1")
PY

  # Reconcile stub: --recover exits STUB_RECOVER_EXIT, plain exits STUB_RECONCILE_EXIT.
  cat > "$WORK/pipeline/reconcile_notion.py" <<'PY'
#!/usr/bin/env python3
import os, sys
if "--recover" in sys.argv:
    sys.exit(int(os.environ.get("STUB_RECOVER_EXIT", "0")))
sys.exit(int(os.environ.get("STUB_RECONCILE_EXIT", "0")))
PY

  # write_notion stub: markers prove whether the gate let the write happen.
  cat > "$WORK/pipeline/write_notion.py" <<'PY'
#!/usr/bin/env python3
import sys
if "--prepare" in sys.argv:
    open("prepare_called", "w").write("1")
else:
    open("write_notion_called", "w").write("1")
PY

  sqlite3 "$WORK/event-recommender.db" "CREATE TABLE candidates (
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

run_sub() {
  # run_sub <subcommand>: runs inside $WORK with fake Notion env + stubbed gh.
  (
    cd "$WORK"
    export PATH="$WORK/bin:$PATH"
    # weekly_run.sh calls python3 -m erlib.run_log; resolve erlib from the
    # real repo so the sandbox matches production.
    export PYTHONPATH="$SCRIPT_DIR"
    export NOTION_TOKEN="fake-token"
    export NOTION_DATABASE_ID="fake-db-id"
    bash weekly_run.sh "$1" >stdout.log 2>stderr.log
    echo $? > rc
  )
  RC=$(cat "$WORK/rc")
}

status_detail() {
  python3 -c "import json; print(json.load(open('$WORK/.last_run_status.json')).get('detail',''))" 2>/dev/null || echo ""
}

teardown_case() {
  rm -rf "$WORK"
  unset STUB_VALIDATOR_EXITS STUB_RECOVER_EXIT STUB_RECONCILE_EXIT 2>/dev/null || true
}

echo "=== Test 1: write-commit: validator crash (exit 1) aborts before write ==="
setup_case
export STUB_VALIDATOR_EXITS="1"
run_sub write-commit
check "exit code is non-zero" "$([ "$RC" -ne 0 ] && echo true || echo false)"
check "write_notion.py never ran" "$([ ! -f "$WORK/write_notion_called" ] && echo true || echo false)"
check "status names the validator" "$(status_detail | grep -qi "validat" && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 2: write-commit: validator exit 0 proceeds to write ==="
setup_case
export STUB_VALIDATOR_EXITS="0"
run_sub write-commit
check "write_notion.py ran" "$([ -f "$WORK/write_notion_called" ] && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 3: write-commit: exit 3 triggers travel retry, then proceeds ==="
setup_case
export STUB_VALIDATOR_EXITS="3,0"
run_sub write-commit
check "travel_time.py re-ran" "$([ -f "$WORK/travel_time_called" ] && echo true || echo false)"
check "write_notion.py ran after retry" "$([ -f "$WORK/write_notion_called" ] && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 4: write-commit: persistent exit 3 still proceeds (existing behaviour) ==="
setup_case
export STUB_VALIDATOR_EXITS="3,3"
run_sub write-commit
check "write_notion.py ran (unresolvable venues tolerated)" "$([ -f "$WORK/write_notion_called" ] && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 5: write-commit: validator crash on the RETRY aborts ==="
setup_case
export STUB_VALIDATOR_EXITS="3,1"
run_sub write-commit
check "exit code is non-zero" "$([ "$RC" -ne 0 ] && echo true || echo false)"
check "write_notion.py never ran" "$([ ! -f "$WORK/write_notion_called" ] && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 6: write-commit: unexpected recover exit (9) aborts before write ==="
setup_case
export STUB_VALIDATOR_EXITS="0"
export STUB_RECOVER_EXIT="9"
run_sub write-commit
check "exit code is non-zero" "$([ "$RC" -ne 0 ] && echo true || echo false)"
check "write_notion.py never ran" "$([ ! -f "$WORK/write_notion_called" ] && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 7: write-commit: unexpected post-write reconcile exit (9) aborts ==="
setup_case
export STUB_VALIDATOR_EXITS="0"
export STUB_RECONCILE_EXIT="9"
run_sub write-commit
check "exit code is non-zero" "$([ "$RC" -ne 0 ] && echo true || echo false)"
check "status names the reconcile exit" "$(status_detail | grep -qi "reconcile" && echo true || echo false)"
teardown_case

# Tests 8-9 (prepare-notion gate) removed in WP4 task 4.3: the dead
# prepare-notion/post-write MCP write path was deleted. The exit-code gate
# fix from WP2 task 2.1 remains covered by the write-commit cases above.

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"
[ "$FAIL" -eq 0 ] || exit 1
