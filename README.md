# event-recommender

Most event platforms answer "what's on near you?". The harder question is
"what's worth *my* Saturday?", and answering it well takes more than
keyword alerts. It takes an opinionated model of your taste, applied
consistently, every week, without you scrolling anything.

This is a weekly pipeline that discovers local events from newsletters,
Meetup, Luma, and venue websites, scores them against a personal taste
profile (30+ hard veto rules and ~25 weighted signals, then an LLM
sense-check for the judgement calls keywords can't make), and surfaces the
survivors as a tiered shortlist on a Notion board. You triage with one
click, and it watches what you actually do to get more like you over time.

## How it works

It runs as a loop with three stages: you teach it your taste once, it
shortlists events for you every week, and it learns from what you attend.

**1. You teach it your taste (once, at setup).** The profile is built the
way the original was: from a **one-off review of your real history**.
`bootstrap_taste_profile.py` reads the events you've actually attended
(your Google Calendar), and an agent session turns the patterns it finds
(the venues and organisers you return to, the event types you go to, what
you pay and how far you travel) into your taste profile, which you review
and approve (`docs/taste-bootstrap.md`). You never enumerate anything from
scratch. Prefer not to connect your Calendar? A guided interview
(`docs/taste-interview.md`) builds a complete profile on its own instead;
either way the profile is entirely yours, nothing preset. Then
`setup_location.py` points it at your city and `bootstrap_notion.py`
creates your board.

**2. Every week it builds your shortlist.** It discovers events from your
sources, scores each one against your taste, and writes the good ones to
your Notion board, tiered Top Picks / Recommended / Borderline:

```
SOURCES                      PIPELINE                        OUTPUT
-------                      --------                        ------
Meetup (public API)    ┐                                 ┌ Notion board
Luma (public API)      ┤  dedup  ->  Python scoring      │   (tiered picks)
Gmail newsletters      ┤            (veto + signals)     │
Venue sites            ┘             |                   ├ SQLite DB
                             travel time (Routes API)    │   (source of truth)
                                      |                  │
                             LLM sense-check (2-pass:    ├ Google Calendar
                             veto checklist + judgement) ┘   (optional sync)
                                      |
                             validators + QA gates  ->  written to your board
```

**3. It learns from what you actually do.** You triage the board with one
tap (Going / Not going). A fortnightly feedback digest then reviews your
Google Calendar, sees which recommended events you genuinely attended,
cross-references your verdicts, and proposes concrete scoring changes:
signals to add, vetoes that fired wrongly, organisers worth trusting. You
approve the ones you like and the scorer absorbs them, so next week's
shortlist is sharper. That closing loop is what makes it *yours* rather
than a generic filter.

Three design decisions carry the engineering weight:

- **Deterministic first, LLM last.** Everything mechanical (fetching,
  dedup, keyword vetoes, travel times, Notion writes) is plain Python
  with self-tests that run at script startup. The LLM only sees
  candidates that survived the deterministic gates, and its output passes
  through a validator that auto-corrects hard-veto violations. No LLM sits
  in the loop for anything that doesn't need judgement.
- **Taste as a synced contract.** Your vetoes live on three surfaces: a
  prose profile for the LLM, `VETO_PATTERNS` regexes for the scorer, and a
  lettered checklist in the sense-check prompt. `scripts/check_veto_sync.py`
  fails CI the moment they drift, because they *did* drift once and
  badminton reached the board.
- **Every failure became a gate.** Whole-word keyword matching (a
  cancellation policy's "withdraw" once fired a creative-activity signal
  on a badminton club), orphan-page reconciliation, write-time
  validators, a prompt-contract check freezing the automation's
  interfaces, a duplication guard keeping helpers in `erlib/`. The CI
  suite is ~650 checks, all offline.

## Getting started

Five minutes, no accounts:

```bash
git clone https://github.com/haniabdemai/event-recommender && cd event-recommender
python3 scripts/init_db.py
python3 seed_demo_data.py
python3 pipeline/score_candidates.py
```

You'll watch ten synthetic events run the funnel: three vetoed (wrong
sport, pub quiz, blocked organiser), two Top Picks, a classical-crossover
exemption, one "Couldn't Process".

> **The shipped taste profile is a demo, not a preset you're meant to
> use.** It belongs to a *fictional* example persona: "Alex", a climbing,
> synth-tinkering creative technologist: so the repo runs the moment you
> clone it. **You build your own** in the guided interview, and it replaces
> the example entirely. There is no built-in taste; the system is designed
> to model *anyone's*.

**It works for any taste, in any city.** Two commands make the pipeline
yours:

```bash
python3 scripts/setup_location.py "Berlin" --write   # your city: see below
# then, in an agent session: "interview me using docs/taste-interview.md"
```

`setup_location.py` asks for your city and resolves everything the
pipeline needs from free, keyless services: coordinates, timezone,
geocoding region, and your Luma discovery area (read live from
`lu.ma/<city>`). Nothing is London-specific: the London defaults are just
a starting point. If a city isn't on Luma, Meetup still discovers from
your coordinates. The taste interview then models your preferences,
however different they are from the example.

From there, [docs/setup-guide.md](docs/setup-guide.md) covers the rest of
a real instance: one-command Notion bootstrap
(`scripts/bootstrap_notion.py`), real fetches, and the weekly automation.
Run your instance in a **private** fork: your taste profile and event
database are personal data.

## What it relies on

| Dependency | Used for | Cost | Optional? |
|---|---|---|---|
| Notion API (integration token) | The recommendations board you triage on | Free | Swappable: see below |
| Meetup + Luma public APIs | Event discovery | Free, no keys | Core |
| Google Maps Routes API | Door-to-door travel times (3 modes, 90-day venue cache) | Free tier covers weekly volume | Yes: skip travel data |
| Gmail access in your agent session (e.g. a Gmail MCP) | Newsletter event extraction | Free | Yes: API sources still work |
| Google Calendar OAuth | Verdict sync + feedback digest | Free | Yes |
| An agent session (e.g. Claude Code) | The LLM sense-check **and** the weekly orchestration | Your existing subscription: **the pipeline holds no LLM API key** | Semi: see below |
| GitHub Actions | Atomic Notion-write + DB-commit jobs | Free tier | Yes: run steps locally |

The LLM sense-check deserves a note: `llm_sense_check.py --prepare`
formats batch prompts to files, the session you're already running reads
and judges them, `--apply` writes results back. The host session *is* the
model: there's no Anthropic/OpenAI key anywhere in the pipeline, and the
weekly scheduled run is just an agent session following
`references/scheduled-task-prompt.md`. Without any LLM you still get the
deterministic scorer's tiers; the sense-check layer is what catches the
judgement calls.

A note on the discovery sources: the Meetup and Luma "public APIs" are
unofficial public endpoints, fetched with a browser User-Agent, and they
may change or throttle without notice.

**Not using Notion?** Built against Notion, but the board contract is
small: create a page with ~20 properties, archive it, read back three
triage fields (Verdict / Reason / Notes). `write_notion.py` and
`sync_verdicts.py` are the only two scripts that touch it: SQLite is the
source of truth throughout: so porting to another board means
reimplementing those two files' API surface and `scripts/bootstrap_notion.py`.

## Configuration

Everything deployment-specific is env-driven and documented in
`.env.example`: city and timezone, home postcode for travel, your Meetup
groups, database ids, optional integration credentials. The one exception
is the Gmail OAuth token (`GMAIL_TOKEN_JSON`), which lives only as a
GitHub Actions secret, never in `.env`. Taste lives in
`references/taste-profile.md` (gitignored; the shipped template is the
example persona) plus the scoring keyword lists: the interview doc walks
you through personalising all of it, and CI verifies you kept the
surfaces consistent.

## Tests

```bash
make test        # pytest wrappers + bash suites (~650 checks, all offline)
make check-fast  # lint + import sweep + CI guards (<10s)
```

Every scoring script also self-tests at startup and refuses to run on a
regression.

## Status

Stable: extracted July 2026 from a system in weekly production use since
April 2026. Location is fully env-driven (city, timezone, coordinates,
geocoding region, Luma discovery area: see the config block in
`.env.example`): the defaults describe London, the city it was first
built against, but point them at any city and the same pipeline runs
unchanged. The taste engine, sources, and scoring carry no geographic
assumptions of their own.

## Built with

Python 3 (stdlib-only at runtime: no pip dependencies), SQLite, the
Notion REST API, Meetup GraphQL + Luma public APIs, Google Routes v2,
GitHub Actions, and an agent session as the orchestrator/LLM.

## Licence

MIT
