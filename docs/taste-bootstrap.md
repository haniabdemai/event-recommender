# Build Your Taste Profile From Your History (one-off setup)

This is the **primary** way to create your taste profile, and the one the
original system was built on: rather than inventing your preferences from
scratch, you derive them from the events you have actually attended. Your
Google Calendar is the seed; your event emails add to it; an agent session
turns the patterns into a profile that you review and approve.

The principle, straight from the original design: **zero enumeration
burden. You never think of everything from scratch. The system generates
the lists from your data and you review, edit, or approve.**

If you would rather not connect your Calendar, skip this and use
[`taste-interview.md`](taste-interview.md) instead: the interview builds a
complete profile on its own. The two are complementary: bootstrap from
history, then use the interview to fill gaps history can't show.

---

## Step 1: Review your Calendar (deterministic)

With your Google Calendar OAuth set (`GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`: the same credentials the
feedback digest uses; see the setup guide):

```bash
python3 scripts/bootstrap_taste_profile.py --months 12
```

It reads the last year of your Calendar, filters out virtual and personal
events (using the same filters as the feedback digest), and writes a
summary of what you actually attend: the event types, the venues and
series you return to, the full list of events. It prints the summary and
saves `.taste_bootstrap.json` for the next step. Nothing is written to
your profile yet: this only gathers the evidence.

## Step 2 (optional, richer): mine your event emails

In your agent session, also search your inbox for more signal, the way the
original system used Gmail:

- **Event confirmations / RSVPs** (Eventbrite, Meetup, Luma, ticketing):
  more events you attended, plus the organisers behind them.
- **Newsletter subscriptions** from venues and promoters: these become
  your `ER_MEETUP_GROUPS` and newsletter senders: the sources the weekly
  pipeline will read. Record them with
  `python3 pipeline/newsletter_tracker.py --add-sender "<email-or-domain>" --sender-name "<display name>"`.
- **Google Maps saved / starred places**, if you have an export: the
  venues you bookmarked are strong taste signal too.

This is agent-driven (it uses your logged-in session, like the newsletter
step), so there is no extra API key.

## Step 3: Draft the profile from the evidence (agent)

In your agent session:

> "Draft my taste profile from `.taste_bootstrap.json` (and the emails you
> found) using docs/taste-bootstrap.md. Follow the three-surface rules."

The agent reads your attendance and produces a first draft of
`references/taste-profile.md`, seeded from real patterns:

- **Strong positive signals**: the event types and activities that recur
  in your history. High confidence: you keep going to them.
- **`KNOWN_ORGANISERS` / `KNOWN_SERIES_PATTERNS`**: the organisers,
  venues, and series you return to (Signals #31/#32). Directly from the
  "recurring" sections of the summary.
- **Newsletter senders / Meetup groups**: from Step 2.
- **Price and travel thresholds**: inferred from what you've paid and how
  far you've gone, for you to confirm.
- **Candidate vetoes**: categories that are conspicuously *absent* from
  your history. Treat these as *proposals to confirm*, not facts:
  never-attended is weaker evidence than often-attended, so the agent
  lists them and asks. (Your firm "no"s are captured better by the
  interview's veto walkthrough: Q11 there.)

It applies the changes across all three surfaces plus the registry: the
same checklist as [`taste-interview.md`](taste-interview.md) Part 2: so
`check_veto_sync.py` stays green.

## Step 4: Review and approve

Read the draft. Fix anything the data got wrong (a one-off work event that
slipped the filter, an organiser you actually didn't like). This is the
only place you edit from judgement: everything else came from evidence.

## Step 5: Fill the gaps with the interview

History shows what you've *done*, not everything you'd *enjoy*: things
you're new to, or want to try, or firmly want to avoid but never booked.
Run [`taste-interview.md`](taste-interview.md) to supplement: it layers
onto the bootstrapped profile rather than replacing it. The interview's
veto walkthrough (Q11) is especially worth doing: vetoes are hard to
infer from attendance alone.

## Step 6: Verify

```bash
make check-fast && make smoke      # veto surfaces in sync, self-tests green
python3 seed_demo_data.py --fresh && python3 pipeline/score_candidates.py
```

You now have a working, data-derived profile. From here the weekly
pipeline scores real events against it, and the fortnightly feedback
digest (`pipeline/feedback_digest.py`) keeps refining it from what you
actually attend: the same loop that maintained the original.
