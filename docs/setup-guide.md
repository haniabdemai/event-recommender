# Setup Guide

From clone to your first scored batch in three stages. Stage 1 needs no
accounts or keys; stages 2-3 wire up real sources one at a time: each is
independent, so stop whenever you have enough.

**Run your instance privately.** This public repo is a template. Your own
copy will accumulate personal data: your taste profile, a database of
events near your home, your newsletter senders. Use a **private** fork or
a fresh private repo for the running instance, and keep the taste profile
(`references/taste-profile.md`) and `.env` out of git (both are
gitignored here).

---

## Stage 1: Offline demo (5 minutes, no keys)

Prerequisites: Python 3.11+ and `pip install ruff pytest` if you want the
full test suite.

```bash
git clone <your-private-copy> && cd event-recommender
python3 scripts/init_db.py        # empty SQLite DB, full schema
python3 seed_demo_data.py         # 10 synthetic events
python3 pipeline/score_candidates.py       # veto + signal scoring
python3 pipeline/llm_sense_check.py --prepare
```

You'll see the funnel work: vetoes fire (tennis, pub quiz, a blocked
organiser), high scorers surface, one event lands in "Couldn't Process",
and `--prepare` writes a review batch for the LLM step. The demo scores
against the shipped example persona ("Alex": see
`references/taste-profile-template.md`).

`make test` runs the full offline suite (~650 checks).

## Stage 2: Make it yours

1. **Taste profile.** Two ways to build it, and you can combine them:
   - **From your history (recommended, the original method):** once your
     Calendar OAuth is set (Stage 3), run
     `python3 scripts/bootstrap_taste_profile.py --months 12`, then in an
     agent session draft the profile from what it found. It derives your
     profile from the events you've actually attended so you approve rather
     than invent. Full flow: [`docs/taste-bootstrap.md`](taste-bootstrap.md).
   - **From an interview (no accounts needed):** open a Claude Code session
     and say "interview me using docs/taste-interview.md". Builds a
     complete profile on its own; also the way to fill gaps your history
     can't show (start here if you want to set up before wiring Calendar).

   Either way it rewrites the three taste surfaces and verifies them with
   `make check-fast`.
2. **Environment.** `cp .env.example .env`.

   **Set your city with one command**: no manual lookups:

   ```bash
   python3 scripts/setup_location.py "Berlin" --write
   ```

   It resolves and writes everything the pipeline needs to work in *your*
   city, using free keyless services: coordinates and country (OpenStreetMap),
   timezone (timeapi.io), the geocoding suffix/region, and your Luma
   discovery area (read live from `lu.ma/<city>` and validated against the
   events API). Run it without `--write` first to review the values.

   If Luma uses a non-obvious slug for your city (e.g. New York is
   `lu.ma/nyc`), pass `--luma-slug nyc`. If Luma doesn't cover your city
   at all, that one value is left at its default and everything else still
   works: Meetup discovers from your coordinates, so it needs no place id.
   Nothing here is London-specific; the same command works for any city.

   Then fill in the rest of `.env` as you go: `ER_HOME_POSTCODE` /
   `ER_HOME_LABEL` (your base for travel times) and `ER_MEETUP_GROUPS`
   (the Meetup groups you belong to).
3. **Notion board** (where picks land):
   - Create an integration at notion.so/my-integrations, put its token
     in `NOTION_TOKEN`.
   - Create a Notion page, connect the integration to it
     (••• → Connections), then:

     ```bash
     python3 scripts/bootstrap_notion.py --parent-page <page-id>
     ```

     Copy the printed ids into `.env`. Optionally add a "Place" property
     in the Notion UI for map pins (the API can't create that type).
4. **First real fetch** (no keys needed). One honest caveat: the Meetup
   and Luma "public APIs" are unofficial public endpoints fetched with a
   browser User-Agent, and they may change or throttle without notice.

   ```bash
   python3 pipeline/fetch_meetup.py --dry-run     # then without --dry-run
   python3 pipeline/fetch_luma.py --dry-run
   python3 pipeline/score_candidates.py
   ```

5. **LLM sense-check.** `python3 pipeline/llm_sense_check.py --prepare` writes
   batch prompts to `.llm_batch_prompts/`. Review them with whatever LLM
   you drive: the intended setup is a Claude Code session reading the
   batch files directly (no API key involved; see the README's "What it
   relies on"). Apply results with `--apply`.
6. **Write to Notion.**

   ```bash
   python3 pipeline/write_notion.py --dry-run     # inspect, then run without
   ```

## Stage 3: Optional integrations

Each is independent; skip freely.

- **Travel times**: Google Cloud project with the Routes API enabled;
  key in `GOOGLE_MAPS_API_KEY`; then `python3 pipeline/travel_time.py`. Free tier
  covers a weekly batch of new venues comfortably (results are cached in
  the `venues` table for 90 days).
- **Newsletters**: needs a Gmail connection in the session that runs the
  pipeline (e.g. a Gmail MCP). Register senders as you go:
  `python3 pipeline/newsletter_tracker.py --add-sender "<email-or-domain>" --sender-name "<display name>"`.
- **Google Calendar sync + feedback digest**: OAuth client
  (`GOOGLE_CLIENT_ID`/`SECRET`/`REFRESH_TOKEN`, `GCAL_CALENDAR_ID`);
  `python3 pipeline/sync_to_gcal.py`, fortnightly `python3 pipeline/feedback_digest.py`.
  The digest writes its findings to a Scoring Feedback database in
  Notion: create it by hand from the schema below and put its id in
  `FEEDBACK_DB_ID`.
- **Weekly automation**: the full unattended run is driven by
  `references/scheduled-task-prompt.md` (an agent session orchestrating
  `weekly_run.sh` + the GitHub Actions in `.github/workflows/`). Wire it
  to any scheduler that can run an agent session on your repo (e.g.
  Claude Code scheduled tasks). Set repo secrets for the workflows you
  use: `NOTION_TOKEN`, `NOTION_DATABASE_ID`, `PIPELINE_RUNS_DB_ID`, and
  the Google credentials above if calendar/label steps are enabled.
  Probe everything with `bash weekly_run.sh health-check`.

  Once your secrets are configured, set the repository variable
  `ER_SCHEDULES_ENABLED=true` (repo Settings → Secrets and variables →
  Actions → Variables) to enable the weekly schedules. Until you set it,
  the cron-triggered workflows (feedback-digest, sync-calendar,
  sync-verdicts, verify-run) skip their scheduled runs, so a fresh copy
  of the repo doesn't fail red before it's configured. Manual and
  push-triggered runs work regardless.

  The scheduled prompt's Step 7B scopes Notion MCP searches by
  data-source id (`collection://…`), which is not the same string as the
  database id. `bootstrap_notion.py` prints the two database ids for
  `.env`; to capture the matching data-source ids, ask your Notion MCP to
  fetch each database once and record the `collection://` id it reports.
  Keep both pairs with your setup notes.

### Scoring Feedback database

`feedback_digest.py` and `apply_findings.py` expect a third Notion
database, which `bootstrap_notion.py` does not create. Create it manually
(same parent page, connect your integration) with these properties, and
put its id in `FEEDBACK_DB_ID`:

| Property | Type | Options / notes |
|---|---|---|
| Month | Title | Page title: finding title, or "June 2026" for summary rows |
| Row Type | Select | `Summary`, `Finding` |
| Status | Select | `New`, `Approved`, `Applied` (add `Rejected` for your own triage) |
| Finding Type | Multi-select | `Organiser`, `Series`, `Veto Exemption`, `Source Gap`, `Tier Mismatch`, `Over-recommended`, `False LLM Reject`, `Expired But Attended` |
| Finding key | Rich text | Stable key used to dedupe findings across digests |
| Evidence | Number | Occurrence count backing the finding |
| Period | Date | Date range the evidence spans |
| Rationale | Rich text | Why the digest proposed it |
| Action | Rich text | What approving it will do |
| Events attended | Number | Summary rows |
| Pipeline matches | Number | Summary rows |
| Source gaps | Number | Summary rows |
| Scoring corrections | Number | Summary rows |
| New interests | Rich text | Summary rows |

(The digest adds a `Health` rich-text property itself if it's missing.)
Your triage loop: findings arrive as `Status=New`; set the ones you agree
with to `Approved`; the next `apply_findings.py` run (Step 3B of the
weekly automation) absorbs `organiser`/`series` findings into
`scoring_overrides.json` and marks them `Applied`.

## Changing your taste later

Hard vetoes live on three synced surfaces: profile, `VETO_PATTERNS`,
LLM letters: plus the registry in `scripts/check_veto_sync.py`. The
change workflow and the post-first-run refinement questions are in
[`docs/taste-interview.md`](taste-interview.md) Parts 2-3. CI fails if
the surfaces drift, so let it catch you.
