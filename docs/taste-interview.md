# Build Your Taste Profile: Guided Interview

The recommender is only as good as its taste profile. This interview turns
your preferences into the three surfaces the pipeline scores with:

1. `references/taste-profile.md`: your profile (copy the template first)
2. `VETO_PATTERNS` + signal keyword lists in `score_candidates.py`
3. The lettered veto checklist in `SENSE_CHECK_INSTRUCTION`
   (`llm_sense_check.py`)

**The interview builds a complete, working profile on its own**: you do
not need anything else to start. It exists so that anyone can set up
without connecting any accounts.

That said, the richer path is to **bootstrap from your real history first**
(what you've actually attended), then use this interview to fill the gaps
history can't show. See [`taste-bootstrap.md`](taste-bootstrap.md). If you
did the bootstrap, this interview *supplements* the draft it produced
rather than starting over: answer only the parts your history left open
(most importantly the veto walkthrough, Q11, which attendance can't infer).

The intended way to run it: **open this file in a Claude Code (or similar)
session and say "interview me using docs/taste-interview.md"**. The
session asks the questions conversationally, then applies your answers to
all three surfaces and verifies the result. You can equally answer the
questions on paper and make the edits yourself: the mapping section below
tells you exactly where each answer lands.

---

## Part 1: The interview

Answer in your own words; concrete examples beat adjectives. "I went to a
pottery class and loved it" is worth ten "I like creative stuff".

### Who you are

1. Roughly how old are you, and which age framings on events feel right
   vs wrong? (e.g. "20s-30s yes, 30s-40s no", or "age framing doesn't
   matter to me")
2. What city are you in, and where's home? (postcode → `.env`, not the
   profile)
3. What do you do professionally, and which industries' events would you
   attend *for your career* even if the host is corporate?
4. Which communities are you part of, where an event "for X people"
   reads as *for you*? (These become positive signals, and exceptions to
   the identity veto.)

### What you love

5. Think of the last five events you genuinely enjoyed. What were they,
   and what made each one work?
6. What activities would you happily do with strangers? (making things,
   sports, music, tech, wellness, food…)
7. Which sports: if any: do you actually want to play? Be precise:
   list sports you'd attend and sports that *sound* adjacent but are a
   "no" (the example persona: climbing yes, tennis/padel/badminton no).
8. Any organisers, venues, or event series you already know and trust?
   (Seeds `KNOWN_ORGANISERS` / `KNOWN_SERIES_PATTERNS`: Signal #31/#32.)
9. Any specific artists or performers you'd travel for? (Signal #7.)

### What you avoid

10. Think of events you went to and regretted, or always scroll past.
    What patterns do you see?
11. Which of these common formats are a hard NO for you: and which are
    fine? Go through the list one by one; each maps to a shipped veto
    you'll keep, adapt, or delete:
    dating/singles events · pub quizzes/karaoke/escape rooms/pub crawls ·
    board games/tabletop · book clubs & discussion groups · classical
    concerts · technical training · startup/pitch networking · corporate-
    hosted events · networking drinks with no activity · language
    exchange · expat/"international professionals" socials · guided city
    walks · group exhibition visits · public speaking clubs · "young
    professionals" events · walk-through "instagrammable" exhibitions ·
    token craft workshops · wellness upsells (manifestation retreats etc.)
12. Any organisers you already know you want blocked outright?
13. What's your price ceiling: the number above which an event is
    auto-rejected no matter what? (`COST_VETO_THRESHOLD`, default £30)
14. How far will you travel for a routine weekly thing, and for a
    one-off special thing? (feeds the travel rule)
15. Any time-of-day constraints? (default: nothing before 10am)

### Edge cases (worth a minute each)

16. For every hard NO in Q11, is there an *exception* flavour you'd still
    want to see? (The engine supports these: e.g. the persona vetoes
    classical concerts *except* crossover, and technical training *except*
    vibe-coding sessions.) Name your exceptions.
17. Are there words that would wrongly trigger your vetoes? (e.g. if you
    veto "tennis" but love "table tennis", say so now: this decides
    keyword scoping and self-test cases.)

---

## Part 2: Applying the answers (for the session doing the edits)

Work through this checklist; do not skip the tests.

1. `cp references/taste-profile-template.md references/taste-profile.md`
   (gitignored) and rewrite every section from the answers. Keep the
   `## Hard Vetoes` heading: `check_veto_sync.py` parses it.
2. For each veto kept/added/removed, touch all three surfaces **and** the
   registry:
   - `VETO_PATTERNS` (or the hardcoded `check_veto` branches),
   - the letter list in `SENSE_CHECK_INSTRUCTION`,
   - the profile's Hard Vetoes section,
   - `MAPPING` / `LETTER_PROFILE_MARKERS` in `scripts/check_veto_sync.py`.
3. Every new keyword gets at least one `_SELF_TEST_CASES` case; every
   Q17 collision risk gets a `forbidden_signals` case with a realistic
   description. Keyword matching must go through `matches_any` (whole-word
   regex): raw substring checks are banned in `score_candidates.py`.
4. Update the positive-signal keyword lists (`AI_TECH_KEYWORDS`,
   `CREATIVE_KEYWORDS`, `SPORT_KEYWORDS`, …), `KNOWN_ORGANISERS`,
   `KNOWN_SERIES_PATTERNS`, and the known-artists list from Q5-Q9.
5. Set `.env`: home postcode/label, city, `ER_MEETUP_GROUPS`.
6. Verify: `make check-fast && make smoke`: `check_veto_sync` must pass
   with your profile, and every self-test must be green.
7. Re-seed and sanity-check the funnel:
   `python3 seed_demo_data.py --fresh && python3 pipeline/score_candidates.py`
   (adjust demo events if your persona diverges wildly from the example).

When `make check-fast && make smoke` are green and the funnel tiers your
demo events sensibly, **you have a complete, working taste profile**: the
pipeline can run against it. Part 3 is optional tuning for later.

---

## Part 3: Refinement questions (after your first real run)

Run these after the first week or two of real recommendations, ideally
with the pipeline's output open. The feedback digest
(`feedback_digest.py`) automates parts of this fortnightly once Calendar
sync is wired, but the questions work standalone:

1. Look at everything tiered **Top Picks/Recommended** that you did NOT
   want to attend. What did the scorer over-value? (Usually: a keyword
   list too broad, or a repeat-format bonus firing on generic meetups.)
2. Look at **Borderline/Not Recommended** events you actually wanted.
   Which signal should have fired and didn't: or which veto fired
   wrongly? Add the counter-example to `_SELF_TEST_CASES` before
   changing the keyword, so the fix is pinned.
3. Did any event slip through a veto category you thought was covered?
   Add the missing keyword AND the LLM letter nuance.
4. Which organisers appeared twice with the same (good or bad) outcome?
   Promote to `KNOWN_ORGANISERS` or `blocked_organiser` respectively.
5. Was anything vetoed by cost that you'd have paid for? Adjust
   `COST_VETO_THRESHOLD` or the category price thresholds in your
   profile.
6. Were travel penalties right? If you kept skipping far events the
   profile said were fine, tighten the travel rule (and vice versa).
7. Re-run `make check-fast && make smoke` after every adjustment.

Iterate in small batches: one or two rule changes per run, so you can
see each change's effect in the next batch.
