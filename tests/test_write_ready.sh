#!/usr/bin/env bash
# Behaviour tests for the validate-write-ready subcommand (WP4 task 4.2).
# Written against the pre-extraction heredoc and kept green across the
# extraction to write_ready_check.py: required-field demotion, UTF-16
# description truncation, untouched good rows, and the commit step.
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
  [ -f "$SCRIPT_DIR/pipeline/write_ready_check.py" ] && { mkdir -p "$WORK/pipeline"; cp "$SCRIPT_DIR/pipeline/write_ready_check.py" "$WORK/pipeline/"; }
  # Sandboxed erlib so nothing can resolve paths back to the real repo
  cp -R "$SCRIPT_DIR/erlib" "$WORK/erlib"
  # write_ready_check imports the shared url_check (invented-URL gate)
  mkdir -p "$WORK/scripts"
  cp "$SCRIPT_DIR/scripts/verify_newsletter_extraction.py" "$WORK/scripts/"

  # Local bare origin so commit_and_push works fully offline
  git init -q -b main "$WORK"
  git -C "$WORK" config user.email "test@test"
  git -C "$WORK" config user.name "Test"
  git init -q --bare "$WORK/origin.git"
  git -C "$WORK" remote add origin "$WORK/origin.git"

  sqlite3 "$WORK/event-recommender.db" "CREATE TABLE candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL DEFAULT '2026-07-04',
    name TEXT,
    date TEXT,
    end_date TEXT,
    pipeline_state TEXT DEFAULT 'ready_to_write',
    llm_tier TEXT,
    description TEXT
  );"
  git -C "$WORK" add -A
  git -C "$WORK" commit -qm "init"
}

run_sub() {
  (
    cd "$WORK"
    bash weekly_run.sh validate-write-ready >stdout.log 2>stderr.log
    echo $? > rc
  )
  RC=$(cat "$WORK/rc")
}

q() { sqlite3 "$WORK/event-recommender.db" "$1"; }

teardown_case() { rm -rf "$WORK"; }

echo "=== Test 1: no ready_to_write candidates ==="
setup_case
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "reports 0 candidates" "$(grep -q 'VALIDATE_WRITE_READY: 0 candidates to validate' "$WORK/stdout.log" && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 2: missing required fields demote to incomplete ==="
setup_case
q "INSERT INTO candidates (name, date, llm_tier, description) VALUES ('', '2026-08-01', 'Recommended', 'd');"
q "INSERT INTO candidates (name, date, llm_tier, description) VALUES ('No Date', '', 'Recommended', 'd');"
q "INSERT INTO candidates (name, date, llm_tier, description) VALUES ('No Tier', '2026-08-01', NULL, 'd');"
q "INSERT INTO candidates (name, date, llm_tier, description) VALUES ('Good Event', '2026-08-01', 'Recommended', 'fine');"
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "3 demoted to incomplete" "$([ "$(q "SELECT COUNT(*) FROM candidates WHERE pipeline_state='incomplete'")" = "3" ] && echo true || echo false)"
check "good row still ready_to_write" "$([ "$(q "SELECT pipeline_state FROM candidates WHERE name='Good Event'")" = "ready_to_write" ] && echo true || echo false)"
check "summary counts demotions" "$(grep -q 'VALIDATE_WRITE_READY: 4 checked, 1 ok, 0 truncated, 3 demoted' "$WORK/stdout.log" && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 3: UTF-16 truncation of long descriptions ==="
setup_case
python3 - "$WORK/event-recommender.db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("INSERT INTO candidates (name, date, llm_tier, description) VALUES (?,?,?,?)",
             ("Long ASCII", "2026-08-01", "Recommended", "x" * 2500))
conn.execute("INSERT INTO candidates (name, date, llm_tier, description) VALUES (?,?,?,?)",
             ("Emoji Heavy", "2026-08-01", "Recommended", "\U0001F389" * 1500))
conn.execute("INSERT INTO candidates (name, date, llm_tier, description) VALUES (?,?,?,?)",
             ("Exactly 2000", "2026-08-01", "Recommended", "y" * 2000))
conn.commit()
PY
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
python3 - "$WORK/event-recommender.db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
def utf16_len(s):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)
def desc(name):
    return conn.execute("SELECT description FROM candidates WHERE name=?", (name,)).fetchone()[0]
ascii_d, emoji_d, exact_d = desc("Long ASCII"), desc("Emoji Heavy"), desc("Exactly 2000")
ok = True
if not (ascii_d == "x" * 1999 + "…"):
    print(f"  FAIL-DETAIL: ascii truncated to {len(ascii_d)} chars, ends {ascii_d[-3:]!r}"); ok = False
if not (emoji_d == "\U0001F389" * 999 + "…"):
    print(f"  FAIL-DETAIL: emoji truncated to {utf16_len(emoji_d)} utf16 units"); ok = False
if not (exact_d == "y" * 2000):
    print(f"  FAIL-DETAIL: exactly-2000 was modified (len {len(exact_d)})"); ok = False
sys.exit(0 if ok else 1)
PY
check "truncation matches pinned semantics" "$([ $? -eq 0 ] && echo true || echo false)"
check "summary counts 2 truncated" "$(grep -q 'VALIDATE_WRITE_READY: 3 checked, 1 ok, 2 truncated, 0 demoted' "$WORK/stdout.log" && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 3B: invented-URL gate (pre-write Pass-1 check) ==="
setup_case
q "ALTER TABLE candidates ADD COLUMN source TEXT;
   ALTER TABLE candidates ADD COLUMN url TEXT;
   ALTER TABLE candidates ADD COLUMN source_snapshot TEXT;
   CREATE TABLE processed_emails (gmail_message_id TEXT PRIMARY KEY, body_text TEXT);"
q "INSERT INTO processed_emails VALUES
   ('m1', 'Great gig tonight, book at https://www.meetup.com/foo/events/123/ now');"
q "INSERT INTO processed_emails VALUES ('m2', 'plain text body, links stripped');"
# Fabricated: domain absent from the body's links -> demote
q "INSERT INTO candidates (name, date, llm_tier, description, source, url, source_snapshot)
   VALUES ('Fabricated Link', '2026-08-01', 'Recommended', 'd', 'Newsletter',
           'https://mailout.city-gallery.example/ls/click?upn=u001example', 'm1');"
# Canonicalised: domain matches a body link -> pass
q "INSERT INTO candidates (name, date, llm_tier, description, source, url, source_snapshot)
   VALUES ('Canonicalised Link', '2026-08-01', 'Recommended', 'd', 'Newsletter',
           'https://www.meetup.com/foo/events/123/', 'm1');"
# Body with no links at all -> check skipped, passes
q "INSERT INTO candidates (name, date, llm_tier, description, source, url, source_snapshot)
   VALUES ('No Links Body', '2026-08-01', 'Recommended', 'd', 'Newsletter',
           'https://example.com/event', 'm2');"
# Canonicalised lu.ma URL vs luma.com body link (scoring rewrites the domain) -> pass
q "INSERT INTO processed_emails VALUES
   ('m3', 'Poetry night, book at https://luma.com/xyz-poet?lm_api_id=123 tonight');"
q "INSERT INTO candidates (name, date, llm_tier, description, source, url, source_snapshot)
   VALUES ('Canonicalised Luma', '2026-08-01', 'Recommended', 'd', 'Newsletter',
           'https://lu.ma/xyz-poet', 'm3');"
# Non-newsletter candidate is never gated
q "INSERT INTO candidates (name, date, llm_tier, description, source, url, source_snapshot)
   VALUES ('Meetup Event', '2026-08-01', 'Recommended', 'd', 'Meetup',
           'https://www.meetup.com/bar/events/456/', NULL);"
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "fabricated URL demoted" "$([ "$(q "SELECT pipeline_state FROM candidates WHERE name='Fabricated Link'")" = "incomplete" ] && echo true || echo false)"
check "gate names the reason" "$(grep -q 'invented-link gate' "$WORK/stdout.log" && echo true || echo false)"
check "canonicalised URL passes" "$([ "$(q "SELECT pipeline_state FROM candidates WHERE name='Canonicalised Link'")" = "ready_to_write" ] && echo true || echo false)"
check "no-links body passes (skip semantics)" "$([ "$(q "SELECT pipeline_state FROM candidates WHERE name='No Links Body'")" = "ready_to_write" ] && echo true || echo false)"
check "canonicalised lu.ma vs luma.com body passes" "$([ "$(q "SELECT pipeline_state FROM candidates WHERE name='Canonicalised Luma'")" = "ready_to_write" ] && echo true || echo false)"
check "non-newsletter untouched" "$([ "$(q "SELECT pipeline_state FROM candidates WHERE name='Meetup Event'")" = "ready_to_write" ] && echo true || echo false)"
teardown_case

echo ""
echo "=== Test 4: DB commit happens after changes ==="
setup_case
q "INSERT INTO candidates (name, date, llm_tier, description) VALUES ('', '2026-08-01', 'Recommended', 'd');"
run_sub
check "exit 0" "$([ "$RC" -eq 0 ] && echo true || echo false)"
# grep without -q: -q exits on first match and SIGPIPEs git log, which
# pipefail then reports as failure even when the commit is present.
check "Step 5B commit exists" "$(git -C "$WORK" log --oneline | grep 'Step 5B' >/dev/null && echo true || echo false)"
teardown_case

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"
[ "$FAIL" -eq 0 ] || exit 1
