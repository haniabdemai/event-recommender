# Taste Profile: Template (example persona: "Alex")

> **This file is a template.** It ships filled in for a fictional example
> persona: Alex, a creative technologist: so the pipeline runs end-to-end
> out of the box and every mechanism has a concrete example. To make the
> recommender yours, run the guided setup (`docs/setup-guide.md` →
> "Build your taste profile") or copy this file to
> `references/taste-profile.md` (gitignored) and rewrite every section in
> your own words. The pipeline prefers `taste-profile.md` when it exists.
>
> **Keep the three taste surfaces in sync.** The hard vetoes below have
> Python counterparts in `score_candidates.py` (`VETO_PATTERNS`) and LLM
> counterparts in `llm_sense_check.py` (`SENSE_CHECK_INSTRUCTION`).
> `scripts/check_veto_sync.py` fails CI when the surfaces drift: see the
> setup guide for the change workflow.

## Purpose

This document briefs the LLM sense-check step of the event recommender
pipeline. Use it to review event candidates that passed the Python veto
and assign final tiers before writing to the recommendations board.

## How to Use This Document

This document tells you who the user is and what they care about. Read it
to understand them: then use your judgement on each event. Don't treat this
as a checklist to match against. Think: "Would they actually enjoy this if
they went?"

The sections below give you the detail, but the principle is simple: if the
core activity isn't something the user would choose to do, it's Not
Recommended: no matter what other signals are present.

## Output Format

For each candidate, return:
```json
{
  "tier": "Top Picks" | "Recommended" | "Borderline" | "Not Recommended",
  "reasoning": "1-2 sentences explaining why"
}
```

**Tier definitions:**
- **Top Picks**: Confident this is a great fit. Multiple strong signals, no red flags.
- **Recommended**: Good fit. Clear alignment with interests.
- **Borderline**: Uncertain. Might be interesting but not confident. Surface for the user to decide.
- **Not Recommended**: Filtered out. Matches a veto or wrong crowd/vibe.

---

## Who Alex Is

Alex is a creative technologist in their late 20s living in London (the
pipeline's reference city: set yours in `.env`). They want to spend
evenings and weekends making things with interesting people rather than
standing at another drinks reception.

Their struggle: most listed events are either unstructured drinking where
you meet strangers from scratch with no filter, or the wrong vibe entirely
(wrong age band, wrong crowd, wrong energy). Finding the sweet spot is the
whole point of this pipeline.

Professionally, Alex works in music technology: audio software and
creative tools. Events about music tech, audio engineering, and creative
technology are relevant to their career even when hosted by companies.
These are Alex's industries, not the "wrong crowd."

---

## Strong Signals

These indicate a good fit. Multiple matches = higher tier.

### Activities & Interests

- **AI/tech builder community**: Hackdays, jams, vibe coding, hands-on
  building. Social, playful framing. Builders and curious people, not
  hardcore engineers grinding through a syllabus.

- **Hands-on creative activities**: Pottery, printmaking, drawing,
  collage, digital arts. Making in a group with no skill barrier. The
  appeal is creating together, not watching someone else create.

- **Music production / synths / music gear**: The creative and production
  side of music: beat making, songwriting, synth meetups, gear expos,
  music tech communities. If an event name implies music gear or music
  production, treat it as aligned: don't dismiss for a thin description.

- **Listening bars / vinyl-curated venues**: Intimate,
  sound-system-obsessed spaces where music curation IS the venue's
  identity. This is about curated listening, NOT about learning to DJ:
  DJ practice events are a hard veto (below).

- **Sensory/immersive experiences**: Installation art, experiences where
  you're INSIDE the work. The key is participation and immersion, not
  walking past things on walls.

- **Sound baths / breathwork**: Interested, but price-sensitive (see
  Price Thresholds below).

- **Fun / weird / different experiences**: Unusual formats, unexpected
  social mechanics, novel creative social design.

### Sports & Fitness

- **Climbing / bouldering**: Beginner and improver level, social climbing
  nights, top-rope intros. This is Alex's one sport. Everything else is a
  hard veto (below).

### Social Format

- **Socialising through shared activity**: Meeting people through doing
  something together, not forced mingling at a bar. Formats that filter
  attendees by interest.

### My Communities

Events framed FOR a community the user belongs to are a positive signal,
not an identity veto. List yours here: the LLM sense-check reads this
section when applying the identity veto.

- Alex's: the indoor climbing community; maker/creative-tech communities.

### Context-Dependent (worth considering)

- **Hiking**: Day hikes outside the city if not crazy difficult, not too
  far, not expensive.

- **Nature-integrated venues**: Gardens, greenhouses, water as atmosphere.

- **One-off film screenings**: Cult classics, special presentations,
  cinema events about the experience of a specific film on a big screen.
  NOT the same as film clubs (vetoed below). Surface as Borderline unless
  the film is a strong match.

- **Book launches / ideas talks**: Only if the topic is genuinely
  interesting: technology, creativity, society with a real thesis.

---

## Hard Vetoes

If any of these match, assign "Not Recommended" immediately.

**Critical rule: generic positive signals (free, beginner-friendly, social
format, "all welcome") NEVER override a veto. If the core activity is
wrong, the event is "Not Recommended" no matter how cheap, social, or
accessible it is. Do not hedge into "Borderline": veto means veto.**

### Event Types

- **Dating events**: Anything where dating is the explicit purpose:
  speed dating, singles nights, matchmaking. Alex wants to meet people
  through activities, not at events labelled as dating.

- **Board games / tabletop gaming**: Zero interest. Warhammer, D&D,
  board game nights: all hard no.

- **Hardcore technical training**: Node.js, microservices, Kubernetes,
  Docker workshops, system design interview prep, database and platform
  user groups. The audience is engineers learning specific tools. NOT the
  same as builder meetups or vibe coding, which are social/playful.

- **Startup / investment / pitch events**: Pitch nights, demo days,
  investor meetups, VC networking. Alex is not a founder; the energy is
  fundraising and career advancement, not building together. Indie
  maker/solopreneur communities are builder community, not startup
  hustle; assess those on their merits.

- **Book clubs / writing groups / discussion groups**: Recurring groups
  organised around reading, writing sprints, or rotating discussion
  prompts (philosophy circles, debate nights, film clubs). The veto is
  the discussion-group format, not books as a concept. One-off launches
  with a genuinely interesting author are fine.

- **Classical concerts**: Symphony, recital, chamber music, opera,
  philharmonic. Exception: classical crossover with folk, jazz,
  electronic, or film score is fine.

- **Corporate-hosted events**: Big-4 firms, banks, enterprise software
  companies hosting. Corporate hosting attracts the wrong crowd.
  **Exception:** events about music technology, audio engineering, or
  creative tools are Alex's professional field: assess on content.

- **Gimmicky walk-through exhibitions**: Instagrammable set-dressing
  with no depth or participation.

- **Token craft workshops**: 90% talk, 10-minute activity at the end.
  Real workshops have making from start to finish.

- **Language learning events**: Language exchange, language practice,
  learn-a-language meetups.

- **International/expat meetups**: "International professionals",
  "expat social" framing attracts the wrong vibe. Exception: 20s-30s
  socials that merely use the word "global" are fine.

- **Pub quizzes / trivia nights / karaoke / escape rooms / pub crawls**:
  Passive or unstructured pub formats with no creative or physical
  element.

- **DJ practice events**: Open decks, DJ workshops, "learn to DJ". Alex
  produces music but has no interest in DJing. Listening bars and curated
  vinyl nights remain a strong positive.

- **Walks inside London**: Thames paths, canal walks, guided
  neighbourhood walks, historical walking tours, evening strolls. Day
  hikes outside the city remain OK. An activity at multiple locations
  (thrifting, shopping) is not a walk.

- **Group exhibition visits**: Meetup-organised group trips to galleries
  or museums. Going to an exhibition independently is assessed on the
  exhibition's own merits.

- **Park hangouts with no structured activity**: "Hang out in the park"
  with no creative or physical element. A park is a location, not an
  activity. A park on the far side of the city is too far for an
  unstructured hangout.

- **Public speaking clubs**: Structured speaking-drill formats and
  speakers clubs. Not an activity Alex wants.

- **Life drawing**: A recurring format Alex has tried and bounced off.
  (Kept as an example of a personal-history veto with no generic
  category: yours will differ.)

### Social Framing (wrong crowd)

- **Identity-specific events**: Events FOR a religious, ethnic, or
  lifestyle-identity community the user isn't part of. Check the "My
  Communities" section above: events for the user's own communities are
  a positive signal, not a veto.

- **30s-40s age framing**: Events pitched at a "30s & 40s" crowd. Alex
  is late 20s; 20s-30s framing works, 30s-40s does not. (Set your own
  band: and note the over-40s/50s+ patterns are a separate Python-only
  veto.)

- **"Young professionals" framing**: Attracts a corporate,
  career-oriented crowd that doesn't match.

- **Events for professional communities the user isn't part of**: Data
  professionals, game developers, journalism tech. The crowd is wrong
  even if the topic sounds adjacent. The test: is the audience working
  professionals in that field, or curious people from different
  backgrounds? **Exception:** music technology, audio engineering, and
  creative technology communities are Alex's own field and hobby:
  assess on merits.

### Specific blocked organisers/series

Explicitly flagged: always Not Recommended regardless of the specific
event's content. (These are fictional examples; the feedback digest
suggests additions to this list from your attendance history.)

- **"The Hype Collective"**: Passive gig format, asked to stop
  recommending.
- **"Megamix Socials"**: Wrong age framing across every event.
- **"Gallery Wander Club"**: Group exhibition visit format, zero
  attendance across many recommendations.

### Sports & Fitness (wrong type)

- **Any sport that is not climbing or bouldering**: Badminton, football,
  volleyball, cricket, rugby, netball, basketball, tennis, padel, ping
  pong, table tennis, squash, hockey, darts. All "Not Recommended"
  regardless of how social, beginner-friendly, or cheap. Also
  F1/Grand Prix viewing parties: watching sport at a pub, not doing it.
  Cardio fitness classes (Zumba, aerobics, dance fitness) are also out.

---

## Contextual Checks

These require reading the full description, not just matching keywords.

1. **Identity-group fit**: If the description reveals the event is
   specifically FOR a community the user isn't part of, Not Recommended.
   "LGBT-friendly" or "inclusive" language is welcoming, not targeting:
   that's fine. Events for the user's own communities (see My
   Communities) are positive.

2. **Technical vs social tech events**: Is the audience engineers
   learning a tool, or curious builders hanging out? "Kubernetes
   workshop" → Not Recommended. "Vibe coding jam" → Recommended or
   better.

3. **Desperate dating vibes**: Even without a "dating" label, if the
   description gives off desperate singles energy, Not Recommended.

4. **Beginner-friendly fitness check**: Climbing events must be
   explicitly beginner-friendly or all-levels; advanced or intimidating
   framing filters out.

5. **Price vs value**: A substantial immersive exhibition at £20-25 is
   fine; a generic uncertain-quality workshop at £25 gets price
   resistance. Factor into tier placement.

6. **Travel time vs event value**: Well-aligned multi-hour event =
   worth 45+ min. Short event far away = tier drops. See the travel rule
   in the sense-check instruction.

7. **Events with no clear agenda**: Recurring events that never say
   what they'll cover score lower. A regular meetup that names its topic
   is fine; "monthly gathering, come along" is suspect.

---

## Price Thresholds

| Category | Comfortable | Uncertain |
|----------|-------------|-----------|
| Free social events | £0-5 | £5+ |
| Workshops/creative | up to £25 if quality clear | £25+ |
| Exhibitions | up to £20-25 for substantial ones | £25+ for uncertain quality |
| Sound baths/wellness | £10-15 | £25+ |
| Climbing | £20-25, up to £30 max | £30+ |

Anything above £30 is auto-vetoed by the Python layer
(`COST_VETO_THRESHOLD`) before the LLM sees it.

---

## Logistics

- **Home:** set via `ER_HOME_POSTCODE` / `ER_HOME_LABEL` in `.env`
  (travel times are computed from there).
- **Max comfortable travel:** ~45 min public transport. Will stretch for
  the right thing.
- **Time of day:** No events before 10am. Otherwise time of day is not a
  factor.

---

## Summary: Quick Reference

**Strong yes:** builder meetups, hackdays, vibe coding, hands-on creative,
music production, synths, music gear expos, listening bars, immersive
participatory art, climbing/bouldering (beginner), weird/novel social
formats, music tech / creative tech events (professional field).

**Lower tier yes:** casual 20s-30s socials, day hikes outside the city,
one-off film screenings, book launches with genuinely interesting topics,
sound baths at sane prices.

**Hard no:** dating events, tabletop gaming, technical training, startup
pitch networking, book/film/discussion clubs, classical concerts (unless
crossover), corporate events (except music/creative tech), walk-through
exhibitions, token craft workshops, language exchange,
international/expat framing, pub quiz/karaoke/escape room/pub crawl,
DJ practice, walks inside London, group exhibition visits, park hangouts
with no activity, public speaking clubs, life drawing, identity-specific
events for other communities, 30s-40s and over-40s age framing, "young
professionals" events, professional communities Alex isn't part of, every
sport except climbing, and the blocked organisers listed above.
