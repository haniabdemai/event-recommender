"""SQLite access helpers: one connect, one additive-migration helper.

connect() is the only sanctioned way to open the pipeline DB: it applies
row_factory=Row so scripts can use column names. ensure_columns() is the
additive PRAGMA→ALTER migration pattern every script used to hand-roll.

today_london() is the canonical "today" for future convergence. Existing
call sites deliberately keep their UTC convention for now: run_date is
stamped in UTC and matched against SQLite date('now') (also UTC) by the
validator: migrating only some of those sites would desync them on
late-evening runs. Converge them together or not at all.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .config import DB_PATH, LONDON_TZ


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open the pipeline DB (or db_path) with row_factory=Row."""
    conn = sqlite3.connect(str(db_path) if db_path is not None else str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> list[str]:
    """Additively add missing columns ({name: SQL decl}). Idempotent.

    Returns the list of columns actually added (empty on a no-op re-run).
    Never drops or alters existing columns.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    added = []
    for col, decl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            print(f"MIGRATE: added column {col} ({decl})")
            added.append(col)
    if added:
        conn.commit()
    return added


def today_london() -> str:
    """ISO date for 'today' in Europe/London."""
    return datetime.now(LONDON_TZ).date().isoformat()


# The full pipeline schema gate, extracted from the weekly_run.sh
# schema_check heredoc in WP4. Column sets are the single source of truth
# for what the pipeline requires; llm_sense_check.LLM_COLUMNS is the
# deliberately narrower set that module alone migrates.
REQUIRED_CANDIDATE_COLUMNS = frozenset({
    "id", "run_date", "name", "date", "time", "tier", "score",
    "signals_fired", "veto_reason",
    "venue_name", "venue_postcode", "area", "organiser", "cost",
    "description", "format_type", "url", "source",
    "travel_transit_min", "travel_cycle_min", "travel_walk_min",
    "travel_display", "travel_lookup_failed",
    "pipeline_state", "notion_page_id",
})

# Columns added after the initial schema: auto-migrated when absent.
MIGRATE_CANDIDATE_COLUMNS = {
    "llm_tier": "TEXT", "llm_reasoning": "TEXT", "llm_reviewed": "TEXT",
    "source_snapshot": "TEXT",
    "needs_enrichment": "INTEGER DEFAULT 0",
    "place_written": "INTEGER DEFAULT 0",
    "description_source": "TEXT",
}

MIGRATE_PROCESSED_EMAIL_COLUMNS = {"body_text": "TEXT", "qa_status": "TEXT"}

# Columns dropped from the schema; their presence means a stale DB.
FORBIDDEN_CANDIDATE_COLUMNS = frozenset({
    "why_youd_like_it", "description_short", "vetoed",
    "verdict_logged_date", "notion_archived",
})


# Full pipeline schema for a fresh database (scripts/init_db.py). Kept in
# sync with REQUIRED/MIGRATE column sets above: schema_check() validates a
# DB created from this DDL, and tests pin the round-trip.
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    name TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT,
    venue_name TEXT,
    venue_postcode TEXT,
    area TEXT,
    organiser TEXT,
    cost TEXT,
    description TEXT,
    format_type TEXT,
    source TEXT,
    url TEXT,
    score INTEGER,
    tier TEXT,
    signals_fired TEXT,
    past_pattern_match TEXT,
    travel_transit_min INTEGER,
    travel_cycle_min INTEGER,
    travel_walk_min INTEGER,
    travel_display TEXT,
    veto_reason TEXT,
    pipeline_state TEXT DEFAULT 'pending_llm',
    notion_page_id TEXT,
    verdict TEXT,
    verdict_reason TEXT,
    verdict_notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    travel_lookup_failed INTEGER DEFAULT 0,
    llm_tier TEXT,
    llm_reasoning TEXT,
    llm_reviewed TEXT,
    notion_status TEXT,
    synced_at TIMESTAMP NULL,
    source_snapshot TEXT,
    place_written INTEGER DEFAULT 0,
    needs_enrichment INTEGER DEFAULT 0,
    user_attended INTEGER DEFAULT 0,
    end_date TEXT,
    description_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_name_date ON candidates(name, date);
CREATE INDEX IF NOT EXISTS idx_candidates_pipeline_state
    ON candidates(pipeline_state);

CREATE TABLE IF NOT EXISTS venues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_name TEXT NOT NULL,
    venue_postcode TEXT,
    travel_transit_min INTEGER,
    travel_cycle_min INTEGER,
    travel_walk_min INTEGER,
    lookup_date TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    venue_lat REAL,
    venue_lon REAL,
    google_place_id TEXT,
    formatted_address TEXT
);
CREATE INDEX IF NOT EXISTS idx_venues_name ON venues(venue_name);
CREATE INDEX IF NOT EXISTS idx_venues_postcode ON venues(venue_postcode);
CREATE INDEX IF NOT EXISTS idx_venues_place_id ON venues(google_place_id);

CREATE TABLE IF NOT EXISTS processed_emails (
    gmail_message_id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    subject TEXT,
    email_date TEXT,
    events_extracted INTEGER DEFAULT 0,
    run_date TEXT,
    processed_at TEXT DEFAULT (datetime('now')),
    body_text TEXT,
    qa_status TEXT,
    discovery INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS discovered_senders (
    sender_query TEXT PRIMARY KEY,
    sender_name TEXT NOT NULL,
    first_seen TEXT DEFAULT (date('now')),
    last_seen TEXT DEFAULT (date('now')),
    emails_processed INTEGER DEFAULT 0,
    events_found INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id TEXT UNIQUE,
    title TEXT NOT NULL,
    location TEXT,
    date TEXT NOT NULL,
    matched_candidate_id INTEGER,
    match_confidence TEXT,
    digest_date TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS digest_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_key TEXT UNIQUE NOT NULL,
    finding_type TEXT NOT NULL,
    target_name TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    events_json TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    notion_page_id TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    additional_types TEXT NOT NULL DEFAULT '[]',
    correction_events TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS format_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    old_type TEXT NOT NULL,
    new_type TEXT NOT NULL,
    run_date TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);

CREATE TABLE IF NOT EXISTS gcal_events (
    candidate_id INTEGER PRIMARY KEY,
    gcal_event_id TEXT NOT NULL,
    last_synced TEXT NOT NULL,
    last_verdict TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_claims (
    candidate_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    claimed_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'in_progress'
);

CREATE TRIGGER IF NOT EXISTS prevent_written_state_regression
BEFORE UPDATE ON candidates
WHEN NEW.pipeline_state IN ('pending_llm', 'pending_travel')
  AND OLD.notion_page_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT,
    'BUG: Cannot set pending state for candidate with Notion page');
END;
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the full pipeline schema on a fresh (or partial) DB. Idempotent."""
    conn.executescript(SCHEMA_DDL)
    conn.commit()


def schema_check(db_path: str | Path | None = None) -> int:
    """Verify + additively migrate the pipeline schema. 0 = OK, 1 = fail.

    Prints SCHEMA_OK / SCHEMA_FAIL exactly as the old heredoc did (the
    scheduled-task log greps for those markers).
    """
    conn = connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for required in ("candidates", "venues"):
        if required not in tables:
            print(f"SCHEMA_FAIL: missing table {required}", file=sys.stderr)
            return 1

    ensure_columns(conn, "candidates", MIGRATE_CANDIDATE_COLUMNS)
    ensure_columns(conn, "processed_emails", MIGRATE_PROCESSED_EMAIL_COLUMNS)

    # State regression guard: prevent scripts from resetting already-written
    # candidates back to pre-review states (root cause: scoring-reset bug 2026-06-02)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS prevent_written_state_regression
        BEFORE UPDATE ON candidates
        WHEN NEW.pipeline_state IN ('pending_llm', 'pending_travel')
          AND OLD.notion_page_id IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT,
            'BUG: Cannot set pending state for candidate with Notion page');
        END
    """)
    conn.commit()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    missing = (REQUIRED_CANDIDATE_COLUMNS | set(MIGRATE_CANDIDATE_COLUMNS)) - cols
    if missing:
        print(f"SCHEMA_FAIL: candidates missing columns: {sorted(missing)}",
              file=sys.stderr)
        return 1
    stragglers = FORBIDDEN_CANDIDATE_COLUMNS & cols
    if stragglers:
        print(f"SCHEMA_FAIL: candidates has dropped columns still present: "
              f"{sorted(stragglers)}", file=sys.stderr)
        return 1
    print("SCHEMA_OK")
    return 0


if __name__ == "__main__":
    sys.exit(schema_check(sys.argv[1] if len(sys.argv) > 1 else None))
