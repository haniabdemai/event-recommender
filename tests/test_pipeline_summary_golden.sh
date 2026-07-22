#!/usr/bin/env bash
# Golden-master test for the pipeline-summary subcommand.
#
# Builds a fully synthetic environment (DB + state files) that exercises the
# rich output branches (validator corrections, enrichment, newsletter sender
# rollup, LLM disagreements, stuck states, run-log embedding), runs
# `bash weekly_run.sh pipeline-summary`, scrubs the two time-derived fields
# (batch_date, batch_label), and diffs the result against
# tests/fixtures/pipeline_summary_golden.json.
#
# The fixture was captured from the pre-extraction heredoc (WP4 task 4.1);
# byte-identical output proves the extraction to pipeline_summary.py changed
# nothing. Regenerate deliberately with: bash tests/test_pipeline_summary_golden.sh --capture
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE="$SCRIPT_DIR/tests/fixtures/pipeline_summary_golden.json"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp "$SCRIPT_DIR/weekly_run.sh" "$WORK/"
# Post-extraction the subcommand delegates to pipeline_summary.py
[ -f "$SCRIPT_DIR/pipeline/pipeline_summary.py" ] && { mkdir -p "$WORK/pipeline"; cp "$SCRIPT_DIR/pipeline/pipeline_summary.py" "$WORK/pipeline/"; }
cp -R "$SCRIPT_DIR/erlib" "$WORK/erlib"
cd "$WORK"

# ntfy stub: tests must stay offline (no push notifications from test runs)
mkdir -p .shims
printf '#!/bin/bash\nexit 0\n' > .shims/curl
chmod +x .shims/curl
export PATH="$WORK/.shims:$PATH"

TODAY=$(date -u +%Y-%m-%d)
FUTURE_DATE=$(date -u -v+14d +%Y-%m-%d 2>/dev/null || date -u -d "+14 days" +%Y-%m-%d)

sqlite3 event-recommender.db "CREATE TABLE candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT NOT NULL,
  name TEXT NOT NULL,
  date TEXT NOT NULL,
  end_date TEXT,
  pipeline_state TEXT DEFAULT 'pending_llm'
);
CREATE TABLE processed_emails (
  gmail_message_id TEXT PRIMARY KEY,
  sender TEXT NOT NULL,
  subject TEXT,
  email_date TEXT,
  events_extracted INTEGER DEFAULT 0,
  run_date TEXT,
  processed_at TEXT DEFAULT (datetime('now'))
);"

insert_candidates() {
  local count="$1" state="$2"
  for i in $(seq 1 "$count"); do
    sqlite3 event-recommender.db "INSERT INTO candidates (run_date, name, date, pipeline_state)
      VALUES ('$TODAY', 'Event $state $i', '$FUTURE_DATE', '$state');"
  done
}

# Funnel: 28 fetched, mixed dispositions, one blocking state (ready_to_write) -> Partial
insert_candidates 12 "written"
insert_candidates 5  "vetoed"
insert_candidates 4  "llm_rejected"
insert_candidates 2  "duplicate"
insert_candidates 2  "pending_llm"
insert_candidates 1  "pending_travel"
insert_candidates 1  "ready_to_write"
insert_candidates 1  "write_failed"
# Past-date stuck candidate: must be ignored by the funnel
sqlite3 event-recommender.db "INSERT INTO candidates (run_date, name, date, pipeline_state)
  VALUES ('$TODAY', 'Past event', '2026-01-01', 'pending_llm');"

# Newsletter rollup rows (fixed email dates so formatted output never drifts)
sqlite3 event-recommender.db "
INSERT INTO processed_emails VALUES ('m1', 'The Roundup',   's', '2026-07-01', 8, '$TODAY', 'x');
INSERT INTO processed_emails VALUES ('m2', 'The Roundup',   's', '2026-07-02', 4, '$TODAY', 'x');
INSERT INTO processed_emails VALUES ('m3', 'City Weekly',    's', '2026-07-01', 5, '$TODAY', 'x');
INSERT INTO processed_emails VALUES ('m4', 'Meetup',      's', '2026-07-01', 9, '$TODAY', 'x');
"

# Pipeline start time: content backdated 2s (duration still rounds to
# '0 min') so the stamped mock state files below count as fresh under the
# data-driven check (erlib.freshness: mtimes are never consulted).
python3 -c "from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat())" > .pipeline_start_time

echo '{"outcome": "success"}' > .last_run_status.json
echo '{"meetup": {"new": 10, "errors": []}, "luma": {"new": 5}, "errors": ["Meetup: 1 group failed"]}' > .last_fetch_counts.json
echo '{"corrections": 3, "hard_veto_count": 2, "cap_demotion_count": 1, "details": []}' > .last_validator_summary.json
echo '{"enriched": 3}' > .last_enrichment_summary.json
cat > .last_llm_summary.json <<'EOF'
{
  "tier_breakdown": {"Top Picks": 3, "Recommended": 6, "Borderline": 3, "Not Recommended": 0},
  "disagreements": [
    {"id": 7, "name": "Ceramics Taster", "python_tier": "Borderline", "llm_tier": "Recommended",
     "llm_reasoning": "Hands-on creative workshop matching past pottery attendance"}
  ]
}
EOF
cat > .pipeline_run_log.json <<'EOF'
[
  {"step": "preflight", "outcome": "success", "detail": "schema ok", "timestamp_utc": "2026-07-04T19:00:00+00:00"},
  {"step": "score-travel", "outcome": "success", "detail": "28 scored", "timestamp_utc": "2026-07-04T19:05:00+00:00"}
]
EOF

# Stamp every state file so the data-driven freshness check reads them
python3 - <<'PY'
import json
from erlib.freshness import stamp
for p in (".last_run_status.json", ".last_fetch_counts.json",
          ".last_validator_summary.json", ".last_enrichment_summary.json",
          ".last_llm_summary.json"):
    with open(p) as f:
        d = json.load(f)
    with open(p, "w") as f:
        json.dump(stamp(d), f)
PY

bash weekly_run.sh pipeline-summary > /dev/null 2>&1

python3 - <<'PY'
import json
with open(".last_pipeline_summary.json") as f:
    summary = json.load(f)
# Time-derived fields: scrub (asserted for presence, not value)
assert summary.pop("batch_date", None), "batch_date missing"
assert summary.pop("batch_label", None), "batch_label missing"
with open("scrubbed.json", "w") as f:
    json.dump(summary, f, indent=2, ensure_ascii=False, sort_keys=True)
PY

if [ "${1:-}" = "--capture" ]; then
  mkdir -p "$(dirname "$FIXTURE")"
  cp scrubbed.json "$FIXTURE"
  echo "CAPTURED fixture -> $FIXTURE"
  cat "$FIXTURE"
  exit 0
fi

if diff -u "$FIXTURE" scrubbed.json; then
  echo "PASS: pipeline-summary output matches golden fixture"
else
  echo "FAIL: pipeline-summary output diverged from golden fixture"
  exit 1
fi
