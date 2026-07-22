# Event Recommender: contributor guide

A weekly pipeline that discovers local events, scores them against a
personal taste profile, and surfaces picks on a Notion board. README.md
has the architecture and product story; docs/setup-guide.md gets an
instance running. This file is the working contract for anyone (human or
agent) changing the code.

## Hard rules (machine-enforced)

CI (`.github/workflows/ci.yml`) and the repo pre-commit hook
(`scripts/git-hooks/pre-commit`, installed automatically by the
session-start hook) enforce these: violating them blocks the commit
locally and fails the build remotely:

1. **Check `erlib/` before writing any helper.** Config, constants, DB
   access, the Notion client, normalisation, dates, dedup, OAuth refresh
   and run-log already live there. Private copies are banned patterns:
   `scripts/check_no_duplication.py` fails the build on them. If the
   helper you need doesn't exist, add it to `erlib/` with a test.
2. **All Notion calls go through `erlib.notion`.** It carries retry,
   bounded 429 backoff, pagination (two hand-rolled clients silently
   dropped every row past 100 before it existed), and UTF-16-safe
   truncation.
3. **`make test` before every commit; CI green before merge.**
4. **A veto change touches three surfaces + a test:** the taste profile
   (`references/taste-profile.md`, or the template if you're changing the
   shipped example), `VETO_PATTERNS` in `score_candidates.py`, the
   lettered checklist in `SENSE_CHECK_INSTRUCTION`
   (`llm_sense_check.py`): plus the registry in
   `scripts/check_veto_sync.py` and a `_SELF_TEST_CASES` case.
   `check_veto_sync.py` fails CI when the surfaces drift.
5. **Frozen automation interfaces.** `references/scheduled-task-prompt.md`
   invokes `weekly_run.sh` subcommands, scripts, and JSON file shapes by
   name; `scripts/check_prompt_contract.py` fails CI if you rename or
   remove anything it references without updating the prompt in the same
   commit.

## Keyword matcher principle

Every keyword check in `score_candidates.py` goes through
`matches_any()`: a whole-word (`\b`-bounded) regex match. Raw substring
matching is banned in that file. Why: a substring matcher once scored a
badminton club as a top pick because "withdraw" (in the cancellation
policy) fired a creative-activity signal.

Consequences when adding keywords or signals:

- Scope matters. Default scope is the full text blob; short or
  polysemous keywords must scope to `title_org_text()` instead:
  description boilerplate is too noisy for them.
- Every new keyword gets a `_SELF_TEST_CASES` case; known collision
  risks get a `forbidden_signals` case with a realistic description.
  The self-test runs at script startup and exits non-zero on regression.
  Never disable it.
- Veto exemptions follow the existing pattern (exemption keyword list
  checked in `check_veto`, suppressing only its own veto): see
  `CLASSICAL_CROSSOVER_KEYWORDS` and `VIBE_CODING_EXEMPTION_KEYWORDS`.
- Title-scoped veto categories live in `TITLE_SCOPED_VETOES`; the LLM
  letters provide defence in depth for what title-scoping lets through.

## Database semantics

SQLite is the source of truth; Notion is presentation. Column semantics
that have bitten people before:

- **`pipeline_state`** is the pipeline disposition (`pending_llm`,
  `pending_travel`, `ready_to_write`, `written`, `vetoed`,
  `llm_rejected`, `expired`, `duplicate`, `incomplete`, `write_failed`).
  It does NOT track the Notion page lifecycle: events can be
  retroactively reclassified without touching their page.
- **`notion_status`** tracks the actual page lifecycle (`active`,
  `archived`, `deleted`, NULL = never written). Queries targeting
  "events visible in Notion" use `notion_status = 'active'`: never
  `pipeline_state = 'written'`: paired with
  `notion_page_id IS NOT NULL` when calling the API.
- **`llm_tier` determines Notion eligibility, not `tier`.** They often
  disagree; that is by design (`tier` is the Python pre-assessment).
- Rows with `pipeline_state='written'` AND `llm_tier='Not Recommended'`
  are historical (the LLM ran retrospectively): not a bug.
- Before filtering on any column, `SELECT DISTINCT <column>` to verify
  actual values. Don't assume from names.
- A DB trigger blocks resetting written candidates to pending states:
  if you hit it, you're about to regress reviewed state.

Expiry is by **event date, never run date**: an event that hasn't
happened yet must be processed regardless of when it was fetched.
Newsletter candidates are one-shot (each email is processed once):
expiring one prematurely loses the event permanently.

## Write path

All Notion event writes go through `write_notion.py` and its eligibility
filter: no direct API calls, no MCP writes. If a row is rejected, fix
the upstream data. `reconcile_notion.py` runs as a hard gate after
writes; orphan pages (Notion page without a SQLite match) stop the
pipeline. In the automated weekly run, Notion writes and the DB commit
happen atomically in the same CI job (`write-notion.yml`): sessions
that write but don't commit create orphans.

## Layout

| Path | What |
|---|---|
| `erlib/` | Shared library (rule 1) |
| `pipeline/` | The step scripts, each runnable (`python3 pipeline/<name>.py`) and importable. See below. |
| `pipeline/fetch_meetup.py`, `pipeline/fetch_luma.py` | Source fetchers (public APIs) |
| `pipeline/score_candidates.py` | Veto pre-filter + signal scoring + self-tests |
| `pipeline/llm_sense_check.py` | Batch prompt prep + result write-back (`--prepare`/`--apply`) |
| `pipeline/validate_llm_output.py` | Post-LLM validator (hard-veto floor, travel checks, Borderline cap) |
| `pipeline/travel_time.py` | Google Routes v2, 90-day venue cache |
| `pipeline/write_notion.py`, `pipeline/reconcile_notion.py`, `pipeline/sync_verdicts.py` | The only Notion touchpoints |
| `pipeline/feedback_digest.py`, `pipeline/apply_findings.py` | Fortnightly feedback loop |
| `weekly_run.sh` | Thin dispatcher over the deterministic plumbing |
| `seed_demo_data.py` | Synthetic demo events for the offline quickstart |
| `references/` | Taste profile template + scheduled-task prompt |
| `scripts/` | CI guards, setup (init/bootstrap/location), backfill utilities, git hooks |
| `docs/` | Setup guide + taste interview |
| `tests/` + per-script `--smoke-test` | The offline suite (`make test`) |

Each `pipeline/` script starts with a small sys.path bootstrap so it runs
both standalone (`python3 pipeline/foo.py`) and when imported. Scripts
outside the package import them as `from pipeline.foo import ...`. Paths to
repo-root resources (state files, `references/`) resolve from
`Path(__file__).resolve().parent.parent`, not the package dir.

## Session hooks

`.claude/settings.json` wires two hooks for agent sessions in this repo:

- **SessionStart** runs `scripts/session-start-git-check.sh`, which sets
  `core.hooksPath` to `scripts/git-hooks` (installing the pre-commit
  guard) and runs `git pull --ff-only`. It warns about concurrent
  sessions on this machine, uncommitted changes, unpushed commits, and a
  diverged branch; address those warnings before starting new work.
- **PreToolUse** (Bash) injects the Database semantics reminder whenever
  a command touches `sqlite3` or `event-recommender.db`.

## Working style

- Deterministic scripts for plumbing; LLM judgement only where keywords
  can't decide. Never put an LLM in a loop that moves values from A to B.
- Verbatim means verbatim: never paraphrase source content into event
  fields; unavailable source ⇒ field is None, never a stand-in.
- One session per clone: the DB is a single binary file in git: two
  concurrent writers silently lose one side's work. The session-start
  hook warns when it detects concurrent sessions.
