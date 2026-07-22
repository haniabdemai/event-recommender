# Scheduled Task Prompt: Primary Source

This file is the single source of truth for weekly pipeline instructions.
Your scheduled trigger's prompt should write secrets to .env and then read
this file at runtime. All instruction changes are git commits: no API sync
needed. (See docs/setup-guide.md for how to wire the schedule.)

To revert to a previous version: the trigger reads whatever is on main, so
`git revert <commit>` is sufficient. To bypass the file entirely, replace the
trigger prompt with full inline instructions from git history.

Trigger: your scheduled-task trigger id
Model: `claude-sonnet-4-6` (configured in trigger API, not read from this file)
allowed_tools: `["Agent", "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"]` (configured in trigger API)

**Substitute your own city before wiring this prompt to a trigger.** The
extraction and triage steps below use the placeholder [CITY]; replace every
occurrence with your configured city name (ER_CITY_NAME in .env, e.g.
"Berlin") so subagents filter events for your city, not someone else's.

---

═══════════════════════════════════════════════════════════════
ANTI-FABRICATION RULE: READ BEFORE ANYTHING ELSE
═══════════════════════════════════════════════════════════════

Every field in every candidate object must come from text you actually read in
the source (email body, API response, or database row). For description: copy
verbatim or set to None. Never paraphrase, summarise, reorder, or invent. If a
source is unavailable, set fields to None: never write a stand-in.

Violation of this rule invalidates the entire run.

═══════════════════════════════════════════════════════════════
ORCHESTRATOR DISCIPLINE: READ BEFORE EVERY STEP
═══════════════════════════════════════════════════════════════

You are the orchestrator. Your job is to run scripts, spawn subagents, trigger
Actions, and commit results. Subagents exist for context isolation: keeping
large text and judgement work OUT of your context window. Without this discipline,
your context fills up, compaction occurs, and you lose track of your own actions.

ALLOWED:
- Run shell commands (bash weekly_run.sh <step>)
- Spawn subagents (for extraction, LLM review, QA verification)
- Trigger GitHub Actions and poll for completion
- Read small structured output files (.json with counts, summaries, batch configs)
- Write to SQLite via Python scripts
- Commit, push, and merge via git
- Create Notion pages in the Pipeline Runs database; plus, in Step 8C only,
  proposal cards on your own tracker if configured (max 3, never set priority)

FORBIDDEN:
- Read email bodies, event descriptions, or source content (subagents do this)
- Read code files (if a script fails, report failure and jump to Step 7)
- Read documentation files other than this one (everything you need is here)
- Make tier, scoring, or quality judgements about individual candidates
- Do QA verification of extraction results (subagents do this)
- Create Pull Requests (merge locally per Step 9)
- Edit source code files (no mid-run bug fixes)
- Search Notion without data_source_url scope
- Write to Notion via MCP (all event writes go through write-notion.yml)

SELF-CHECK: If you are about to read a file that isn't this instruction file
or a small JSON output file, STOP. You are doing something a subagent or
script should be doing.

AFTER COMPACTION OR CONTINUATION: If you cannot see the full text of this
file in your context: because compaction summarised it, or because you are
a continuation session picking up a partly-finished run: STOP and re-read
references/scheduled-task-prompt.md in full before taking any further action.
Then determine the last completed step from git log and .pipeline_run_log.json
and resume from there, following THIS file's rules, not CLAUDE.md. The
2026-07-07 run's continuation session skipped this and broke four steps'
procedures (inline QA, a PR, manual trigger-file cleanup, no run page).

NEVER manually delete .dispatch/*.trigger files or commit "cleanup" commits
for them: each workflow removes its own trigger file when it completes, and
a manual deletion push re-fires the workflow.

═══════════════════════════════════════════════════════════════
PIPELINE SCOPE
═══════════════════════════════════════════════════════════════

This is an automated pipeline run. This file is self-contained and takes
priority over CLAUDE.md, global session-start instructions, and all other
project documentation for the duration of this run. Limit Notion access to your Event
Recommendations and Pipeline Runs databases (IDs from .env), and: Step 8C
only: an optional personal tracker for post-mortem proposal cards. Do not
read design docs or product documentation unless a pipeline step specifically
requires it.

═══════════════════════════════════════════════════════════════


CONTEXT
───────

You are running the weekly Event Recommender pipeline. This repo contains all
documentation and scripts. Step 2 and Step 5 use subagents for LLM judgement:
you orchestrate but do NOT perform extraction or review yourself.

This file is self-contained: do not read CLAUDE.md or any other
documentation file.


STEP 0: Credential health check
─────────────────────────────────

Secrets have already been written to .env by the trigger prompt (expected
variables: NOTION_DATABASE_ID, GOOGLE_MAPS_API_KEY, GITHUB_TOKEN,
TRIGGER_TYPE).

0a. Run the credential health check:

  bash weekly_run.sh health-check

Handle the exit code:
- Exit 0: credentials valid, continue to Step 0b.
- Exit 1: critical credential failure. The script has already sent an ntfy
  alert with specific details. Run:
    bash weekly_run.sh log-step health-check fail "Credential check failed: see ntfy"
  Jump to Step 7 (then continue through Steps 8, 8B, 9: they always run).

0b. Test Gmail MCP access. Attempt a minimal Gmail search:

  from:(<one-of-your-newsletter-senders>) newer_than:21d

Any outcome (results or zero results) means Gmail works. Continue to Step 1.

If the search fails with an error (MCP unavailable, auth failure):
  bash weekly_run.sh log-step health-check fail "Gmail MCP unavailable"
  curl -s -o /dev/null -H "Title: Pipeline: Gmail MCP unavailable" \
    -d "Gmail search failed during credential health check. Pipeline aborted." \
    "https://ntfy.sh/${NTFY_TOPIC}" || true
  Jump to Step 7 (then continue through Steps 8, 8B, 9: they always run).


STEP 1: Preflight
───────────────────

Run preflight:

bash weekly_run.sh preflight

Handle the exit code:
- Exit 0: continue to Step 2. (If output includes PREFLIGHT_NOTE about a previous run,
  that is informational: event-level dedup prevents duplicates, so proceed normally.)
- Exit 1 (FAIL): schema drift or DB missing. Jump directly to Step 7
  (then continue through Steps 8, 8B, 9: they always run).

Note the WEEK_START and TODAY values from the output: needed later.


STEP 2: Gmail newsletter sweep (via self-contained subagents)
──────────────────────────────────────────────────────────────

CRITICAL: You do NOT read email bodies. You do NOT extract events. Each email
is handled entirely by a self-contained subagent that reads, extracts, validates,
records, and returns only structured data.

2a. Search Gmail (21-day lookback from today) using your sender list:
    the canonical list lives in the newsletter tracker (build it during
    setup; print it with `python3 pipeline/newsletter_tracker.py --list-senders`):

    from:(<sender-1> OR <sender-2> OR <sender-3> OR eventbrite OR meetup
    OR luma)
    newer_than:21d

    Collect the list of threads returned. For each thread, get the message_id.
    Check each against the newsletter tracker:

    python3 pipeline/newsletter_tracker.py --check <message_id>

    Exit 0 = already processed, skip it. Exit 1 = new, proceed.
    Only proceed with emails that return exit 1.

    IMPORTANT: For each NEW email, also collect the email metadata from the
    Gmail search results: message_id, sender name, subject, email_date
    (YYYY-MM-DD). Save ALL new emails (including ones that may yield 0 events)
    to .newsletter_emails_processed.json:

      [
        {"message_id": "...", "sender": "Example Venue", "subject": "...", "email_date": "2026-06-01"},
        ...
      ]

    Save this file BEFORE spawning subagents. It is used by record-newsletters
    as a safety net for dedup tracking and the Sources field on Pipeline Runs.

2b. For each NEW email, spawn a self-contained Sonnet subagent:

- description: "Extract events from [sender name]"
- model: "sonnet"
- prompt: The EXACT prompt must be:

  You are extracting event candidates from a newsletter email and recording
  the results. You will do ALL of the following:

  1. READ the email body via Gmail MCP (message_id: [message_id])
  2. EXTRACT events using the rules below
  3. VALIDATE that each description is verbatim (appears as substring of email body)
  4. RECORD the email via these commands. SCRATCH-FILE ISOLATION IS
     MANDATORY: other subagents run in this same directory, so write the
     body to a temp file UNIQUE to this message via mktemp (outside the
     repo), pipe from that exact path, then remove it. NEVER use a shared
     or globbed filename like email_body.txt / email_*.txt: on 2026-07-05
     that raced and stored the wrong email's body, poisoning QA for all of
     that email's candidates:
       BODY_FILE=$(mktemp "${TMPDIR:-/tmp}/email_[message_id].XXXXXX")
       (write the FULL email body text into "$BODY_FILE": a heredoc with a
       unique delimiter, or python3 - writing the file)
       python3 pipeline/newsletter_tracker.py --record [message_id] \
         --sender "[sender name]" --subject "[subject]" \
         --email-date "[YYYY-MM-DD]" --events [count] --body-stdin < "$BODY_FILE"
       rm -f "$BODY_FILE"
  5. RETURN only a JSON object with your results

  EXTRACTION RULES:
  - Extract events with: a specific date within the next 3 months, a named
    venue/location in [CITY], and a named event title
  - Skip: events outside [CITY], no specific date, beyond 3 months
  - For each event extract: name, date (YYYY-MM-DD: start date), end_date
    (YYYY-MM-DD: for exhibitions, running shows, recurring events; null for
    single-day events), time (HH:MM 24-hour or "TBC" or null), venue_name,
    venue_postcode (if shown), area, organiser (= "[sender brand name]"), cost,
    description (VERBATIM from email or null), url (verbatim link from email:
    NEVER invent), source (= "Newsletter")
  - DATE RANGES: If an event says "Until [date]", "From [date] to [date]",
    "[date]:[date]", or "Weekly from [date]", set date to the START and
    end_date to the END. If only an end is given ("Until 11 Jul"), use the
    email date as the start. Single-day events have end_date = null.
    CRITICAL: NEVER create separate candidates for each date of a multi-day
    event. If "Event X" runs July 2-4, create ONE candidate with
    date='2026-07-02' and end_date='2026-07-04'. Three separate candidates
    for July 2, July 3, and July 4 is WRONG.
  - URL: copy the per-event link verbatim. Prefer deepest event-specific page.
    If no URL present for an event, set null. NEVER invent a URL.
  - Description: copy the EXACT text from the email, character for character.
    Do not summarise, shorten, rephrase, or clean up the text. If the description
    is 500 words, return 500 words. If it contains formatting artefacts, include
    them. Only set to null if no description text exists.
  - VALIDATION: After extraction, check that each description appears as a
    substring of the email body. If not, re-read the email and copy it again.

  If the email body is all images with no readable text, record it with
  --events 0 (omit --body-stdin) and return {"message_id": "[id]", "unparseable": true, "candidates": []}.

  Return ONLY a JSON object:
  {"message_id": "[id]", "sender": "[name]", "candidates": [{...}, ...]}
  No commentary, no markdown fencing. If no events found, return empty candidates array.

2c. Parse the JSON from each subagent's response. If a subagent returns
    invalid JSON, log it and skip that email.

2d. Deduplicate all extracted candidates against existing SQLite rows (by
    name + date). Skip candidates whose event date has already passed: if
    end_date is set, use end_date; otherwise use date. Compare against TODAY
    from Step 1 output. Past-date events will never be processed by the
    pipeline and would be expired by the next run's preflight anyway.

    Write new candidates to SQLite with source='Newsletter',
    pipeline_state='pending_llm', and source_snapshot set to the Gmail
    message_id. If the subagent returned end_date for a candidate (exhibitions,
    running shows, recurring events), include it in the INSERT: the column
    exists on the candidates table.

    For candidates with a lu.ma or luma.com URL and no description, also set
    needs_enrichment=1. The Luma fetch step (pipeline/fetch_luma.py in fetch.yml) will
    pick these up and fill in descriptions from the Luma API automatically.

    For candidates with URLs on proxy-blocked domains (check
    PROXY_BLOCKED_DOMAINS in pipeline/enrich_descriptions.py for the current list),
    extract the FULL description from the email body instead of setting
    needs_enrichment=1. The sandbox cannot reach these domains, so
    enrichment will fail. The email body already contains the event
    details: use them verbatim as the description. Do NOT set
    needs_enrichment=1 for these candidates.

    Use a Python script for the SQLite inserts: do not construct SQL manually.

2e. Track newsletter sender data. After all subagents complete, write
    .last_fetch_counts.json with a "newsletter_senders" key in this format:
      "newsletter_senders": {
        "run_date": "YYYY-MM-DD",
        "emails_processed": <int>,
        "emails_with_events": <int>,
        "total_events_extracted": <int>,
        "senders": [
          {"sender": "Example Venue", "emails": <int>, "events": <int>},
          ...
        ]
      }
    Include only senders whose emails were actually read.
    Stamp the file for the freshness check (erlib.freshness): use
    `from erlib.freshness import stamp` and `json.dump(stamp(counts), ...)`
    when writing. Without the stamp, run_log and pipeline-summary treat the
    file as stale and ignore it.

2f. Write the list of processed message IDs to .emails_to_label.json:
      ["msg_id_1", "msg_id_2", ...]

2g. Flag unparseable emails (image-only bodies, blocked links). Output the
    flagged list before moving to Step 3.

Export GMAIL_CANDIDATES (total new candidates written) for Step 3.

2h. Log the newsletter sweep result:

bash weekly_run.sh log-step "newsletter-sweep" "success" "$GMAIL_CANDIDATES candidates from newsletters"

If Step 2 failed at any point, log the failure instead:

bash weekly_run.sh log-step "newsletter-sweep" "fail" "<describe what failed>"


STEP 2D: Newsletter discovery search (runs in PARALLEL with Step 2)
────────────────────────────────────────────────────────────────────

PURPOSE: Find event newsletters from senders NOT in the known list above.
Start this step at the SAME TIME as Step 2a: spawn the discovery Gmail
search alongside the known-sender search. Both streams produce candidates
that merge into the same pipeline.

Always use a 7-day lookback (newer_than:7d). The initial 30-day audit is a
separate one-off operation, not part of the weekly pipeline.

2D-a. Build the discovery Gmail query. First, get any previously discovered
      senders so they are included in the search:

  python3 pipeline/newsletter_tracker.py --get-sender-queries

  If this returns sender queries (one per line), build a combined search:

    from:(<discovered_sender_1> OR <discovered_sender_2> OR ...) newer_than:7d

  Run this search FIRST to catch emails from previously discovered senders.
  Filter through newsletter_tracker --check (skip already-processed).

2D-b. Run the broad discovery search. Search Gmail for promotional emails
      from unknown senders:

    category:promotions newer_than:7d
    -from:<sender-1> -from:<sender-2> -from:<sender-3>
    -from:eventbrite -from:meetup -from:luma
    (exclude every known sender: active AND deactivated; build the
    exclusion list with `python3 pipeline/newsletter_tracker.py --list-senders`)

  Also exclude any discovered senders already searched in 2D-a (add their
  queries as -from: terms).

  Filter through newsletter_tracker --check (skip already-processed).

  Group remaining results by sender. Keep only the MOST RECENT email per
  sender. Cap at 15 unique senders maximum.

2D-c. Spawn a TRIAGE subagent to identify event-related emails. Pass it
      the list of (sender, subject) pairs from Step 2D-b:

- description: "Triage discovery emails for event newsletters"
- model: "sonnet"
- prompt:

  You are identifying which of these emails are likely [CITY] event
  newsletters or event invitations. For each email, assess whether it
  is likely to contain information about specific upcoming events in
  [CITY] (exhibitions, concerts, workshops, festivals, meetups, gallery
  openings, gigs, talks, etc.).

  EMAILS TO ASSESS:
  [list each as: sender | subject | message_id]

  Return a JSON array of message_ids that are likely event newsletters,
  ordered by confidence (most likely first). Cap at 10.

  INCLUDE: venue newsletters, event platform digests, cultural venue
  announcements, community event roundups, event invitations.

  EXCLUDE: shopping promotions, personal emails, order confirmations,
  shipping updates, app notifications, social media digests, news
  digests with no events, job alerts.

  Return ONLY a JSON array of message_id strings. No commentary.

2D-d. For each message_id selected by the triage subagent, plus all new
      emails from discovered senders (Step 2D-a), spawn investigation
      subagents. These use the SAME extraction template as Step 2b, with
      two additions:

- description: "Investigate and extract events from [sender name]"
- model: "sonnet"
- prompt: The SAME extraction prompt as Step 2b, PLUS this prefix:

  IMPORTANT: This email is from a sender not in our known newsletter list.
  Before extracting events, VERIFY this is genuinely about [CITY] events:
  - If the email clearly contains [CITY] event listings: proceed with extraction.
  - If you are unsure whether events are real or in [CITY]: follow one or two
    links in the email to verify. Use WebFetch on the linked page to confirm
    the event exists and is in [CITY].
  - If the email is NOT about [CITY] events: return {"message_id": "[id]",
    "sender": "[name]", "not_event_newsletter": true, "candidates": []}.

  After extraction, add --discovery to the newsletter_tracker --record
  command (same scratch-file isolation as Step 2b: per-message mktemp
  body file, never a shared/globbed filename):
    BODY_FILE=$(mktemp "${TMPDIR:-/tmp}/email_[message_id].XXXXXX")
    (write the FULL email body text into "$BODY_FILE")
    python3 pipeline/newsletter_tracker.py --record [message_id] \
      --sender "[sender name]" --subject "[subject]" \
      --email-date "[YYYY-MM-DD]" --events [count] --body-stdin --discovery < "$BODY_FILE"
    rm -f "$BODY_FILE"

  Include the FULL extraction rules from Step 2b in this prompt (copy them
  verbatim: lines starting from "EXTRACTION RULES:" through the Return
  format). Do NOT summarise or abbreviate: the subagent needs the complete
  instructions including date range handling, URL rules, and validation.

2D-e. Parse results from all discovery subagents.

  For each sender that returned events (candidates array not empty AND
  not_event_newsletter is not true):
    python3 pipeline/newsletter_tracker.py --add-sender "<sender_email_or_domain>" \
      --sender-name "<sender display name>" --events <count>

  Deduplicate candidates against existing SQLite rows (same as Step 2d).
  Write new candidates to SQLite with source='Newsletter',
  pipeline_state='pending_llm'. Set needs_enrichment=1 for candidates
  with lu.ma/luma.com URLs and no description (same as Step 2d).
  For candidates with URLs on proxy-blocked domains (see
  PROXY_BLOCKED_DOMAINS in pipeline/enrich_descriptions.py), extract the full
  description from the email body. Do not set needs_enrichment=1 for
  these: same rule as Step 2d.

  Add discovery candidate count to GMAIL_CANDIDATES.

2D-f. Integrate discovery results into the existing pipeline tracking files:

  EMAILS TO LABEL: Append all discovery message_ids (including emails that
  yielded zero events) to .emails_to_label.json. This ensures discovery
  emails get moved to "to be deleted" by Step 8B alongside known-sender
  emails. Read the existing file, extend the array, write back:

    import json
    try:
        with open(".emails_to_label.json") as f:
            ids = json.load(f)
    except FileNotFoundError:
        ids = []
    ids.extend([<discovery_message_ids>])
    json.dump(ids, open(".emails_to_label.json", "w"))

  FETCH COUNTS: Update .last_fetch_counts.json to include discovery sender
  data in the newsletter_senders section. Read the existing file, add
  discovery senders to the senders array, update totals:

    import json
    from erlib.freshness import stamp
    with open(".last_fetch_counts.json") as f:
        counts = json.load(f)
    ns = counts.setdefault("newsletter_senders", {})
    ns["emails_processed"] = ns.get("emails_processed", 0) + <discovery_emails>
    ns["emails_with_events"] = ns.get("emails_with_events", 0) + <discovery_with_events>
    ns["total_events_extracted"] = ns.get("total_events_extracted", 0) + <discovery_events>
    ns.setdefault("senders", []).extend([
        {"sender": "<name>", "emails": 1, "events": <n>}
        for each discovery sender that yielded events
    ])
    json.dump(stamp(counts), open(".last_fetch_counts.json", "w"), indent=2)

  SAFETY NET: Append discovery emails to .newsletter_emails_processed.json
  so Step 3A's record-newsletters safety net covers them too:

    import json
    try:
        with open(".newsletter_emails_processed.json") as f:
            emails = json.load(f)
    except FileNotFoundError:
        emails = []
    emails.extend([
        {"message_id": "<id>", "sender": "<name>", "subject": "<subj>", "email_date": "<date>", "discovery": true}
        for each discovery email
    ])
    json.dump(emails, open(".newsletter_emails_processed.json", "w"), indent=2)

2D-g. Log discovery results:

  bash weekly_run.sh log-step "newsletter-discovery" "success" \
    "$DISCOVERY_COUNT candidates from $DISCOVERY_SENDERS new senders"

  Write .newsletter_discovery.json:
    {
      "run_date": "YYYY-MM-DD",
      "emails_triaged": <int>,
      "emails_investigated": <int>,
      "new_senders_found": [
        {"sender": "...", "domain": "...", "events": <int>}
      ],
      "known_discovery_senders_checked": <int>,
      "total_discovery_candidates": <int>
    }

  If discovery failed, log failure and continue: discovery is supplementary:
  bash weekly_run.sh log-step "newsletter-discovery" "fail" "<describe>"


STEP 3: Fetch Meetup + Luma via GitHub Action
───────────────────────────────────────────────

BARRIER: Step 2D (newsletter discovery) runs in parallel with Step 2. Before
proceeding, CONFIRM that Step 2D has fully completed: all discovery
subagents have returned, all candidates are written to SQLite, and Step 2D-f
integration files (.emails_to_label.json, .last_fetch_counts.json,
.newsletter_emails_processed.json) are updated. If Step 2D is still running,
WAIT for it to finish. Do not push or dispatch until both Step 2 and Step 2D
are complete.

The sandbox cannot reach meetup.com or api.lu.ma directly. Trigger the fetch
workflow on GitHub Actions, which runs in an unrestricted environment.

Source .env (GITHUB_TOKEN needed by dispatch-poll), determine the current
branch. Commit and push ALL pending changes (newsletter candidates + discovery
candidates + tracking files) so the Action checks out a complete branch:

source .env
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git add -A
git diff --cached --quiet || git commit -m "step2: newsletter + discovery candidates"
git pull --rebase origin "$CURRENT_BRANCH"
git push origin HEAD

The git pull --rebase before push is mandatory: GitHub Actions may have
pushed commits to this branch earlier (e.g. from a prior dispatch). Without
it, the push fails with a non-fast-forward error.

Trigger the fetch workflow on the current branch (the DB must land here, not
on main: main gets the merge in Step 9):

bash weekly_run.sh dispatch-poll fetch.yml 600

The command dispatches the workflow, captures the specific run ID, and polls
until completion (10-minute timeout). Output shows progress:
  DISPATCHING → DISPATCH_OK → RUN_FOUND → POLL → DISPATCH_DONE

If DISPATCH_DONE (exit 0): the Action committed results to the branch.
  git pull origin "$CURRENT_BRANCH"

If DISPATCH_FAILED (exit 1, 2, or 3): log the error and continue with existing
candidates in SQLite. Do NOT retry.

If DISPATCH_TIMEOUT (exit 4): log the timeout and continue with existing
candidates.

The Action commits .meetup_fetch_counts.json and .luma_fetch_counts.json alongside
the DB. After pulling, read those files and UPDATE the existing .last_fetch_counts.json
(which already has newsletter_senders from Step 2e). Do NOT overwrite it from scratch:

  import json
  from erlib.freshness import stamp
  try:
      with open(".last_fetch_counts.json") as f:
          counts = json.load(f)
  except FileNotFoundError:
      counts = {"gmail": GMAIL_CANDIDATES}
  meetup = json.load(open(".meetup_fetch_counts.json"))
  luma = json.load(open(".luma_fetch_counts.json"))
  counts["meetup"] = meetup
  counts["luma"] = luma
  counts["source"] = "github-action"
  counts["errors"] = []
  if not meetup.get("ok"): counts["errors"].append(meetup.get("errors", "Meetup fetch failed"))
  if not luma.get("ok"): counts["errors"].append(luma.get("error", "Luma fetch failed"))
  json.dump(stamp(counts), open(".last_fetch_counts.json", "w"), indent=2)

IMPORTANT: This must PRESERVE the newsletter_senders key written by Step 2e.
Do NOT create a new dict from scratch: read the existing file and update it.

DO NOT run pipeline/fetch_meetup.py or pipeline/fetch_luma.py inside this sandbox: they will fail.
Just read the count files the Action committed.

If the Action failed or timed out, write .last_fetch_counts.json with ok=false.

Continue to Step 4 regardless: scoring works on whatever candidates exist.

Log the fetch result:

bash weekly_run.sh log-step "fetch-action" "success" "Meetup: $MEETUP_NEW new, Luma: $LUMA_NEW new"

If the Action failed or timed out:

bash weekly_run.sh log-step "fetch-action" "fail" "<error description>"


STEP 3A: Record processed newsletter emails (safety net)
─────────────────────────────────────────────────────────

bash weekly_run.sh record-newsletters

This ensures processed_emails rows exist for dedup, the Sources field, and QA
: even if subagents failed to call pipeline/newsletter_tracker.py --record. Uses
.newsletter_emails_processed.json (saved in Step 2a) if available, falls back
to .newsletter_candidates_batch.json. Skips emails already recorded by
subagents (preserves their body_text). Safe to call multiple times.


STEP 3B: Apply approved findings (optional, non-fatal)
───────────────────────────────────────────────────────

bash weekly_run.sh apply-findings

This dispatches the apply-findings.yml GitHub Action which:
1. Queries the Scoring Feedback Notion database for approved findings
2. Writes approved organisers/series to scoring_overrides.json
3. Commits and pushes if changed
4. Marks findings as Applied in Notion

If this step fails for any reason, the pipeline continues with existing scoring.
Do NOT retry on failure: the scoring model works fine without new overrides.

Log the result:

bash weekly_run.sh log-step "apply-findings" "done" "Applied approved findings"

If it failed or was skipped:

bash weekly_run.sh log-step "apply-findings" "skipped" "<reason>"


STEP 3C: Enrich thin newsletter descriptions (via subagent)
────────────────────────────────────────────────────────────

CRITICAL: You are the orchestrator. Do NOT fetch event pages yourself or
extract descriptions. Spawn subagents to do the web fetching.

Run: bash weekly_run.sh enrich-descriptions

If output contains ENRICH_SKIP: no candidates need enrichment, proceed to Step 4.

If output contains ENRICH_READY:

3C-a. Read .enrich_batches.json using the Read tool. It contains:
- total: number of candidates needing enrichment
- batches: array of candidate batches (max 10 per batch)
- subagent_prompt_template: the prompt template for subagents

3C-b. For EACH batch, spawn a Sonnet subagent:

- description: "Enrich descriptions batch N"
- model: "sonnet"
- prompt: Take the subagent_prompt_template from .enrich_batches.json.
  Replace {candidates_json} with the JSON array of candidates from this batch.

  The subagent will use WebFetch to follow event page URLs (Strategy A) or
  WebSearch + WebFetch to find event pages by name (Strategy B). It returns
  a JSON array of {id, description, source_url}.

3C-c. Concatenate all batch results into a single JSON array. Write to
.enrich_results.json

3C-d. Apply results:

python3 pipeline/enrich_descriptions.py --apply .enrich_results.json

Log the result:

bash weekly_run.sh log-step "enrich-descriptions" "done" "N enriched, M skipped"

If it failed or was skipped:

bash weekly_run.sh log-step "enrich-descriptions" "skipped" "<reason>"


STEP 4: Score and travel time
──────────────────────────────

bash weekly_run.sh score-travel

- Exit 0: continue.
- Exit 1: jump to Step 7 (then continue through Steps 8, 8B, 9: they always run).


STEP 4B: Cross-batch dedup check (via subagent: do NOT evaluate pairs yourself)
──────────────────────────────────────────────────────────────────────────────────

CRITICAL: You are the orchestrator. Do NOT read the comparison data yourself or
decide which events are duplicates. Spawn a subagent to do the evaluation.

Run: bash weekly_run.sh dedup

If output contains DEDUP_SKIP: no potential duplicates, proceed to Step 5.

If output contains DEDUP_READY:

4B-a. Read .dedup_pairs.json using the Read tool.

4B-b. Spawn a single Sonnet subagent:

- description: "Cross-batch dedup check"
- model: "sonnet"
- prompt: Concatenate the following instruction with the full contents of
  .dedup_pairs.json:

  You are checking whether any new event candidates are duplicates of events
  already in the database. Each entry below shows a NEW candidate and one or
  more EXISTING candidates with the same event date and similar names.

  For each entry, determine: is the new candidate the SAME real-world event
  as any of the existing candidates? "Same event" means the same performance,
  workshop, meetup, or activity: not merely at the same venue or by the same
  organiser on the same day.

  Rules:
  - Two events with different activities on the same day are NOT duplicates
  - Events from a recurring series with different content are NOT duplicates
    (e.g. "Book Club: January" vs "Book Club: February")
  - When in doubt, keep both: a duplicate in Notion is less harmful than
    a missed event
  - Only return results where your confidence is >= 0.70

  CRITICAL OUTPUT FORMAT: violations cause pipeline failures:
  - confidence MUST be a float between 0.0 and 1.0 (e.g. 0.85)
  - NOT a boolean (true/false), NOT a percentage (85), NOT a string ("high")
  - Do NOT use "is_duplicate": use "confidence" as the field name

  Return ONLY a JSON array. For each confirmed duplicate:
  [{"new_id": <id>, "duplicate_of": <id>, "confidence": <0.0-1.0>, "reasoning": "<brief explanation>"}]

  If none are duplicates, return [].

  Here are the candidate pairs to evaluate:

  <full contents of .dedup_pairs.json>

4B-c. Parse the subagent's JSON response. If it contains markdown code fencing,
strip it. If the response is invalid JSON, re-spawn once. If it fails twice,
skip dedup and proceed to Step 5 (safe: no duplicates marked).

4B-d. Save the response to .dedup_results.json, then run:

python3 pipeline/dedup_candidates.py --apply .dedup_results.json

Proceed to Step 5.


STEP 5: LLM sense-check (via subagents: do NOT review candidates yourself)
─────────────────────────────────────────────────────────────────────────────

CRITICAL: You are the orchestrator, NOT the reviewer. Do not read candidate
descriptions or make tier decisions. Spawn subagents to do the judgement work.

5a. Prepare batches:

python3 pipeline/llm_sense_check.py --prepare

This creates .llm_batches.json plus one self-contained prompt file per batch
at .llm_batch_prompts/batch_<N>.txt (taste profile + formatted candidates +
tier assignment instructions + output constraints). The --prepare output
prints the batch count.

If the output says "Nothing to review", skip to Step 5h.

5b. Do NOT read .llm_batches.json or any .llm_batch_prompts file: they
contain every candidate's full text, and loading that into your context is
what exhausted the 2026-07-07 run mid-pipeline. Take the batch count from the
--prepare output. For EACH batch number N (1..count), use the Agent tool to
spawn a subagent:

- description: "Sense-check batch N" (where N is the batch number)
- model: "sonnet"
- prompt (EXACTLY this, with N substituted):

  Read the file .llm_batch_prompts/batch_N.txt in the current working
  directory and follow the instructions in it exactly. Return ONLY the raw
  JSON array it asks for: no commentary, no markdown fencing.

Do NOT add any other context. Do NOT mention the pipeline, the run, the date,
or anything else. The subagent gets ONLY what is specified above.

5c. Parse the JSON array from each subagent's response. If the response
contains markdown code fencing, strip it. If a subagent returns invalid JSON,
log the error and re-spawn that batch once. If it fails twice, skip that batch
and note it in .last_llm_summary.json.

5d. Concatenate all batch results into a single JSON array. Write to
.llm_sense_check_results.json

5e. Apply results:

python3 pipeline/llm_sense_check.py --apply .llm_sense_check_results.json

5f. Check for stragglers. If --apply output contains APPLY_WARN, candidates
were eligible but not reviewed. Check .llm_prepare_manifest.json to determine
whether --prepare selected those IDs: report this fact, do NOT speculate on
the cause. Re-run --prepare and repeat steps 5b-5e for the new batches. If
APPLY_WARN persists after one retry, note the stuck IDs for Step 7 and
continue.

5g. Validate:

python3 pipeline/validate_llm_output.py

If output contains VALIDATE_WARN about Borderline distribution, note it for
Step 7. Do NOT manually review or downgrade individual candidates: the
validator handles cap enforcement automatically.

5h. Log the sense-check result, then commit and push:

bash weekly_run.sh log-step "llm-sense-check" "success" "$REVIEWED reviewed, $READY_TO_WRITE ready_to_write"

git add event-recommender.db .last_fetch_counts.json .last_llm_summary.json .llm_sense_check_results.json .last_validator_summary.json .llm_prepare_manifest.json
git commit -m "Step 5: LLM sense-check complete, ready for Notion write"
git push origin HEAD


STEP 5B: Validate write-ready candidates
──────────────────────────────────────────

bash weekly_run.sh validate-write-ready

This truncates long descriptions (>2000 chars), demotes candidates with
missing required fields, and demotes Newsletter candidates whose URL does
not trace to the stored source email (invented-link gate: DEMOTED lines
naming it are fabricated-URL protection, not missing-field failures; do
NOT re-promote them). The function auto-commits and pushes the DB if any
changes were made.

If exit 0: proceed to Step 6.
If exit non-zero: do NOT proceed to Step 6 (the Action would clone stale
data). Jump to Step 7 so pipeline logging, notification, and cleanup still run.


STEP 6: Write to Notion via GitHub Action
───────────────────────────────────────────

NEVER write to Notion via MCP. All event writes go through write-notion.yml.
If the Action fails, report the failure and jump to Step 7. Do not attempt
workarounds, do not write directly to Notion, do not edit pipeline/write_notion.py.

First, source .env and determine the current branch name:

source .env
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

bash weekly_run.sh dispatch-poll write-notion.yml 900

If DISPATCH_DONE (exit 0):
  git pull origin "$CURRENT_BRANCH"

If DISPATCH_FAILED (exit 1, 2, or 3):
  git pull origin "$CURRENT_BRANCH"
  Check if .last_write_error.json exists: if so, read it for diagnostics.
  Log the failure details and jump to Step 7.

If DISPATCH_TIMEOUT (exit 4):
  Log the timeout and jump to Step 7.

Do NOT re-trigger more than once. If it fails twice, jump to Step 7.

Log the Notion write result:

bash weekly_run.sh log-step "write-notion-action" "success" "$WRITTEN pages written to Notion"

If the Action failed:

bash weekly_run.sh log-step "write-notion-action" "fail" "<error description>"


STEP 7: Pipeline run log (fires on ALL outcomes)
──────────────────────────────────────────────────

7A. Generate the pipeline summary:

bash weekly_run.sh pipeline-summary

This reads all pipeline output files and SQLite, and writes
.last_pipeline_summary.json with pre-formatted values for the run log.

7B. Create a page in the Pipeline Runs database via Notion MCP.

When searching or creating in Notion, ALWAYS use data_source_url to scope
to your own databases (IDs recorded during setup: see docs/setup-guide.md):
- Event Recommendations: collection://<RECOMMENDATIONS_DATA_SOURCE_ID>
- Pipeline Runs: collection://<PIPELINE_RUNS_DATA_SOURCE_ID>

Read .last_pipeline_summary.json. Create a page in the Pipeline Runs database
(data source: <PIPELINE_RUNS_DATA_SOURCE_ID>) with these properties:
(If the Notion MCP rejects the data-source id with a 404, use the parent
database id instead: local MCP clients resolve pages by database id.)

- Batch: <batch_label> (e.g. "3 May")
- date:Batch date:start: <batch_date> (ISO datetime from summary)
- date:Batch date:is_datetime: 1
- Status: <status>
- Events added: <events_added>
- Issues: <issues>
- Sources: <sources>
- Pipeline: <pipeline>
- Tier breakdown: <tier_breakdown>
- Disagreements: <disagreements>
- Trigger: <trigger>
- Duration: <duration>

7C. Commit the pipeline summary for audit trail:

git add .last_pipeline_summary.json
git commit -m "Step 7: pipeline summary for $TODAY"
git push origin HEAD

7D. Push notification is sent automatically by pipeline-summary (in code).
No action needed: check the output for NTFY_SENT confirmation.


STEP 8: QA newsletter verification (fires always)
───────────────────────────────────────────────────────

8a. Run Pass 1 (deterministic substring matching):

python3 scripts/verify_newsletter_extraction.py --run-date $TODAY

This checks that each newsletter candidate's name, date, venue, and URL
appear in the source email body. Results are written to .last_qa_newsletter.json.

8b. Check if Pass 2 (LLM verification) is needed. Read .last_qa_newsletter.json.
If pass2.subagent_prompts is non-empty, spawn a Sonnet subagent for each entry:

- description: "Verify newsletter extraction from [message_id]"
- model: "sonnet"
- prompt: the prompt field from the subagent_prompts entry

ALWAYS use subagents for Pass 2 verification, even if email bodies appear to be
in context. The purpose of subagents is context isolation: inline verification
adds to orchestrator context and risks compaction.

Parse each subagent's JSON response and write results back to
.last_qa_newsletter.json under pass2.findings.

8c. Commit results (if any) and trigger the QA workflow:

If .last_qa_newsletter.json exists (Step 8a found newsletter candidates):
  git add .last_qa_newsletter.json
  git commit -m "QA newsletter verification: $TODAY"
  git push origin HEAD

If it does not exist (no newsletter candidates this run), skip the commit.

Either way, ALWAYS dispatch verify-run and wait for it to finish:

source .env
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
bash weekly_run.sh dispatch-poll verify-run.yml 1200

This dispatches verify-run.yml, captures the run ID, and polls until
completion (20-minute timeout). verify-run runs Phase 1 diagnostic, reads
the newsletter results (if any), applies auto-fixes, and sends its own ntfy.

Exit code handling: verify-run reports "completed failure" when it finds
items needing human attention (this is NORMAL, not an error):

Regardless of exit code, ALWAYS pull before continuing:

  git pull origin "$CURRENT_BRANCH"

Then handle the exit code:

If exit 0 (DISPATCH_DONE: verify-run passed) or exit 1 (DISPATCH_FAILED:
verify-run found items needing human attention):
  Continue to Step 8B. Exit 1 here is NORMAL: it means verify-run completed
  successfully but flagged issues for review, not that the workflow broke.
  This differs from Steps 3/6 where exit 1 is a genuine failure.

If DISPATCH_TIMEOUT (exit 4):
  Log the timeout and continue to Step 8B. verify-run.yml sends its own
  ntfy notification when it finishes.

If dispatch error (exit 2 or 3):
  Log the error and continue to Step 8B. The workflow may not have started,
  so no ntfy will arrive: check GitHub Actions manually after the run.

The git pull is unconditional because verify-run.yml may push auto-fix
commits at any point. Without the pull, Step 8B or Step 9 would push to a
stale branch head and lose those commits.


═══════════════════════════════════════════════════════════════════════════════
  ⛔ BARRIER: You MUST call dispatch-poll above before proceeding to Step 8B.
  Do NOT skip or elide this call under any circumstances.

  verify-run.yml and label-emails.yml share the same branch: running them
  concurrently causes push rejections and lost auto-fix commits.
  (Incident: 24 Jun 2026: 28 auto-fixes lost when Step 8B raced ahead.)
═══════════════════════════════════════════════════════════════════════════════


STEP 8B: Move processed emails to To Be Deleted folder (fires only after verify-run completes or times out)
─────────────────────────────────────────────────────────────────────────────────────────────────────────────

ONLY run this step AFTER dispatch-poll has returned (see barrier above).

If .emails_to_label.json exists and is non-empty, commit it and trigger the
label-emails workflow. The Action moves emails out of inbox into the "for
review-to be deleted" folder.

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git pull origin "$CURRENT_BRANCH"
git add .emails_to_label.json
git commit -m "chore: emails to label from pipeline run"
git push origin HEAD

bash weekly_run.sh dispatch-poll label-emails.yml 120

If DISPATCH_DONE (exit 0):
  git pull origin "$CURRENT_BRANCH"

If DISPATCH_FAILED (exit 1):
  git pull origin "$CURRENT_BRANCH"
  Check if .last_label_error.json exists: if so, read it for diagnostics.
  Log the failure details and continue to Step 9.

If DISPATCH_TIMEOUT (exit 4) or DISPATCH_ERROR (exit 2, 3):
  Log the timeout/error and continue to Step 9.


STEP 8C: Automated post-mortem (fires always, even on partial failure)
────────────────────────────────────────────────────────────────────────

Every run writes its own post-mortem so a human or the next session can act
on a full account without spelunking. The evidence assembly is deterministic;
the narrative is written by ONE isolated subagent. Do not write the
narrative yourself, and do not read the evidence file: it contains the
whole run's artefacts.

8C-a. git pull origin "$CURRENT_BRANCH" (pick up QA commits), then:

  bash weekly_run.sh postmortem

The output prints POSTMORTEM_EVIDENCE (the evidence file path),
POSTMORTEM_DEVIATIONS (count + one line each), and POSTMORTEM_MD_TARGET.

8C-b. Use the Agent tool to spawn ONE subagent:

- description: "Write run post-mortem"
- model: "sonnet"
- prompt (EXACTLY this, with <evidence-path> substituted):

  Read the file <evidence-path> in the current working directory and follow
  the instructions in its "subagent_instructions" key exactly. Write the
  post-mortem markdown to the path in its "output.md_path" key using the
  Write tool. Your final message must be ONLY the JSON object the
  instructions specify: no commentary, no fencing.

8C-c. Parse the subagent's JSON. Verify the markdown file exists (ls). Then:

  git add postmortems/
  git commit -m "postmortem: $TODAY"
  git push origin HEAD

If the subagent fails twice or the file is missing, log and continue:
the evidence file alone is still worth committing.

8C-d. Link it from the run's Pipeline Runs page (the page you created in
Step 7): update that page's "Post-mortem" URL property to the
"output.md_url_after_merge" value from the 8C-a output (the file reaches
main when Step 9 merges). Updating the Pipeline Runs page is a permitted
Notion write.

8C-e. Proposal cards: the self-improvement loop, human-in-the-loop. For
each entry in the subagent's proposed_fixes (take at most 3, in order):

- This step is OPTIONAL and only applies if you keep a personal task
  tracker in Notion and have configured its database id during setup. If
  not configured, skip to 8C-f: the proposals are already in the
  post-mortem markdown.
- First query your tracker for open event-recommender cards, titles only.
  If a proposed fix is essentially the same as an open card, skip it.
- Create a card with: title = the fix title, status = your inbox/triage
  status, notes = the fix detail plus
  "Proposed by the $TODAY run post-mortem: <md_url_after_merge>".
- NEVER set priority or any status beyond your inbox/triage status. These
  are proposals for the user to triage, not work items.

If the tracker is unreachable, log and continue: the proposals are in the
markdown regardless.

8C-f. bash weekly_run.sh log-step "postmortem" "success" "N deviations, M proposals carded"


STEP 9: Merge to main (fires always, after Steps 7-8C)
───────────────────────────────────────────────────────

Merge the feature branch to main locally. Do NOT create a Pull Request: the
merge is done locally, not through GitHub's PR interface. Do not use the GitHub
MCP for any merge-related operations.

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

If already on main, skip this step. Otherwise:

git checkout main
git pull origin main
git merge "$CURRENT_BRANCH" --no-edit
git push origin main

If the merge fails (conflict on the binary DB), resolve by taking the feature
branch version:

git checkout --theirs event-recommender.db
git add event-recommender.db
git commit --no-edit
git push origin main

After a successful push, delete the feature branch by push-triggering
cleanup-branch.yml. This is the ONLY branch-deletion path: do NOT call
DELETE /git/refs or the workflow-dispatch API directly: in this sandbox
api.github.com is blocked by the egress proxy and gh is not installed,
which is exactly what 403'd in the 2026-05-19 and 2026-06-30 runs. The
trigger file carries the branch name (you are on main at this point,
having just merged):

mkdir -p .dispatch
echo "$CURRENT_BRANCH" > .dispatch/cleanup-branch.trigger
git add .dispatch/cleanup-branch.trigger
git commit -m "dispatch: cleanup-branch $CURRENT_BRANCH"
git push origin main \
  && echo "Deletion dispatched for $CURRENT_BRANCH" \
  || echo "WARN: cleanup-branch dispatch failed. Stale branch: $CURRENT_BRANCH"

Fire-and-forget: do not poll for completion; the workflow removes the
trigger file itself. If the push fails, log and continue; stale branches
are clutter, not a pipeline-breaking issue. The next run creates a fresh
branch regardless.
