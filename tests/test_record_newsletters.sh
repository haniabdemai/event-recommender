#!/usr/bin/env bash
# Behaviour tests for the record-newsletters subcommand (WP4 task 4.2).
# Written against the pre-extraction heredoc and kept green across the move
# into newsletter_tracker.py --record-batch: metadata/batch/label-file
# precedence, idempotent re-runs, and run_date propagation.
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

setup_case() {
  WORK=$(mktemp -d)
  cp "$SCRIPT_DIR/weekly_run.sh" "$WORK/"
  mkdir -p "$WORK/pipeline"
  cp "$SCRIPT_DIR/pipeline/newsletter_tracker.py" "$WORK/pipeline/"
  # Sandboxed erlib: newsletter_tracker's default DB resolves relative to
  # erlib's parent, so a copied erlib keeps every write inside $WORK.
  cp -R "$SCRIPT_DIR/erlib" "$WORK/erlib"
}

run_sub() {
  (
    cd "$WORK"
    bash weekly_run.sh record-newsletters >stdout.log 2>stderr.log
    echo $? > rc
  )
  RC=$(cat "$WORK/rc")
}

q() { sqlite3 "$WORK/event-recommender.db" "$1"; }

teardown_case() { rm -rf "$WORK"; unset RUN_DATE 2>/dev/null || true; }

echo "=== Test 1: no input files -> RECORD_SKIP, exit 0 ==="
setup_case
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "prints RECORD_SKIP" "$(grep -q 'RECORD_SKIP: no metadata, batch, or label file found' "$WORK/stdout.log" && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 2: metadata + batch counts; idempotent re-run ==="
setup_case
cat > "$WORK/.newsletter_emails_processed.json" <<'EOF'
[
  {"message_id": "m1", "sender": "The Roundup", "subject": "This week", "email_date": "2026-07-01"},
  {"message_id": "m2", "sender": "City Weekly", "subject": "Weekend", "email_date": "2026-07-02", "events_extracted": 7}
]
EOF
cat > "$WORK/.newsletter_candidates_batch.json" <<'EOF'
[
  {"message_id": "m1", "organiser": "The Roundup", "name": "Event A"},
  {"message_id": "m1", "organiser": "The Roundup", "name": "Event B"},
  {"message_id": "m3", "organiser": "Secret London", "name": "Event C"}
]
EOF
export RUN_DATE="2026-07-04"
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "used metadata file" "$(grep -q 'RECORD_SOURCE: .newsletter_emails_processed.json (2 emails)' "$WORK/stdout.log" && echo true || echo false)"
check "2 recorded" "$(grep -q 'RECORD_NEWSLETTERS: 2 recorded, 0 already existed' "$WORK/stdout.log" && echo true || echo false)"
check "m1 events from batch count" "$([ "$(q "SELECT events_extracted FROM processed_emails WHERE gmail_message_id='m1'")" = "2" ] && echo true || echo false)"
check "m2 events kept from metadata" "$([ "$(q "SELECT events_extracted FROM processed_emails WHERE gmail_message_id='m2'")" = "7" ] && echo true || echo false)"
check "run_date propagated" "$([ "$(q "SELECT run_date FROM processed_emails WHERE gmail_message_id='m1'")" = "2026-07-04" ] && echo true || echo false)"
run_sub
check "re-run records nothing" "$(grep -q 'RECORD_NEWSLETTERS: 0 recorded, 2 already existed' "$WORK/stdout.log" && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 3: batch-only fallback ==="
setup_case
cat > "$WORK/.newsletter_candidates_batch.json" <<'EOF'
[
  {"message_id": "m9", "organiser": "Secret London", "name": "Event X"},
  {"message_id": "m9", "organiser": "Secret London", "name": "Event Y"},
  {"message_id": "m9", "organiser": "Secret London", "name": "Event Z"}
]
EOF
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "fallback source logged" "$(grep -q 'RECORD_SOURCE: .newsletter_candidates_batch.json (fallback, 1 emails, no email_date)' "$WORK/stdout.log" && echo true || echo false)"
check "m9 recorded with 3 events" "$([ "$(q "SELECT events_extracted FROM processed_emails WHERE gmail_message_id='m9'")" = "3" ] && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 4: label file fills unknown ids ==="
setup_case
cat > "$WORK/.newsletter_candidates_batch.json" <<'EOF'
[
  {"message_id": "m1", "organiser": "The Roundup", "name": "Event A"}
]
EOF
cat > "$WORK/.emails_to_label.json" <<'EOF'
["m1", "m20"]
EOF
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "label fill logged" "$(grep -q 'RECORD_LABEL_FILL: 1 additional emails from .emails_to_label.json' "$WORK/stdout.log" && echo true || echo false)"
check "m20 recorded as Unknown/0" "$([ "$(q "SELECT sender || '|' || events_extracted FROM processed_emails WHERE gmail_message_id='m20'")" = "Unknown|0" ] && echo true || echo false)"
teardown_case

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"
[ "$FAIL" -eq 0 ] || exit 1
