#!/usr/bin/env bash
# Tests for cmd_health_check in weekly_run.sh.
# Group 1 (Tests 1-4): env-var presence checks: no network needed.
# Group 2 (Tests 5-11): mocked git + curl for connectivity and scope checks.
# Group 3 (Test 12): error detail reporting.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp "$SCRIPT_DIR/weekly_run.sh" "$WORK/"
cp -R "$SCRIPT_DIR/erlib" "$WORK/erlib"
cd "$WORK"
touch event-recommender.db

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

# ---------------------------------------------------------------------------
# Mocks: installed BEFORE any test runs. Group 1 used to run unmocked and
# sent REAL ntfy pushes (phone spam on every local/CI run, 2026-07-05) and
# real github.com / routes.googleapis.com requests with fake creds.
# ---------------------------------------------------------------------------

mkdir -p "$WORK/bin"

# git mock: intercepts ls-remote (wherever it appears: health-check
# passes -c http.extraHeader=... first), passes everything else through
cat > "$WORK/bin/git" << 'MOCK_GIT'
#!/usr/bin/env bash
REAL_GIT="$(PATH="${ORIGINAL_PATH:-/usr/bin:/usr/local/bin}" command -v git 2>/dev/null || echo /usr/bin/git)"

for arg in "$@"; do
  if [ "$arg" = "ls-remote" ]; then
    EXIT="${MOCK_GIT_LS_REMOTE_EXIT:-0}"
    if [ "$EXIT" = "0" ]; then
      echo "abc123def456	HEAD"
    else
      echo "fatal: Authentication failed" >&2
    fi
    exit "$EXIT"
  fi
done

exec "$REAL_GIT" "$@"
MOCK_GIT
chmod +x "$WORK/bin/git"

# curl mock (Maps + ntfy only): ntfy calls logged to $NTFY_LOG for assertions
cat > "$WORK/bin/curl" << 'MOCK_CURL'
#!/usr/bin/env bash
HTTP_CODE="000"
NEXT_IS=""
IS_NTFY=""

for arg in "$@"; do
  if [ -n "$NEXT_IS" ]; then
    NEXT_IS=""
    continue
  fi
  case "$arg" in
    -D|-o|-w|-H|-X|-d) NEXT_IS="skip" ;;
    https://routes.googleapis.com*) HTTP_CODE="${MOCK_MAPS_HTTP:-200}" ;;
    https://ntfy.sh*) HTTP_CODE="200"; IS_NTFY=1 ;;
  esac
done

if [ -n "$IS_NTFY" ] && [ -n "${NTFY_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$NTFY_LOG"
fi

printf "%s" "$HTTP_CODE"
MOCK_CURL
chmod +x "$WORK/bin/curl"

# python3 mock: intercepts health_probes.py (so probes never hit the network
# from tests; default exit 2 = SKIP); everything else passes through.
# NOTE: ORIGINAL_PATH is poisoned ($WORK/bin already prepended when it is
# captured), so resolve the real python3 by stripping this mock's own dir:
# otherwise exec loops on itself forever.
cat > "$WORK/bin/python3" << 'MOCK_PY'
#!/usr/bin/env bash
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CLEAN_PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$SELF_DIR" | tr '\n' ':')
REAL_PY="$(PATH="$CLEAN_PATH" command -v python3 2>/dev/null || echo /usr/bin/python3)"

for arg in "$@"; do
  case "$arg" in
    *health_probes.py)
      probe="${@: -1}"
      key=$(printf '%s' "$probe" | tr '[:lower:]-' '[:upper:]_')
      exit_var="MOCK_PROBE_${key}_EXIT"
      msg_var="MOCK_PROBE_${key}_MSG"
      echo "${!msg_var:-SKIP: mocked probe}"
      exit "${!exit_var:-2}"
      ;;
  esac
done
exec "$REAL_PY" "$@"
MOCK_PY
chmod +x "$WORK/bin/python3"

# Probe script must exist for the health-check probe loop to run at all;
# the python3 mock above intercepts before this stub would execute.
mkdir -p "$WORK/scripts"
echo "# stub: intercepted by the python3 PATH mock" > "$WORK/scripts/health_probes.py"

# ---------------------------------------------------------------------------
# Group 1: Presence checks (no network, no mock needed)
# ---------------------------------------------------------------------------

echo "=== Test 1: Both tokens missing → exit 1, both failures reported ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" HEALTH_CHECK_RETRY_DELAY=0 NTFY_TOPIC=test-topic GITHUB_TOKEN="" GOOGLE_MAPS_API_KEY="" bash weekly_run.sh health-check 2>&1 || true)
check "reports GITHUB_TOKEN not set" "$(echo "$OUTPUT" | grep -q 'HEALTH_FAIL: GITHUB_TOKEN not set' && echo true || echo false)"
check "reports GOOGLE_MAPS_API_KEY not set" "$(echo "$OUTPUT" | grep -q 'HEALTH_FAIL: GOOGLE_MAPS_API_KEY not set' && echo true || echo false)"
check "final line says 2 failures" "$(echo "$OUTPUT" | grep -q 'HEALTH_CHECK_FAIL: 2' && echo true || echo false)"

echo ""
echo "=== Test 2: Only GITHUB_TOKEN missing → exit 1, only GitHub failure ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" HEALTH_CHECK_RETRY_DELAY=0 NTFY_TOPIC=test-topic GITHUB_TOKEN="" GOOGLE_MAPS_API_KEY="fake_key_for_presence_test" bash weekly_run.sh health-check 2>&1 || true)
check "reports GITHUB_TOKEN not set" "$(echo "$OUTPUT" | grep -q 'HEALTH_FAIL: GITHUB_TOKEN not set' && echo true || echo false)"
check "does NOT report Maps missing" "$(echo "$OUTPUT" | grep -q 'GOOGLE_MAPS_API_KEY not set' && echo false || echo true)"

echo ""
echo "=== Test 3: Only GOOGLE_MAPS_API_KEY missing → exit 1, only Maps failure ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" HEALTH_CHECK_RETRY_DELAY=0 NTFY_TOPIC=test-topic GITHUB_TOKEN="fake_token_for_presence_test" GOOGLE_MAPS_API_KEY="" bash weekly_run.sh health-check 2>&1 || true)
check "reports GOOGLE_MAPS_API_KEY not set" "$(echo "$OUTPUT" | grep -q 'HEALTH_FAIL: GOOGLE_MAPS_API_KEY not set' && echo true || echo false)"
check "does NOT report GitHub missing" "$(echo "$OUTPUT" | grep -q 'GITHUB_TOKEN not set' && echo false || echo true)"

echo ""
echo "=== Test 4: write_status JSON has correct format on failure ==="
rm -f .last_run_status.json .pipeline_run_log.json
PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" HEALTH_CHECK_RETRY_DELAY=0 NTFY_TOPIC=test-topic GITHUB_TOKEN="" GOOGLE_MAPS_API_KEY="" bash weekly_run.sh health-check 2>&1 || true
check ".last_run_status.json exists" "$([ -f .last_run_status.json ] && echo true || echo false)"
PHASE=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['phase'])")
check "phase is 'health-check'" "$([ "$PHASE" = "health-check" ] && echo true || echo false)"
OUTCOME=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['outcome'])")
check "outcome is 'fail'" "$([ "$OUTCOME" = "fail" ] && echo true || echo false)"

# ---------------------------------------------------------------------------
# Group 2: Connectivity checks (mocks already installed above)
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 5: Both credentials valid (mocked) → exit 0 ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1)
EXIT=$?
check "exit code is 0" "$([ "$EXIT" = "0" ] && echo true || echo false)"
check "GitHub OK reported" "$(echo "$OUTPUT" | grep -q 'HEALTH_OK: GitHub PAT valid' && echo true || echo false)"
check "Maps OK reported" "$(echo "$OUTPUT" | grep -q 'HEALTH_OK: Google Maps API key valid' && echo true || echo false)"
check "final line says all passed" "$(echo "$OUTPUT" | grep -q 'HEALTH_CHECK_OK' && echo true || echo false)"

echo ""
echo "=== Test 6: GitHub PAT rejected (auth failure) → exit 1 after retries ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic MOCK_GIT_LS_REMOTE_EXIT=128 HEALTH_CHECK_RETRY_DELAY=0 NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_expired" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1 || true)
check "reports GitHub auth failed" "$(echo "$OUTPUT" | grep -q 'HEALTH_FAIL: GitHub PAT rejected' && echo true || echo false)"
check "retried before failing" "$(echo "$OUTPUT" | grep -q 'HEALTH_RETRY' && echo true || echo false)"

echo ""
echo "=== Test 7: Maps API returns 403 (invalid key) → exit 1 ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic MOCK_MAPS_HTTP=403 GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_bad" bash weekly_run.sh health-check 2>&1 || true)
check "reports Maps HTTP 403" "$(echo "$OUTPUT" | grep -q 'HEALTH_FAIL.*Maps.*HTTP 403' && echo true || echo false)"

echo ""
echo "=== Test 8: Maps API returns 429 (rate-limited) → exit 0 (key is valid) ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic MOCK_MAPS_HTTP=429 GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1)
EXIT=$?
check "exit code is 0" "$([ "$EXIT" = "0" ] && echo true || echo false)"
check "Maps OK with rate-limit note" "$(echo "$OUTPUT" | grep -q 'HEALTH_OK.*Maps.*rate-limited' && echo true || echo false)"

echo ""
echo "=== Test 9: write_status on success has correct format ==="
rm -f .last_run_status.json .pipeline_run_log.json
PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1
OUTCOME=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['outcome'])")
check "outcome is 'ok'" "$([ "$OUTCOME" = "ok" ] && echo true || echo false)"
DETAIL=$(python3 -c "import json; print(json.load(open('.last_run_status.json'))['detail'])")
check "detail mentions credentials valid" "$(echo "$DETAIL" | grep -qi 'valid' && echo true || echo false)"

echo ""
echo "=== Test 10: .pipeline_run_log.json appended on health-check ==="
rm -f .pipeline_run_log.json
PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1
check ".pipeline_run_log.json exists" "$([ -f .pipeline_run_log.json ] && echo true || echo false)"
LOG_STEP=$(python3 -c "import json; print(json.load(open('.pipeline_run_log.json'))[0]['step'])")
check "log step is 'health-check'" "$([ "$LOG_STEP" = "health-check" ] && echo true || echo false)"

echo ""
echo "=== Test 11: GitHub only fails → exit 1 with 1 failure ==="
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic MOCK_GIT_LS_REMOTE_EXIT=128 HEALTH_CHECK_RETRY_DELAY=0 NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_bad" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1 || true)
check "1 failure total" "$(echo "$OUTPUT" | grep -q 'HEALTH_CHECK_FAIL: 1' && echo true || echo false)"

# ---------------------------------------------------------------------------
# Group 3: Credential probes (WP9.2): mocked via the python3 wrapper
# ---------------------------------------------------------------------------

echo ""
echo "=== Test 12: Notion probe BAD → exit 1, distinct ntfy names notion ==="
rm -f .last_run_status.json .pipeline_run_log.json "$WORK/ntfy.log"
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_LOG="$WORK/ntfy.log" \
  MOCK_PROBE_NOTION_EXIT=1 MOCK_PROBE_NOTION_MSG="FAIL: Notion token rejected (HTTP 401): expired or revoked" \
  NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1 || true)
check "probe failure reported" "$(echo "$OUTPUT" | grep -q 'HEALTH_PROBE(notion): FAIL' && echo true || echo false)"
check "1 failure total" "$(echo "$OUTPUT" | grep -q 'HEALTH_CHECK_FAIL: 1' && echo true || echo false)"
check "ntfy names the notion credential" "$(grep -q 'Pipeline credential FAILED: notion' "$WORK/ntfy.log" && echo true || echo false)"

echo ""
echo "=== Test 13: Google probe BAD → exit 1, distinct ntfy names google ==="
rm -f .last_run_status.json .pipeline_run_log.json "$WORK/ntfy.log"
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_LOG="$WORK/ntfy.log" \
  MOCK_PROBE_GOOGLE_EXIT=1 MOCK_PROBE_GOOGLE_MSG="FAIL: Google refresh token rejected (HTTP 400): re-auth needed" \
  NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1 || true)
check "probe failure reported" "$(echo "$OUTPUT" | grep -q 'HEALTH_PROBE(google): FAIL' && echo true || echo false)"
check "ntfy names the google credential" "$(grep -q 'Pipeline credential FAILED: google' "$WORK/ntfy.log" && echo true || echo false)"
check "detail carries re-auth hint" "$(grep -q 're-auth needed' "$WORK/ntfy.log" && echo true || echo false)"

echo ""
echo "=== Test 14: PAT scope probe BAD + all-SKIP probes both behave ==="
rm -f .last_run_status.json .pipeline_run_log.json "$WORK/ntfy.log"
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_LOG="$WORK/ntfy.log" \
  MOCK_PROBE_GITHUB_SCOPES_EXIT=1 MOCK_PROBE_GITHUB_SCOPES_MSG="FAIL: GitHub PAT missing scope(s): workflow" \
  NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1 || true)
check "scope failure reported" "$(echo "$OUTPUT" | grep -q 'HEALTH_PROBE(github-scopes): FAIL' && echo true || echo false)"
check "ntfy names github-scopes" "$(grep -q 'Pipeline credential FAILED: github-scopes' "$WORK/ntfy.log" && echo true || echo false)"
rm -f .last_run_status.json .pipeline_run_log.json
OUTPUT=$(PATH="$WORK/bin:$PATH" ORIGINAL_PATH="$PATH" NTFY_TOPIC=test-topic GITHUB_TOKEN="ghp_test" GOOGLE_MAPS_API_KEY="AIza_test" bash weekly_run.sh health-check 2>&1)
EXIT=$?
check "all probes SKIP → exit 0" "$([ "$EXIT" = "0" ] && echo true || echo false)"
check "skip lines logged" "$(echo "$OUTPUT" | grep -q 'HEALTH_PROBE(notion): SKIP' && echo true || echo false)"

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed ($(( PASS + FAIL )) total)"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
