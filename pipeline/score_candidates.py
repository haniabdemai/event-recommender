#!/usr/bin/env python3
"""
Algorithmic event scoring: replaces LLM scoring for ~36 of 45 signals.

Usage:
    python3 score_candidates.py [--db PATH] [--dry-run]

Reads unscored candidates from SQLite, applies veto pre-filter + algorithmic
signal scoring, writes scores back. Outputs a list of Top Picks/Recommended candidates
for LLM sense-check review.

Signals that genuinely need LLM judgment (~6) are flagged but scored
conservatively (partial credit). The LLM is only called for blurb generation
on candidates that already passed algorithmic scoring.
"""

# Runnable as `python3 pipeline/<name>.py` or importable as a module:
# put the repo root (for erlib) and this package dir (for sibling
# modules) on sys.path before the repo imports below.
import sys as _sys
import pathlib as _pl
_r = _pl.Path(__file__).resolve().parent.parent
for _p in (str(_r / "pipeline"), str(_r)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import argparse
import functools
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from fetch_meetup import classify_format

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib import config  # noqa: E402
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.normalise import normalise_luma_url  # noqa: E402
OVERRIDES_PATH = SCRIPT_DIR / "scoring_overrides.json"

_EMPTY_OVERRIDES = {
    "known_organisers": [],
    "known_series_patterns": [],
}


def load_scoring_overrides(path: Path = OVERRIDES_PATH) -> dict:
    """Load user-approved scoring overrides from data file.

    Returns empty overrides on any error (missing file, bad JSON, wrong schema).
    Overrides only ADD to scoring sets: they never remove entries or change
    matching logic.
    """
    if not path.exists():
        return dict(_EMPTY_OVERRIDES)
    try:
        with open(path) as f:
            data = json.load(f)
        return {
            "known_organisers": list(data.get("known_organisers", [])),
            "known_series_patterns": list(data.get("known_series_patterns", [])),
        }
    except (json.JSONDecodeError, TypeError, ValueError, OSError) as e:
        print(f"WARNING: {path.name} malformed ({e}), using hardcoded scoring only",
              file=sys.stderr)
        return dict(_EMPTY_OVERRIDES)


def _test_load_overrides() -> int:
    """Verify override loading handles all edge cases. Run via --test-overrides."""
    import tempfile
    failures = 0

    def check(label, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL: {label}: got {got!r}, want {want!r}")
            failures += 1

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)  # noqa: SIM115  path must outlive handle
    json.dump({"version": 1, "known_organisers": ["test org"],
               "known_series_patterns": ["test series"]}, tmp)
    tmp.close()
    result = load_scoring_overrides(Path(tmp.name))
    check("valid file: organisers", result["known_organisers"], ["test org"])
    check("valid file: series", result["known_series_patterns"], ["test series"])
    os.unlink(tmp.name)

    result = load_scoring_overrides(Path("/nonexistent/path.json"))
    check("missing file: empty", result["known_organisers"], [])

    tmp2 = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)  # noqa: SIM115  path must outlive handle
    tmp2.write("{bad json")
    tmp2.close()
    result = load_scoring_overrides(Path(tmp2.name))
    check("malformed JSON: empty", result["known_organisers"], [])
    os.unlink(tmp2.name)

    tmp3 = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)  # noqa: SIM115  path must outlive handle
    json.dump({"version": 1}, tmp3)
    tmp3.close()
    result = load_scoring_overrides(Path(tmp3.name))
    check("missing keys: empty", result["known_organisers"], [])
    os.unlink(tmp3.name)

    if failures:
        print(f"{failures} override test(s) FAILED")
    else:
        print("Override loading tests: PASS")
    return failures


# ---------------------------------------------------------------------------
# Matcher: whole-word matching with declared scope
# ---------------------------------------------------------------------------
#
# Core principle: a keyword matches only as a whole word, in a declared scope.
# Raw substring matching (``kw in blob``) is banned in this file: it produced
# false positives such as "withdraw" firing the drawing signal and "space"
# firing wellness signals.
#
# Default scope for signals is the full text blob (name + description +
# organiser). Signals whose keywords are short or polysemous (e.g. WOMEN_LED)
# must use title_org_text(candidate) instead: description boilerplate
# (cancellation policies, venue directions, organiser bios) is too noisy for
# those to match reliably.
#
# Trailing spaces on existing keywords (e.g. "draw ", "build ") are a legacy
# of the old substring approach and are now redundant; _kw_re strips them.

@functools.cache
def _kw_re(kw: str) -> "re.Pattern[str]":
    """Compile a keyword to a case-insensitive \\b-bounded regex."""
    return re.compile(r"\b" + re.escape(kw.strip().lower()) + r"\b", re.IGNORECASE)


def matches_any(keywords, text: str) -> bool:
    """True if any keyword in ``keywords`` appears as a whole word in ``text``."""
    return any(_kw_re(kw).search(text) for kw in keywords)


def title_org_text(candidate) -> str:
    """Title + organiser only. Scope for signals whose keywords are polysemous
    or short enough to collide with description boilerplate."""
    parts = [candidate.get("name") or "", candidate.get("organiser") or ""]
    return " ".join(parts).lower()

# ---------------------------------------------------------------------------
# Veto pre-filter
# ---------------------------------------------------------------------------

VETO_PATTERNS = {
    "token_craft_workshop": [
        "candle making", "soap making", "wreath making", "macramé",
        "flower arranging", "pressed flowers", "terrarium",
    ],
    "networking_drinks": [
        "networking drinks", "drinks reception", "networking mixer",
        "networking mingle", "after work drinks", "professional networking",
    ],
    "corporate_tech": [
        "enterprise software", "enterprise solution", "b2b sales",
        "b2b platform", "b2b marketing", "b2b saas", "saas summit",
        "corporate innovation", "startup pitch", "investor pitch",
        "startup demo day", "investor demo day",
    ],
    "generic_social_dating": [
        "speed dating", "singles night", "dating event",
        "making friends", "find your tribe",
    ],
    "life_drawing": [
        "life drawing", "life sketching", "life painting",
        "live drawing", "live sketching", "live painting",
    ],
    "wellness_upsell": [
        "wellness journey", "transformational retreat",
        "transformational journey", "healing circle",
        "manifestation workshop", "manifestation circle",
        "law of attraction",
    ],
    "immersive_walkthrough": [
        "you are the character", "interactive theatre",
        "immersive dining",
    ],
    "classical_concert": [
        "symphony", "symphonic", "recital", "chamber music",
        "chamber orchestra", "string quartet", "piano trio",
        "concerto", "sonata", "oratorio", "requiem",
        "philharmonic", "sinfonia", "sinfonietta",
        "early music", "baroque ensemble", "period instruments",
    ],
    "language_learning": [
        "language exchange", "language learning", "language practice",
        "learn mandarin", "learn japanese", "learn korean", "learn chinese",
        "learn spanish", "learn french", "learn german", "learn italian",
        "speak mandarin", "speak japanese", "speak korean",
        "mandarin practice", "japanese practice", "korean practice",
        "language cafe", "language meetup",
    ],
    # "International/expat"-first framing: attracts the wrong crowd for the
    # example persona. A 20s-30s social that merely uses "global" passes to
    # the LLM instead (see letter I in SENSE_CHECK_INSTRUCTION).
    "expat_international": [
        "expat meetup", "expat social", "expat drinks", "expat community",
        "international professionals", "international crowd",
        "international meetup",
    ],
    "writing_groups": [
        "silent writing", "writing sprint", "write-in", "write club",
        "writers group", "writing group",
        "share your writing", "read your work",
        "book club", "read-a-long", "read along",
        # Exception: "learn to write" / "writing workshop" about skills = pass to LLM
    ],
    "tabletop_gaming": [
        # "dungeons and dragons" / "dungeons & dragons": keep both spellings:
        # event titles use either, and \b-matching won't bridge the ampersand.
        "warhammer", "40k", "heresy", "miniatures painting", "miniatures gaming",
        "dungeons and dragons", "dungeons & dragons", "d&d session", "d&d campaign", "dnd",
        "rpg night", "tabletop rpg", "tabletop gaming",
        "board game night", "board game cafe", "board game meetup",
        "board games", "gaming at the", "gaming afternoon",
        "gaming social", "backgammon", "mafia game",
    ],
    "technical_developer_training": [
        "node.js", "nodejs", "react.js", "reactjs", "vue.js",
        "microservices", "rest api", "graphql tutorial",
        "kubernetes", "docker workshop", "devops training",
        "system design interview", "coding interview prep",
        "backend development", "frontend development",
        "data engineering", "data pipeline",
        # Database/platform user groups and framework meetups: wrong crowd
        # (engineers grinding a syllabus, not the builders the example
        # persona is looking for).
        "clickhouse", "aws user group", "platform user group",
        "architecture kata", "android developers",
        "webflow meetup", "wordpress meetup", "crypto meetup",
        "postgres meetup", "terraform meetup", "golang meetup",
        "pytorch", "pydata", "gophers",
        "devops", "neo4j", "serverless",
        # Note: "hackathon", "vibe coding", "AI builders", "hackday" = POSITIVE
    ],
    "startup_investment": [
        "pitch night", "pitch competition", "demo day",
        "investor meetup", "vc meetup", "angel investor",
        "seeking funding", "fundraising", "startup funding",
        "founder networking", "startup networking",
        # Note: "founders building together" or "indie hackers" = pass to LLM
    ],
    "dating_events": [
        "singles night", "singles event", "singles party",
        "dating event", "matchmaking event",
        "find love", "meet your match",
        # Note: "speed dating" already in generic_social_dating
    ],
    # Taste profile: climbing and bouldering are the persona's sport. Every
    # other sport is a hard no. Wrong sports slipped through as Borderline
    # events that the LLM then sometimes rubber-stamped; veto them before
    # the LLM sees them.
    "wrong_sports": [
        "badminton", "football", "soccer", "5-a-side", "5 a side",
        "volleyball", "cricket", "rugby", "netball", "basketball",
        "tennis", "padel", "ping pong", "squash", "hockey",
        "darts",
        # Note: climbing/bouldering are WANTED signals: never add them here.
        # Bare "tennis" also catches "table tennis" as a whole-word match.
    ],
    # F1 / Grand Prix viewing parties: watching sport at a pub, not doing it.
    "sport_viewing": [
        "grand prix", "f1 canadian", "f1 miami", "f1 monaco",
        "f1 british", "formula 1", "f1:",
    ],
    # Philosophy / political / film discussion groups: same format as a book
    # club, just labelled differently: recurring meetups organised around
    # rotating discussion prompts.
    "discussion_group": [
        "philosophy discussion", "philosophical discussion",
        "philosophy meetup", "philosophy circle",
        "political debates", "debate night", "debate society", "open debate",
        "film club", "film discussion", "book discussion",
        "let's discuss",
    ],
    # Public-speaking drill formats: rhetoric practice clubs. Distinct from
    # one-off talks or ideas evenings.
    "public_speaking": [
        "public speaking", "speakers club", "speech practice",
        "oratory club",
    ],
    # Wrong age demographic: events for 40+/50+ age groups. Set to the
    # bands adjacent to yours; the 30s-40s band below is separate because
    # the LLM letter S only covers that framing.
    "wrong_age": [
        "over 40s", "over 50s", "over 60s",
        "45+", "50+", "60+",
    ],
    "wrong_age_30s_40s": [
        "30s & 40s", "30s and 40s", "30s-40s", "30s 40s",
        # Note: "20s & 30s", "20s-30s" are WANTED: do not add those here.
    ],
    "young_professionals": [
        "young professionals", "young professional",
    ],
    "pub_quiz": [
        "pub quiz", "trivia night", "quiz night",
    ],
    "karaoke": [
        "karaoke",
    ],
    "escape_room": [
        "escape room", "escape game",
    ],
    "pub_crawl": [
        "pub crawl", "bar crawl",
    ],
    "dj_practice": [
        "open decks", "dj workshop", "learn to dj", "beginner dj",
        "dj course", "dj training",
        # Note: "dj set", "dj" alone are NOT vetoed: they appear in
        # listening bar / music event contexts that are positive.
    ],
    "london_walks": [
        "walking tour", "guided walk", "thames walk", "thames path",
        "canal walk", "london walk", "haunted walk", "haunted walking",
        "evening walk", "evening stroll",
        # Note: "hike", "trek", "countryside" are NOT vetoed: hikes
        # outside London in nice scenery are still OK.
    ],
    # Organisers the user has explicitly blocked. These are fictional
    # examples for the example persona: the feedback digest proposes real
    # additions from your own attendance history.
    "blocked_organiser": [
        "the hype collective", "hype collective",
        "megamix socials", "gallery wander club",
    ],
}

# Classical crossover escape: if any of these appear alongside classical
# keywords, don't auto-veto; flag for LLM judgment instead
CLASSICAL_CROSSOVER_KEYWORDS = [
    "folk", "jazz", "film score", "electronic", "dj",
    "hip-hop", "rock", "dance", "world music", "improvisation",
]

# Vibe coding escape: if any of these appear alongside technical training
# keywords, don't auto-veto; the event is social/playful, not hardcore training.
VIBE_CODING_EXEMPTION_KEYWORDS = [
    "vibe coding", "vibe coder", "vibe coded",
]

TITLE_SCOPED_VETOES = {"tabletop_gaming", "wrong_sports"}

# Short abbreviations that must match as whole words (not substrings).
# "lso" would otherwise match "also", "bbc so" could match oddly, etc.
CLASSICAL_WORD_BOUNDARY_PATTERNS = [
    re.compile(r"\blso\b", re.IGNORECASE),
    re.compile(r"\bbbc\s+so\b", re.IGNORECASE),
    re.compile(r"\bbbc\s+symphony\b", re.IGNORECASE),
    re.compile(r"\blondon\s+symphony\b", re.IGNORECASE),
]

# Regex for "single" / "singles" as whole words in title or group name only
SINGLES_RE = re.compile(r"\bsingles?\b", re.IGNORECASE)

COST_VETO_THRESHOLD = 30  # £30

# ---------------------------------------------------------------------------
# Your Meetup groups: used by search layer Source 2A (priority fetch).
# Configured via ER_MEETUP_GROUPS (see erlib/config.py and .env.example).
# ---------------------------------------------------------------------------

MY_MEETUP_GROUPS = config.MEETUP_GROUPS

# ---------------------------------------------------------------------------
# Known organisers from past events (Signal #32, +2)
#
# Seeded by the setup interview and then maintained by the feedback digest:
# organisers whose events you attend earn a place here; organisers with
# zero attendance across many recommendations get pruned. The shipped
# entries are fictional examples matching the demo data and taste profile.
# ---------------------------------------------------------------------------

KNOWN_ORGANISERS = {
    "signal path collective",
    "the print room studio",
    "the boulder room",
    "analog sundays",
    "city makers guild",
    "vibe coding club",
}

# ---------------------------------------------------------------------------
# Known event series (Signal #31, +2): phrase-matched against the title.
# Same lifecycle as KNOWN_ORGANISERS; fictional examples shipped.
# ---------------------------------------------------------------------------

KNOWN_SERIES_PATTERNS = [
    "analog sundays", "makers night", "art jam",
    "vibe coding", "silent hike",
]

# ---------------------------------------------------------------------------
# Format mapping and repeat formats (Signal #30, +3)
# Formats with 3+ occurrences in past events after normalisation
# ---------------------------------------------------------------------------

FORMAT_NORMALISE = {
    "workshop / course": "Workshop",
    "meetup / networking": "Social meetup",
    "creative / social": "Creative social",
    "wellness / breathwork": "Wellness",
    "wellness / spa": "Wellness",
    "talk/panel": "Talk",
    "concert / festival": "Concert",
    "cultural visit / museum": "Museum visit",
    "museum": "Museum visit",
    "trial class (movement/dance?)": "Workshop",
    "outdoor / nature": "Outdoor",
    "book launch": "Book event",
}

# After normalisation: Workshop=14, Meetup/Social meetup=9+1=10, Wellness=7
REPEAT_FORMATS = {"Workshop", "Social meetup", "Wellness"}

# ---------------------------------------------------------------------------
# Signal keyword lists
# ---------------------------------------------------------------------------

AI_TECH_KEYWORDS = [
    "ai ", "artificial intelligence", "machine learning",
    "llm", "large language model", "gpt", "claude", "anthropic", "openai",
    "tech meetup", "hackathon", "hackday", "hacknight", "hack day", "hack night",
    "vibe coding", "live coding", "coding", "builder",
    "agentic", "ai agent", "autonomous agent",
    # Removed after a feedback audit: these fire on professional-tech
    # events the persona doesn't attend (entrepreneur networking, data
    # meetups, niche academic):
    # "startup", "founder", "developer", "software engineer", "engineering",
    # "api", "deep learning", "reinforcement learning", "neural network",
    # "computer vision", "nlp", "natural language", "data science", "blockchain"
]

HANDS_ON_KEYWORDS = [
    "workshop", "hands-on", "hands on", "build ", "building ",
    "create ", "creating ", "hack",
    "bring your laptop", "collaborative", "jam session",
    "make your own", "you'll make", "you will make",
    "make something", "get to make",
]

CREATIVE_KEYWORDS = [
    "painting", "paint ", "collage", "drawing", "draw ",
    "digital art", "sketch", "illustration", "printmaking",
    "pottery", "ceramics", "embroidery", "textile",
    "art workshop", "creative workshop",
]

MUSIC_PRODUCTION_KEYWORDS = [
    "music production", "music technology", "music tech",
    "beat making", "beatmaking", "songwriting",
    "audio gear", "audio equipment", "audio technology",
    "music gear", "music equipment",
    "synth ", "modular synth",
    "ableton", "logic pro", "fl studio",
    "daw ", "midi ", "audio interface",
    "music expo", "gearexpo", "gear expo",
]

BOOK_IDEAS_KEYWORDS = [
    "book launch", "book club", "book event",
    "politics", "political", "democracy", "society",
    "philosophy", "sociology", "activism",
]

BREATHWORK_KEYWORDS = [
    "breathwork", "sound healing", "sound bath", "gong bath",
    "sensory wellness", "sacred sound",
]

NATURE_KEYWORDS = [
    "garden", "botanical", "park ", "waterside", "canal",
    "outdoor", "nature walk", "urban farm", "greenhouse",
]

HERITAGE_KEYWORDS = [
    "heritage", "historic", "historical", "listed building",
    "victorian", "georgian", "brutalist", "art deco",
]

PASSIVE_FORMATS = {"Concert", "Film", "Exhibition", "Talk"}

POMPOUS_GALLERY_KEYWORDS = [
    "gallery show", "gallery exhibition", "fine art exhibition",
    "photography exhibition", "retrospective",
    "private view", "vernissage",
]

THOUGHT_LEADERSHIP_KEYWORDS = [
    "thought leadership", "keynote", "fireside chat",
    "panel discussion", "industry leaders",
    "executive briefing",
]

NETWORKING_NO_ACTIVITY = [
    "networking drinks", "drinks and networking",
    "mixer", "mingle", "after work drinks",
    "professional networking", "networking event",
]

UNKNOWN_MUSIC_KEYWORDS = [
    "lineup tba", "lineup tbc", "artists tba", "acts tba",
    "various artists", "live music", "dj set",
]

VIOLENCE_KEYWORDS = [
    "horror", "slasher", "gore", "violent", "torture",
    "murder mystery", "true crime",
]

CORPORATE_KEYWORDS = [
    "corporate", "enterprise", "b2b ", "saas ",
    "deloitte", "kpmg", "pwc", "accenture", "mckinsey",
    "sponsored by", "hosted by aws", "hosted by google cloud",
    "hosted by microsoft",
]

# The persona's one sport. Multi-word phrases keep "climbing" from
# false-firing on figurative uses ("social climbing", "climbing plants").
SPORT_KEYWORDS = [
    "bouldering", "climbing wall", "rock climbing", "climbing gym",
    "top rope", "climbing social",
]


# ---------------------------------------------------------------------------
# Core scoring function
# ---------------------------------------------------------------------------

def text_blob(candidate):
    """Combine name + description + organiser for keyword matching."""
    parts = [
        candidate.get("name") or "",
        candidate.get("description") or "",
        candidate.get("organiser") or "",
    ]
    return " ".join(parts).lower()


def parse_cost(cost_str):
    """Extract numeric cost from string like '£15', 'From £25', 'Free'.

    Thousands separators are part of the number ("£1,500" -> 1500.0, not
    1.0: audit P2: the old regex stopped at the comma, so four-figure
    retreats passed the £30 veto AND earned the free/cheap signal).
    Ranges take the first number ("£40–£60" -> 40.0).
    """
    if not cost_str:
        return None
    cost_str = cost_str.lower().strip()
    if cost_str in ("free", ""):
        return 0
    if cost_str in ("unknown", "tbc", "tba"):
        return None
    match = re.search(r"£?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", cost_str)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def parse_time(time_str):
    """Parse time string to hour (0-23). Returns None if unparseable."""
    if not time_str:
        return None
    match = re.match(r"(\d{1,2}):(\d{2})", str(time_str))
    if match:
        return int(match.group(1))
    return None


def check_veto(candidate):
    """
    Run Python veto pre-filter. Returns (vetoed: bool, reason: str|None).
    """
    blob = text_blob(candidate)

    # An exemption suppresses ONLY the veto it belongs to: later veto
    # categories must still run (audit P0-3: returning here let a vibe-coding
    # pub quiz bypass the pub_quiz veto entirely).
    for pattern_name, keywords in VETO_PATTERNS.items():
        search_text = title_org_text(candidate) if pattern_name in TITLE_SCOPED_VETOES else blob
        if matches_any(keywords, search_text):
            # Classical crossover exception
            if pattern_name == "classical_concert" and matches_any(CLASSICAL_CROSSOVER_KEYWORDS, blob):
                continue
            # Vibe coding exception: social/playful, not hardcore training
            if (pattern_name == "technical_developer_training"
                    and matches_any(VIBE_CODING_EXEMPTION_KEYWORDS, blob)):
                continue
            return True, pattern_name

    # Word-boundary checks for classical abbreviations (LSO, BBC SO, etc.)
    for pattern in CLASSICAL_WORD_BOUNDARY_PATTERNS:
        if pattern.search(blob):
            if matches_any(CLASSICAL_CROSSOVER_KEYWORDS, blob):
                break  # exempted: remaining veto checks below still apply
            return True, "classical_concert"

    # Singles veto: check title and group/organiser name only (not description)
    name_str = candidate.get("name") or ""
    org_str = candidate.get("organiser") or ""
    if SINGLES_RE.search(name_str) or SINGLES_RE.search(org_str):
        return True, "singles_in_title_or_group"

    # Cost veto
    cost = parse_cost(candidate.get("cost"))
    if cost is not None and cost > COST_VETO_THRESHOLD:
        return True, "cost_over_30"

    # Park hangout veto: parks 50+ min away by transit.
    # Park keywords in the name + high travel time = not worth the trip
    # for an unstructured outdoor social.
    PARK_KEYWORDS = ["in the park", "at the park", "picnic", "park social",
                     "park hangout", "sunset in the park", "park walk"]
    travel_min = candidate.get("travel_transit_min")
    if travel_min and travel_min > 50 and matches_any(PARK_KEYWORDS, name_str.lower()):
        return True, "park_hangout_far"

    return False, None


def score_candidate(candidate):
    """
    Score a candidate against all algorithmic signals.
    Returns (score, signals_fired, needs_llm_judgment).
    """
    blob = text_blob(candidate)
    name_lower = (candidate.get("name") or "").lower()
    org_lower = (candidate.get("organiser") or "").lower()
    venue_lower = (candidate.get("venue_name") or "").lower()
    format_type = candidate.get("format_type") or ""
    signals = []
    needs_llm = []

    # --- POSITIVE SIGNALS ---

    # #1 AI / tech / vibe coding community (+3)
    if matches_any(AI_TECH_KEYWORDS, blob):
        signals.append(("Signal #1: AI/tech community", 3))

    # #3 Listening bar / vinyl (+3): rare for events, keyword-matchable
    if matches_any(["listening bar", "vinyl", "record bar", "hi-fi bar"], blob):
        signals.append(("Signal #3: Listening bar/vinyl", 3))

    # #4 Hands-on (+2): "build" and "create" scoped to title only
    # to avoid false positives from description boilerplate ("build your network")
    hands_on_title = ["build ", "building ", "create ", "creating ", "hack"]
    hands_on_any = [kw for kw in HANDS_ON_KEYWORDS if kw not in
                    ["build ", "building ", "create ", "creating ", "hack"]]
    if matches_any(hands_on_any, blob) or matches_any(hands_on_title, name_lower):
        signals.append(("Signal #4: Hands-on", 2))

    # #5 Accessible creative activity (+2)
    if matches_any(CREATIVE_KEYWORDS, blob):
        signals.append(("Signal #5: Creative activity", 2))

    # #5b Music production / music technology (+2)
    if matches_any(MUSIC_PRODUCTION_KEYWORDS, blob):
        signals.append(("Signal #5b: Music production/tech", 2))

    # #6 Socialising through shared activity (+2)
    if format_type in ("Creative social", "Workshop", "Hackday"):
        signals.append(("Signal #6: Social through activity", 2))

    # #7 Specific artists the user follows (+2): small known list, seeded
    # by the setup interview (fictional examples shipped)
    known_artists = ["velvet arcade", "moon pilot", "the modular set"]
    if matches_any(known_artists, blob):
        signals.append(("Signal #7: Known artist", 2))

    # #8 (venue-specific signal): REMOVED after a feedback audit.
    # A favourite-venue signal sounds appealing but attendance data showed
    # the venue attracted events the user consistently rejected.

    # #9 Immersive / participatory art (+2): NEEDS LLM for "genuinely participatory" check
    immersive_kw = ["immersive", "installation", "sensory", "interactive art",
                    "participatory", "multisensory"]
    if matches_any(immersive_kw, blob):
        # Give partial credit (+1) algorithmically; LLM can upgrade to +2
        signals.append(("Signal #9: Immersive (partial, needs LLM)", 1))
        needs_llm.append("Signal #9: is this genuinely participatory immersive?")

    # #14 Climbing / bouldering (+2)
    if matches_any(SPORT_KEYWORDS, blob):
        signals.append(("Signal #14: Climbing/bouldering", 2))

    # #15 Organiser-demographic signal: REMOVED after a feedback audit.
    # Who runs an event is not a factor in event quality; attendance data
    # confirmed near-zero correlation.

    # #16 Free or under £15 (+1)
    cost = parse_cost(candidate.get("cost"))
    if cost is not None and cost <= 15:
        signals.append(("Signal #16: Free/cheap", 1))

    # #17 Evening slot: REMOVED after a feedback audit. Time of day is
    # not a factor; the only constraint is no events before 10am.
    hour = parse_time(candidate.get("time"))
    if hour is not None and hour < 10:
        signals.append(("Signal #17: Before 10am", -2))

    # #18 Afternoon slot: REMOVED (was already 0 weight).

    # #20 Book/ideas talk: REMOVED after a feedback audit. Whether a
    # book/ideas event is interesting is a contextual LLM judgment, not a
    # keyword signal.

    # #21 Breathwork / sound healing (+1)
    if matches_any(BREATHWORK_KEYWORDS, blob):
        signals.append(("Signal #21: Breathwork/sound healing", 1))

    # #22 Nature venue: REMOVED after a feedback audit. Being outdoors
    # doesn't make an event good.

    # #23 Structured dating: REMOVED 2026-07-05 (WP5, audit P2).
    # Dead/self-contradictory: every keyword it rewarded is also a hard veto
    # (speed dating, singles party, matchmaking), so candidates were vetoed
    # before this signal could ever fire.

    # #24 Heritage building: REMOVED after a feedback audit. A building
    # being old doesn't make an event good.

    # #30 Same format 3+ times (+1, reduced from +3 after a feedback
    # audit: at +3 it was the single biggest score inflator, pushing
    # generic recurring meetups to Recommended with zero content alignment).
    normalised = FORMAT_NORMALISE.get(format_type.lower(), format_type) if format_type else ""
    if normalised in REPEAT_FORMATS:
        signals.append(("Signal #30: Repeat format", 1))

    # #31 Same event series (+2): whole-word phrase match on the title only
    # (this file's matcher invariant; raw substring would let a series like
    # "art jam" fire inside an unrelated "smart jam"). Override series
    # strings from apply_findings get the same \b-bounded semantics.
    if matches_any(KNOWN_SERIES_PATTERNS, name_lower):
        signals.append(("Signal #31: Known series", 2))

    # #32 Same organiser (+2): deliberately EXACT match, unlike #31's phrase
    # match: KNOWN_ORGANISERS holds canonical full names from the taste data,
    # and loose matching on short organiser names ("ORA") would false-fire
    # inside unrelated text. A renamed/suffixed organiser simply re-earns
    # trust via the feedback digest.
    if org_lower and org_lower in KNOWN_ORGANISERS:
        signals.append(("Signal #32: Known organiser", 2))
    # Also check venue as proxy for organiser
    if venue_lower and venue_lower in KNOWN_ORGANISERS:
        signals.append(("Signal #32: Known venue-as-organiser", 2))

    # --- NEGATIVE SIGNALS ---

    # #33 Passive format (-1): don't double-penalise if already hands-on
    if format_type in PASSIVE_FORMATS and not any("Hands-on" in s[0] for s in signals):
        signals.append(("Signal #33: Passive format", -1))

    # #34 Over 45 min travel for routine event (-1): NEEDS LLM for "routine"
    travel = candidate.get("travel_transit_min")
    if travel and travel > 45:
        # Check if it's high-scoring (not routine)
        current_score = sum(w for _, w in signals)
        if current_score < 6:
            signals.append(("Signal #34: Far + routine", -1))
        else:
            needs_llm.append("Signal #34: far travel but high score: is this routine?")

    # #35 Pompous gallery (-1)
    if matches_any(POMPOUS_GALLERY_KEYWORDS, blob):
        signals.append(("Signal #35: Pompous gallery", -1))

    # #36 Thought leadership panel (-1): doesn't fire if hands-on element exists
    if (matches_any(THOUGHT_LEADERSHIP_KEYWORDS, blob)
            and not any("Hands-on" in s[0] for s in signals)):
        signals.append(("Signal #36: Thought leadership panel", -1))

    # #38 Networking drinks no activity (-2)
    if matches_any(NETWORKING_NO_ACTIVITY, blob) and not any("Social through activity" in s[0] for s in signals):
        signals.append(("Signal #38: Networking drinks", -2))

    # #39 Unknown music act (-2)
    if (format_type == "Concert" and matches_any(UNKNOWN_MUSIC_KEYWORDS, blob)
            and not any("Known artist" in s[0] for s in signals)):
        signals.append(("Signal #39: Unknown music act", -2))

    # #40 Heavy violence (-2)
    if matches_any(VIOLENCE_KEYWORDS, blob):
        signals.append(("Signal #40: Violence", -2))

    # #41 Corporate (-2)
    if matches_any(CORPORATE_KEYWORDS, blob):
        signals.append(("Signal #41: Corporate", -2))

    total_score = sum(w for _, w in signals)
    return total_score, signals, needs_llm


def assign_tier(score):
    """Assign preliminary tier based on score.

    The LLM sense-check reviews ALL non-vetoed candidates and may change
    these tiers. Borderline candidates get extra scrutiny.
    """
    if score >= 8:
        return "Top Picks"
    elif score >= 4:
        return "Recommended"
    else:
        # Low score but not vetoed: LLM will review and may change to Not Recommended
        return "Borderline"


# ---------------------------------------------------------------------------
# Second-pass format verification (regex patterns)
# ---------------------------------------------------------------------------
#
# classify_format() uses keyword matching against the title. These regex
# patterns catch Outdoor events that use route/distance language instead
# of walk-specific keywords. Only applied to Social meetup candidates
# (the default fallback). Runs at score time, before scoring signals fire.

_MILES_IN_TITLE = re.compile(r'\b\d+\s*miles?\b', re.IGNORECASE)
_CIRCULAR_IN_TITLE = re.compile(r'\bcircular\b', re.IGNORECASE)


def verify_format(candidate):
    """Second-pass format check for Social meetup candidates only."""
    if candidate.get("format_type") != "Social meetup":
        return candidate.get("format_type") or "Social meetup"

    title = candidate.get("name") or ""

    if _MILES_IN_TITLE.search(title):
        return "Outdoor"

    if _CIRCULAR_IN_TITLE.search(title):
        return "Outdoor"

    return "Social meetup"


_FORMAT_VERIFY_TESTS = [
    {"title": "Harewood Bluebells (14 or 11 miles)", "expect": "Outdoor"},
    {"title": "Beacon Point - (16 or 8 miles)", "expect": "Outdoor"},
    {"title": "Riverbend to Oakhollow via Beacon Tower (6 miles)", "expect": "Outdoor"},
    {"title": "Station Loop via Old Arches Market, 4 miles", "expect": "Outdoor"},
    {"title": "Millbrook circular walk – delights of the high downs", "expect": "Outdoor"},
    {"title": "Sussex Circular: An Ancient Tree", "expect": "Outdoor"},
    {"title": "Haslemere Circular", "expect": "Outdoor"},
    # Guards: must stay Social meetup
    {"title": "Friday drinks in Soho", "expect": "Social meetup"},
    {"title": "Startup Founder Showcase", "expect": "Social meetup"},
    {"title": "AI x Weather x Climate Demo Night", "expect": "Social meetup"},
    {"title": "Farnham to Frensham", "expect": "Social meetup"},
]


def _run_format_verify_test():
    failures = []
    for case in _FORMAT_VERIFY_TESTS:
        result = verify_format({"name": case["title"], "format_type": "Social meetup"})
        if result != case["expect"]:
            failures.append(
                f"  {case['title']!r} -> {result!r} (expected {case['expect']!r})"
            )
    if failures:
        print("FORMAT VERIFY SELF-TEST FAILED:", file=sys.stderr)
        for f in failures:
            print(f, file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Self-test: runs on every invocation before scoring
# ---------------------------------------------------------------------------
#
# Small regression corpus. Each case pins down known failure modes of the
# pre-\b-matcher implementation or core recall cases that must keep working.
# Adding a new keyword that regresses one of these fails the script at
# startup: not silently in Notion a week later.

_SELF_TEST_CASES = [
    # Regression: "withdraw" (cancellation policy) and "ladies' doubles"
    # (league blurb) must not fire creative/women-led on a badminton club.
    {
        "id": "badminton_withdraw_ladies_in_description",
        "candidate": {
            "name": "Riverside Smash Badminton Club",
            "organiser": "Riverside Smash",
            "description": (
                "Weekly badminton at the local sports centre. "
                "Cancellation: you may withdraw up to 24 hours before. "
                "We run ladies' doubles leagues monthly."
            ),
            "cost": "£5",
            "time": "19:00",
            "format_type": "Social meetup",
        },
        "forbidden_signals": [
            "Signal #5: Creative activity",
        ],
    },
    # Regression: "decoding" must not fire AI via "coding".
    {
        "id": "decoding_shakespeare_not_ai",
        "candidate": {
            "name": "Decoding Shakespeare",
            "organiser": "English Literature Society",
            "description": "A lecture on decoding the metaphors in Shakespeare.",
            "cost": "£10",
        },
        "forbidden_signals": ["Signal #1: AI/tech community"],
    },
    # Regression: "Hackney" venue must not fire hands-on via "hack".
    {
        "id": "hackney_wick_not_hands_on",
        "candidate": {
            "name": "Poetry reading",
            "organiser": "Riverside Studios",
            "description": "An evening of new poetry at our Hackney Wick venue.",
            "cost": "£8",
        },
        "forbidden_signals": ["Signal #4: Hands-on"],
    },
    # Regression: "space" must not fire anything that looked for "spa".
    # (Covers the general class; no current signal uses bare "spa", but this
    # pins the invariant for future additions.)
    {
        "id": "coworking_space_clean",
        "candidate": {
            "name": "Coworking Space Drop-In",
            "organiser": "Space Coworking",
            "description": "Drop in to use our shared space.",
            "cost": "Free",
        },
        "forbidden_signals": [
            "Signal #21: Breathwork/sound healing",
        ],
    },
    # Recall: a real drawing workshop still fires creative + hands-on.
    {
        "id": "real_drawing_workshop",
        "candidate": {
            "name": "Drawing Workshop",
            "organiser": "The Print Room Studio",
            "description": "Bring your sketchbook for a guided drawing session.",
            "cost": "£12",
            "time": "19:00",
        },
        "required_signals": [
            "Signal #5: Creative activity",
            "Signal #4: Hands-on",
        ],
    },
    # Recall: a real AI meetup still fires.
    {
        "id": "real_ai_meetup",
        "candidate": {
            "name": "AI Builders Meetup",
            "organiser": "AI Builders Club",
            "description": "Monthly gathering for AI engineers and builders.",
            "cost": "Free",
            "time": "19:00",
        },
        "required_signals": ["Signal #1: AI/tech community"],
    },
    # Scope: women-led in title fires; same keyword in description only does not.
    # This test verifies a hiking club is not vetoed (it is not a London
    # walk) and that no spurious signals fire.
    {
        "id": "women_hiking_club_not_vetoed",
        "candidate": {
            "name": "Women Only Hiking Club",
            "organiser": "Trail Women Collective",
            "description": "A weekly hike.",
            "cost": "Free",
        },
        "forbidden_signals": [],
    },

    # Regression: null/empty description must not fire any description-based
    # signals. The main() loop handles these as "Couldn't Process": this pins
    # that score_candidate() produces no spurious signals from an empty text blob.
    {
        "id": "empty_description_no_signals",
        "candidate": {
            "name": "Monthly Community Gathering",
            "organiser": "Local Community Group",
            "description": "",
            "cost": "£20",
            "time": "14:00",
            "format_type": "",
        },
        "max_score": 0,
    },
    # --- veto coverage ---
    # Wrong sport: badminton in title must be vetoed (even if other signals would fire).
    {
        "id": "wrong_sport_badminton_vetoed",
        "candidate": {
            "name": "Casual Badminton Session, beginners welcome",
            "organiser": "London Badminton Meetup",
            "description": "Drop in for a friendly game, £7, all levels welcome.",
            "cost": "£7",
            "time": "19:00",
        },
        "expect_veto": "wrong_sports",
    },
    # Climbing must NOT be vetoed: it's in SPORT_KEYWORDS (positive #14).
    {
        "id": "climbing_signal_fires",
        "candidate": {
            "name": "Beginner Bouldering Social",
            "organiser": "The Boulder Room",
            "description": "Coached intro to bouldering, then a social.",
            "cost": "£15",
            "time": "19:00",
        },
        "required_signals": ["Signal #14: Climbing/bouldering"],
    },
    # Tennis is now a wrong sport for the example persona.
    {
        "id": "tennis_vetoed",
        "candidate": {
            "name": "Tennis for All: beginners and intermediates",
            "organiser": "City Tennis Club",
            "description": "Coaching-led tennis session for all levels.",
            "cost": "£15",
            "time": "19:00",
        },
        "expect_veto": "wrong_sports",
    },
    # Bare "tennis" also catches table tennis as a whole-word match.
    {
        "id": "table_tennis_vetoed",
        "candidate": {
            "name": "Table Tennis Social Night",
            "organiser": "PingPong Club",
            "description": "Casual table tennis at the pub.",
            "cost": "£5",
        },
        "expect_veto": "wrong_sports",
    },
    # Silent-writing sprint formats fire the writing-groups veto.
    {
        "id": "silent_writing_vetoed",
        "candidate": {
            "name": "Silent Writing Club at the Coffee House",
            "organiser": "Silent Writing London",
            "description": "Silent writing sessions for writers of all levels.",
            "cost": "Free",
        },
        "expect_veto": "writing_groups",
    },
    # Discussion group: philosophy circle: same format as a book club.
    {
        "id": "philosophy_circle_vetoed",
        "candidate": {
            "name": "Philosophy Circle #139",
            "organiser": "The Philosophy Circle",
            "description": "A weekly philosophical discussion open to all.",
            "cost": "Free",
        },
        "expect_veto": "discussion_group",
    },
    # Speakers club: rhetoric drill format.
    {
        "id": "speakers_club_vetoed",
        "candidate": {
            "name": "Free Public Speaking: Beginners to Advanced – City Speakers Club",
            "organiser": "City Speakers Club",
            "description": "Practise public speaking in a supportive group.",
            "cost": "Free",
        },
        "expect_veto": "public_speaking",
    },
    # Database user group: close to the ClickHouse/AWS/LOPUG events that slipped through.
    {
        "id": "clickhouse_user_group_vetoed",
        "candidate": {
            "name": "ClickHouse London Meetup",
            "organiser": "ClickHouse",
            "description": "Monthly meetup for data engineers using ClickHouse.",
            "cost": "Free",
        },
        "expect_veto": "technical_developer_training",
    },
    # F1 viewing party: wrong-sport spectating.
    {
        "id": "f1_grand_prix_vetoed",
        "candidate": {
            "name": "F1: Canadian Grand Prix 2026",
            "organiser": "London Formula 1 Fans",
            "description": "Casual get-together to watch the F1 race at Redwood.",
            "cost": "Free",
        },
        "expect_veto": "sport_viewing",
    },
    # Vibe coding / AI builders must NOT be vetoed even though tech keywords appear.
    # This pins the "technical training" vs "social/playful builder meetup" distinction
    # that the taste profile calls out explicitly.
    {
        "id": "vibe_coding_not_vetoed",
        "candidate": {
            "name": "Vibe Coding Club | Build an AI Agent Together",
            "organiser": "Vibe Coding Club",
            "description": "Hands-on AI agent building with Notion, small groups.",
            "cost": "Free",
            "time": "18:00",
        },
        "required_signals": ["Signal #1: AI/tech community"],
    },
    # Expat/international-first framing is a keyword veto.
    {
        "id": "expat_drinks_vetoed",
        "candidate": {
            "name": "International Professionals Thursday Drinks",
            "organiser": "Expat Social City",
            "description": "Weekly expat drinks for the international crowd.",
            "cost": "Free",
        },
        "expect_veto": "expat_international",
    },
    # "global" on a 20s-30s social must NOT trip the expat veto.
    {
        "id": "global_20s30s_not_vetoed",
        "candidate": {
            "name": "20s-30s Global Socialising Night",
            "organiser": "City Social Club",
            "description": "Casual social night for people in their 20s and 30s.",
            "cost": "Free",
        },
        "required_signals": ["Signal #16: Free/cheap"],
    },
    # Inclusive language is welcoming, not targeting: identity fit is the
    # LLM's call (letter J), so no Python veto may fire here.
    {
        "id": "inclusive_social_not_vetoed",
        "candidate": {
            "name": "LGBT-friendly Pottery Social",
            "organiser": "Clay Together",
            "description": "An inclusive pottery evening, all welcome.",
            "cost": "£10",
        },
        "required_signals": ["Signal #5: Creative activity"],
    },
    # --- feedback-audit veto additions ---
    {
        "id": "pub_quiz_vetoed",
        "candidate": {
            "name": "FREE Chaotic PUB QUIZ (TUESDAYS)",
            "organiser": "The Quiz Masters",
            "description": "Weekly pub quiz in Shoreditch. Teams of 4-6.",
            "cost": "Free",
        },
        "expect_veto": "pub_quiz",
    },
    # Regression (audit P0-3): an exemption suppresses ONLY its own veto.
    # Vibe coding exempts technical_developer_training, but the pub quiz
    # veto must still fire on the same event.
    {
        "id": "vibe_coding_pub_quiz_still_vetoed",
        "candidate": {
            "name": "Vibe Coding Pub Quiz Night",
            "organiser": "Tech Socials London",
            "description": (
                "A vibe coding pub quiz about Node.js: teams answer "
                "trivia rounds over drinks."
            ),
            "cost": "Free",
        },
        "expect_veto": "pub_quiz",
    },
    {
        "id": "karaoke_vetoed",
        "candidate": {
            "name": "Music Trivia & Karaoke Mixer",
            "organiser": "London Social Events",
            "description": "Karaoke and music trivia at a Soho bar.",
            "cost": "Free",
        },
        "expect_veto": "karaoke",
    },
    {
        "id": "darts_vetoed",
        "candidate": {
            "name": "Social Darts Night | Meet 20 People",
            "organiser": "Bullseye Socials",
            "description": "Social darts with speed-round rotations.",
            "cost": "£3",
        },
        "expect_veto": "wrong_sports",
    },
    {
        "id": "escape_room_vetoed",
        "candidate": {
            "name": "Let's Do an Escape Room @ Escape Plan",
            "organiser": "London Adventures",
            "description": "Collaborative escape room challenge.",
            "cost": "£25",
        },
        "expect_veto": "escape_room",
    },
    {
        "id": "pub_crawl_vetoed",
        "candidate": {
            "name": "Upper Street Pub Crawl - Dancing Later",
            "organiser": "London Pub Crawls",
            "description": "Hit 5 pubs in Islington then dancing.",
            "cost": "Free",
        },
        "expect_veto": "pub_crawl",
    },
    {
        "id": "dj_workshop_vetoed",
        "candidate": {
            "name": "Beginners DJ Workshop",
            "organiser": "Deck School London",
            "description": "Learn to DJ in a friendly beginner session.",
            "cost": "Free",
        },
        "expect_veto": "dj_practice",
    },
    # DJ set at a bar must NOT fire the dj_practice veto.
    {
        "id": "dj_set_not_vetoed",
        "candidate": {
            "name": "Friday Night DJ Set at The Listening Room",
            "organiser": "The Listening Room",
            "description": "Resident DJ spinning vinyl all night.",
            "cost": "Free",
        },
        "forbidden_signals": [],
    },
    {
        "id": "london_walking_tour_vetoed",
        "candidate": {
            "name": "Lost London - Stunning Walking Tour of the Thames",
            "organiser": "London Walks Group",
            "description": "A beautiful scenic 5.5 mile walk along the Thames.",
            "cost": "Free",
        },
        "expect_veto": "london_walks",
    },
    # Country hike must NOT fire the london_walks veto.
    {
        "id": "country_hike_not_vetoed",
        "candidate": {
            "name": "Hike: Arundel via River Arun & Amberley",
            "organiser": "GO London Hiking",
            "description": "Beautiful countryside hike through the South Downs.",
            "cost": "Free",
        },
        "forbidden_signals": [],
    },
    {
        "id": "30s_40s_vetoed",
        "candidate": {
            "name": "New Circle Social (30s & 40s)",
            "organiser": "City Social Circle - 30s & 40s",
            "description": "Monthly social for people in their 30s and 40s.",
            "cost": "Free",
        },
        "expect_veto": "wrong_age_30s_40s",
    },
    # 20s-30s must NOT fire the 30s-40s veto.
    {
        "id": "20s_30s_not_vetoed",
        "candidate": {
            "name": "Friday Social 20s-30s @ London City",
            "organiser": "London 20s 30s Meetup",
            "description": "Casual Friday drinks for 20s and 30s.",
            "cost": "Free",
        },
        "forbidden_signals": [],
    },
    {
        "id": "young_professionals_vetoed",
        "candidate": {
            "name": "Evening Social for Young Professionals",
            "organiser": "YP Network London",
            "description": "Monthly meetup for young professionals in London.",
            "cost": "Free",
        },
        "expect_veto": "young_professionals",
    },
    {
        "id": "padel_vetoed_not_tennis",
        "candidate": {
            "name": "Beginner Padel Session - All Welcome",
            "organiser": "London Padel Club",
            "description": "Friendly padel session for all levels.",
            "cost": "£15",
        },
        "expect_veto": "wrong_sports",
    },
    {
        "id": "blocked_organiser_hype_collective",
        "candidate": {
            "name": "Live Music Night at The Castle",
            "organiser": "The Hype Collective",
            "description": "Live band night with social drinks.",
            "cost": "Free",
        },
        "expect_veto": "blocked_organiser",
    },
    {
        "id": "canal_walk_vetoed",
        "candidate": {
            "name": "London Society WALK - Regents Canal",
            "organiser": "London Society of Explorers",
            "description": "Canal walk from Angel to Mile End.",
            "cost": "Free",
        },
        "expect_veto": "london_walks",
    },
    # Vibe coding events that happen to use specific technologies must NOT be
    # vetoed by technical_developer_training. "node.js" appears in the title
    # as "AI-Node.js" but the event is a vibe coding session.
    {
        "id": "vibe_coding_with_nodejs_not_vetoed",
        "candidate": {
            "name": "🔥🚀 Microservice Development with AI-Node.js - (Vibe Coding) - In Class",
            "organiser": "Code & Chill",
            "description": "Learn to build microservices using AI tools. Hands-on vibe coding session.",
            "cost": "Free",
            "time": "18:00",
        },
        "required_signals": ["Signal #1: AI/tech community"],
    },
    # Pure Node.js training without vibe coding context must STILL be vetoed.
    {
        "id": "pure_nodejs_training_vetoed",
        "candidate": {
            "name": "Node.js Backend Development Workshop",
            "organiser": "London Node User Group",
            "description": "Deep dive into Node.js streams, clustering, and deployment.",
            "cost": "£20",
        },
        "expect_veto": "technical_developer_training",
    },
    # Park social where board games and volleyball are listed as activities among
    # several: NOT a board game or sport event. Keywords appear only in the
    # description, not the title. TITLE_SCOPED_VETOES suppresses the veto; the
    # LLM Veto G multi-activity clarification handles judgment.
    # 29/282 events bypass Python veto (17 tabletop + 12 sports);
    # LLM Veto A/G provides defence in depth for genuine sport/game events.
    {
        "id": "park_social_with_board_games_not_vetoed",
        "candidate": {
            "name": "Park Social 🌳 - Expand Your Circle",
            "organiser": "Park Circle",
            "description": (
                "A relaxed afternoon of board games, volleyball, team challenges, "
                "and easy conversations with a group of friendly people."
            ),
            "cost": "Free",
            "time": "15:00",
            "format_type": "Social meetup",
        },
        "required_signals": ["Signal #16: Free/cheap"],
    },
    # Actual board game event (title says board games): must STILL be vetoed
    # even with TITLE_SCOPED_VETOES active, because the keyword is in the title.
    {
        "id": "board_game_night_vetoed",
        "candidate": {
            "name": "Board Game Night at the Pub",
            "organiser": "London Board Game Club",
            "description": "Bring your favourites or try ours. All welcome!",
            "cost": "Free",
        },
        "expect_veto": "tabletop_gaming",
    },
    # Board game cafe (title): must still be vetoed.
    {
        "id": "board_game_cafe_vetoed",
        "candidate": {
            "name": "Board Game Cafe Social",
            "organiser": "Dice Corner Cafe",
            "description": "Evening at the board game cafe with unlimited games.",
            "cost": "£12",
        },
        "expect_veto": "tabletop_gaming",
    },
    # parse_cost regressions (WP5 task 5.1, audit P2): "£1,500" used to parse
    # as £1: passing the £30 cost veto AND earning the free/cheap signal.
    {
        "id": "thousands_cost_vetoed",
        "candidate": {
            "name": "Luxury Wellness Retreat Weekend",
            "organiser": "Retreat Collective",
            "description": "A restorative weekend away.",
            "cost": "£1,500",
        },
        "expect_veto": "cost_over_30",
    },
    {
        "id": "cost_range_uses_first_number",
        "candidate": {
            "name": "Pottery Wheel Session",
            "organiser": "Clay Studio",
            "description": "Beginner pottery wheel session.",
            "cost": "£10–£20",
        },
        "required_signals": ["Signal #16: Free/cheap"],
    },
    # £15.50 must stay 15.5 (> £15), not truncate to 15 and fire free/cheap.
    {
        "id": "decimal_cost_preserved",
        "candidate": {
            "name": "Evening Pottery Session",
            "organiser": "Clay Studio",
            "description": "Pottery session with materials included.",
            "cost": "£15.50",
        },
        "forbidden_signals": ["Signal #16: Free/cheap"],
    },
    # Signal #31 must whole-word match: "art jam" inside "Smart Jam" is a
    # substring hit but not a word match.
    {
        "id": "known_series_whole_word_only",
        "candidate": {
            "name": "Smart Jam Night",
            "organiser": "Data Events Co",
            "description": "An evening of smart-city lightning talks.",
            "cost": "Free",
        },
        "forbidden_signals": ["Signal #31: Known series"],
    },
    {
        "id": "known_series_still_fires",
        "candidate": {
            "name": "Art Jam: Collage Edition",
            "organiser": "Art Jam Studio",
            "description": "Drop-in collage jam, materials provided.",
            "cost": "£10",
        },
        "required_signals": ["Signal #31: Known series"],
    },
    # --- WP6 task 6.2: negative signals #33-#41 + their label-string guards ---
    # The guards suppress a negative signal by substring-matching a prior
    # signal's DISPLAY LABEL; renaming a label silently breaks the guard.
    # These pairs turn that failure mode into a test failure.
    {
        "id": "passive_format_fires",
        "candidate": {
            "name": "An Evening Lecture on Bridges",
            "organiser": "Civic Society",
            "description": "A lecture about London bridges.",
            "cost": "Free", "format_type": "Talk",
        },
        "required_signals": ["Signal #33: Passive format"],
    },
    {
        "id": "passive_format_guard_hands_on",
        "candidate": {
            "name": "Talk and Make: Bridges",
            "organiser": "Civic Society",
            "description": "Short talk, then a hands-on model building workshop.",
            "cost": "Free", "format_type": "Talk",
        },
        "forbidden_signals": ["Signal #33: Passive format"],
    },
    {
        "id": "far_routine_penalty",
        "candidate": {
            "name": "Community Coffee Morning",
            "organiser": "Neighbours Group",
            "description": "A regular coffee morning.",
            "cost": "Free", "travel_transit_min": 60,
        },
        "required_signals": ["Signal #34: Far + routine"],
    },
    {
        "id": "pompous_gallery_penalty",
        "candidate": {
            "name": "Retrospective: Four Decades of Prints",
            "organiser": "Fine Arts Society",
            "description": "A private view of the retrospective.",
            "cost": "Free",
        },
        "required_signals": ["Signal #35: Pompous gallery"],
    },
    {
        "id": "thought_leadership_penalty",
        "candidate": {
            "name": "Fireside Chat: The Future of Retail",
            "organiser": "Business Forum",
            "description": "A fireside chat with industry leaders.",
            "cost": "Free",
        },
        "required_signals": ["Signal #36: Thought leadership panel"],
    },
    {
        "id": "thought_leadership_guard_hands_on",
        "candidate": {
            "name": "Fireside Chat and Workshop",
            "organiser": "Makers Forum",
            "description": "A fireside chat followed by a hands-on workshop.",
            "cost": "Free",
        },
        "forbidden_signals": ["Signal #36: Thought leadership panel"],
    },
    {
        "id": "networking_drinks_penalty",
        "candidate": {
            "name": "Drinks and Networking Evening",
            "organiser": "City Circle",
            "description": "Drinks and networking at a rooftop bar.",
            "cost": "Free",
        },
        "required_signals": ["Signal #38: Networking drinks"],
    },
    {
        "id": "networking_drinks_guard_activity",
        "candidate": {
            "name": "Pottery Mixer",
            "organiser": "Clay Social Club",
            "description": "A mixer where everyone throws a pot together.",
            "cost": "Free", "format_type": "Creative social",
        },
        "forbidden_signals": ["Signal #38: Networking drinks"],
    },
    {
        "id": "unknown_music_act_penalty",
        "candidate": {
            "name": "Friday Live Sessions",
            "organiser": "The Basement",
            "description": "Live music, lineup TBA.",
            "cost": "£10", "format_type": "Concert",
        },
        "required_signals": ["Signal #39: Unknown music act"],
    },
    {
        "id": "unknown_music_act_guard_known_artist",
        "candidate": {
            "name": "Velvet Arcade: Live in London",
            "organiser": "The Basement",
            "description": "Live music from Velvet Arcade, support lineup TBA.",
            "cost": "£25", "format_type": "Concert",
        },
        "forbidden_signals": ["Signal #39: Unknown music act"],
    },
    {
        "id": "violence_penalty",
        "candidate": {
            "name": "True Crime Evening",
            "organiser": "Story Club",
            "description": "An evening of true crime stories.",
            "cost": "Free",
        },
        "required_signals": ["Signal #40: Violence"],
    },
    {
        "id": "corporate_penalty",
        "candidate": {
            "name": "Innovation Breakfast",
            "organiser": "City Events",
            "description": "A breakfast sponsored by Deloitte.",
            "cost": "Free",
        },
        "required_signals": ["Signal #41: Corporate"],
    },
    # --- WP6 task 6.2: check_veto non-keyword branches ---
    {
        "id": "singles_in_org_vetoed",
        "candidate": {
            "name": "Thames Boat Evening",
            "organiser": "London Singles Club",
            "description": "An evening cruise on the Thames.",
            "cost": "£20",
        },
        "expect_veto": "singles_in_title_or_group",
    },
    # "singles" in the DESCRIPTION only must not veto (title/org scope).
    {
        "id": "singles_in_description_not_vetoed",
        "candidate": {
            "name": "Sunday Riverside Social",
            "organiser": "London Friends",
            "description": "Popular with singles and couples alike.",
            "cost": "Free",
        },
        "required_signals": ["Signal #16: Free/cheap"],
    },
    {
        "id": "park_hangout_far_vetoed",
        "candidate": {
            "name": "Sunset Picnic in the Park",
            "organiser": "Chill London",
            "description": "Bring a blanket.",
            "cost": "Free", "travel_transit_min": 60,
        },
        "expect_veto": "park_hangout_far",
    },
    {
        "id": "park_hangout_near_not_vetoed",
        "candidate": {
            "name": "Sunset Picnic in the Park",
            "organiser": "Chill London",
            "description": "Bring a blanket.",
            "cost": "Free", "travel_transit_min": 20,
        },
        "required_signals": ["Signal #16: Free/cheap"],
    },
    {
        "id": "lso_word_boundary_vetoed",
        "candidate": {
            "name": "An Evening with the LSO",
            "organiser": "Concert Hall",
            "description": "Orchestral favourites.",
            "cost": "£30",
        },
        "expect_veto": "classical_concert",
    },
    # "also" contains "lso" as a substring: word boundary must hold.
    {
        "id": "also_not_classical",
        "candidate": {
            "name": "Beginner Pottery Session",
            "organiser": "Clay Studio",
            "description": "We also welcome complete beginners.",
            "cost": "Free",
        },
        "required_signals": ["Signal #16: Free/cheap"],
    },
    # Classical crossover exemption: jazz reframes the LSO match, and later
    # vetoes must still run after the exemption (mirrors the vibe-coding fix).
    {
        "id": "classical_crossover_exempted",
        "candidate": {
            "name": "LSO Jazz Crossover Night",
            "organiser": "Concert Hall",
            "description": "The orchestra meets a live jazz quartet.",
            "cost": "Free",
        },
        "required_signals": ["Signal #16: Free/cheap"],
    },
    {
        "id": "classical_crossover_later_veto_still_runs",
        "candidate": {
            "name": "LSO Jazz Crossover Pub Quiz",
            "organiser": "Concert Hall",
            "description": "Jazz-themed pub quiz with the orchestra.",
            "cost": "Free",
        },
        "expect_veto": "pub_quiz",
    },
]


def _run_self_test() -> None:
    failures = []
    for case in _SELF_TEST_CASES:
        c = dict(case["candidate"])
        vetoed, veto_reason = check_veto(c)
        if vetoed:
            signals_fired = []
            score = 0
        else:
            score, signals, _ = score_candidate(c)
            signals_fired = [s[0] for s in signals]

        if "expect_veto" in case:
            if not vetoed:
                failures.append(
                    f"[{case['id']}] expected veto {case['expect_veto']!r} "
                    f"but none fired (signals: {signals_fired})"
                )
            elif veto_reason != case["expect_veto"]:
                failures.append(
                    f"[{case['id']}] expected veto {case['expect_veto']!r} "
                    f"but got {veto_reason!r}"
                )

        for forbidden in case.get("forbidden_signals", []):
            if any(forbidden in s for s in signals_fired):
                failures.append(
                    f"[{case['id']}] forbidden signal fired: {forbidden!r}"
                )
        for required in case.get("required_signals", []):
            if vetoed:
                failures.append(
                    f"[{case['id']}] required signal {required!r} can't fire: "
                    f"candidate was vetoed ({veto_reason})"
                )
            elif not any(required in s for s in signals_fired):
                failures.append(
                    f"[{case['id']}] required signal missing: {required!r} "
                    f"(got: {signals_fired})"
                )
        if "max_score" in case and not vetoed and score > case["max_score"]:
            failures.append(
                f"[{case['id']}] score {score} exceeds max {case['max_score']} "
                f"(signals: {signals_fired})"
            )

    if failures:
        print("SELF-TEST FAILED: scoring-layer invariants broken:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nSee CLAUDE.md §Keyword matcher principle before changing keywords "
            "or the matcher.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Smoke tests (DB integration)
# ---------------------------------------------------------------------------

def _smoke_tests() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    # assign_tier thresholds (WP6 6.2): 8+ Top Picks, 4-7 Recommended,
    # everything else Borderline for LLM review.
    check("assign_tier 9 -> Top Picks", assign_tier(9), "Top Picks")
    check("assign_tier 8 -> Top Picks (boundary)", assign_tier(8), "Top Picks")
    check("assign_tier 7 -> Recommended", assign_tier(7), "Recommended")
    check("assign_tier 4 -> Recommended (boundary)", assign_tier(4), "Recommended")
    check("assign_tier 3 -> Borderline", assign_tier(3), "Borderline")
    check("assign_tier 0 -> Borderline", assign_tier(0), "Borderline")
    check("assign_tier -2 -> Borderline", assign_tier(-2), "Borderline")

    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115  path must outlive handle
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY, name TEXT, description TEXT, source TEXT,
        url TEXT, date TEXT, end_date TEXT, venue_name TEXT, venue_postcode TEXT,
        organiser TEXT, cost TEXT, format_type TEXT, run_date TEXT,
        score INTEGER, tier TEXT, signals_fired TEXT, veto_reason TEXT,
        llm_tier TEXT, llm_reasoning TEXT, llm_reviewed TEXT,
        pipeline_state TEXT DEFAULT 'pending_llm',
        travel_display TEXT, travel_lookup_failed INTEGER DEFAULT 0,
        needs_enrichment INTEGER DEFAULT 0, source_snapshot TEXT,
        notion_page_id TEXT, notion_status TEXT,
        past_pattern_match TEXT, place_written INTEGER DEFAULT 0,
        user_attended INTEGER DEFAULT 0
    )""")
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, llm_reviewed, llm_tier)
        VALUES (9001, 'Already reviewed no-desc', '', 'Newsletter', '2026-12-01',
                'llm_rejected', '2026-06-02T21:55:07', 'Not Recommended')""")
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state)
        VALUES (9002, 'New no-desc', '', 'Newsletter', '2026-12-01',
                'pending_llm')""")
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state)
        VALUES (9003, 'Has description', 'A real event about art', 'Newsletter', '2026-12-01',
                'pending_llm')""")
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, notion_page_id)
        VALUES (9004, 'Written event rescored', 'Art exhibition in London', 'Newsletter', '2026-12-01',
                'written', 'fake-page-id-for-test')""")
    # Past-date candidate: should be excluded from --rescore
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, score)
        VALUES (9005, 'Past event', 'A past art exhibition', 'Newsletter', '2025-01-01',
                'pending_llm', 5)""")
    # Future-date candidate: should be included in --rescore
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, score)
        VALUES (9006, 'Future event', 'A future art exhibition', 'Newsletter', '2027-06-01',
                'pending_llm', 5)""")
    # Written candidate matching a veto: state must NOT change
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, notion_page_id, score)
        VALUES (9007, 'Silent Writing London', 'A writing group meetup', 'Meetup', '2027-06-01',
                'written', 'fake-notion-page-9007', 8)""")
    # Non-written candidate matching same veto: state SHOULD change
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, score)
        VALUES (9008, 'Silent Writing London', 'A writing group meetup', 'Meetup', '2027-06-01',
                'pending_llm', 8)""")
    # Unscored written candidate matching veto: tests guard in NORMAL mode (not just --rescore)
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, notion_page_id)
        VALUES (9009, 'Silent Writing Camden', 'A writing group meetup', 'Meetup', '2027-06-01',
                'written', 'fake-notion-page-9009')""")
    # Luma tracking URL on a pending candidate: must be canonicalised pre-scoring
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, url)
        VALUES (9010, 'Luma newsletter event', 'A talk about design', 'Newsletter', '2027-06-01',
                'pending_llm', 'https://luma.com/abc-123?lm_api_id=discplace-XYZ&lm_medium=email')""")
    # Luma tracking URL on a WRITTEN candidate: history must not be rewritten
    conn.execute("""INSERT INTO candidates
        (id, name, description, source, date, pipeline_state, notion_page_id, score, url)
        VALUES (9011, 'Written Luma event', 'A gig announcement', 'Newsletter', '2027-06-01',
                'written', 'fake-notion-page-9011', 5,
                'https://luma.com/def-456?lm_api_id=discplace-KEEP')""")
    conn.commit()
    conn.close()

    import subprocess
    result = subprocess.run(
        [sys.executable, __file__, "--db", str(db_path)],
        capture_output=True, text=True,
    )

    check("scoring subprocess succeeded", result.returncode, 0)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    row1 = conn.execute("SELECT pipeline_state, tier FROM candidates WHERE id = 9001").fetchone()
    check("already-reviewed keeps pipeline_state", row1["pipeline_state"], "llm_rejected")

    row2 = conn.execute("SELECT pipeline_state, tier FROM candidates WHERE id = 9002").fetchone()
    check("new no-desc gets pending_llm", row2["pipeline_state"], "pending_llm")
    check("new no-desc gets Couldn't Process tier", row2["tier"], "Couldn't Process")

    row3 = conn.execute("SELECT pipeline_state, score FROM candidates WHERE id = 9003").fetchone()
    check("described candidate gets pending_llm", row3["pipeline_state"], "pending_llm")
    check("described candidate gets scored", row3["score"] is not None, True)

    row4 = conn.execute("SELECT pipeline_state, score FROM candidates WHERE id = 9004").fetchone()
    check("written event keeps pipeline_state", row4["pipeline_state"], "written")
    check("written event still gets scored", row4["score"] is not None, True)

    row9 = conn.execute("SELECT pipeline_state, tier FROM candidates WHERE id = 9009").fetchone()
    check("normal mode: written+vetoed keeps pipeline_state", row9["pipeline_state"], "written")
    check("normal mode: written+vetoed gets Vetoed tier", row9["tier"], "Vetoed")

    row10 = conn.execute("SELECT url FROM candidates WHERE id = 9010").fetchone()
    check("pending Luma URL canonicalised", row10["url"], "https://lu.ma/abc-123")

    row11 = conn.execute("SELECT url FROM candidates WHERE id = 9011").fetchone()
    check("written Luma URL untouched", row11["url"],
          "https://luma.com/def-456?lm_api_id=discplace-KEEP")

    conn.close()

    # --- Rescore-specific tests ---
    result2 = subprocess.run(
        [sys.executable, __file__, "--db", str(db_path), "--rescore"],
        capture_output=True, text=True,
    )

    check("rescore subprocess succeeded", result2.returncode, 0)

    check("past-date excluded from rescore", "Past event" not in result2.stdout, True)
    check("future-date included in rescore", "Future event" in result2.stdout, True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    row7 = conn.execute("SELECT pipeline_state, tier FROM candidates WHERE id = 9007").fetchone()
    check("written+vetoed keeps pipeline_state during rescore", row7["pipeline_state"], "written")
    check("written+vetoed gets Vetoed tier during rescore", row7["tier"], "Vetoed")

    row8 = conn.execute("SELECT pipeline_state, tier FROM candidates WHERE id = 9008").fetchone()
    check("non-written+vetoed gets pipeline_state=vetoed during rescore", row8["pipeline_state"], "vetoed")
    check("non-written+vetoed gets Vetoed tier during rescore", row8["tier"], "Vetoed")

    conn.close()
    Path(db_path).unlink(missing_ok=True)

    return 0 if ok else 1


def normalise_pending_urls(conn) -> int:
    """Canonicalise Luma URLs on pre-review candidates.

    API-fetched candidates are canonical at insert (fetch_luma normalises),
    but newsletter extraction copies URLs verbatim from the email, so Luma
    links arrive as luma.com/slug?lm_api_id=... tracking variants. The DB is
    the operational source of truth and DB-wins remediation pushes its URL
    back to Notion, so the stored value must be the clean one. Scoring runs
    on every candidate before any write, which makes this pass cover every
    insert path. Only pending_llm rows are touched: written history is not
    rewritten.
    """
    rows = conn.execute(
        "SELECT id, url FROM candidates WHERE pipeline_state = 'pending_llm' "
        "AND url IS NOT NULL "
        "AND (url LIKE 'https://luma.com/%' OR url LIKE 'https://lu.ma/%')"
    ).fetchall()
    fixed = 0
    for row in rows:
        rid, url = row[0], row[1]
        canonical = normalise_luma_url(url)
        if canonical != url:
            conn.execute("UPDATE candidates SET url = ? WHERE id = ?", (canonical, rid))
            fixed += 1
    if fixed:
        conn.commit()
        print(f"URL normalisation: {fixed} Luma URL(s) canonicalised to lu.ma without params")
    return fixed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Algorithmic event scoring")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB),
                        help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-score all future-date candidates, not just unscored ones")
    parser.add_argument("--test-overrides", action="store_true",
                        help="Run override loading tests (not run in production)")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run DB integration smoke tests")
    args = parser.parse_args()

    if args.smoke_test:
        sys.exit(_smoke_tests())

    if args.test_overrides:
        sys.exit(_test_load_overrides())

    # Self-test: verify matcher invariants before touching any data.
    _run_self_test()
    _run_format_verify_test()

    # Load user-approved overrides (after self-tests: hardcoded logic always validated first)
    overrides = load_scoring_overrides()
    if overrides["known_organisers"] or overrides["known_series_patterns"]:
        KNOWN_ORGANISERS.update(overrides["known_organisers"])
        KNOWN_SERIES_PATTERNS.extend(overrides["known_series_patterns"])
        print(f"Overrides loaded: +{len(overrides['known_organisers'])} organisers, "
              f"+{len(overrides['known_series_patterns'])} series")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Canonicalise Luma URLs from non-API insert paths (newsletter extraction
    # copies tracking variants verbatim). Skipped on --dry-run.
    if not args.dry_run:
        normalise_pending_urls(conn)

    # Fetch candidates to score (exclude manual stubs: user-added Notion pages)
    if args.rescore:
        cur.execute("SELECT * FROM candidates WHERE pipeline_state != 'vetoed' AND COALESCE(source, '') != 'manual' AND COALESCE(end_date, date) >= date('now')")
    else:
        cur.execute("SELECT * FROM candidates WHERE score IS NULL AND pipeline_state != 'vetoed' AND COALESCE(source, '') != 'manual' AND COALESCE(end_date, date) >= date('now')")

    candidates = [dict(row) for row in cur.fetchall()]

    if not candidates:
        print("No candidates to score.")
        return

    print(f"Scoring {len(candidates)} candidates...\n")

    results = {"vetoed": [], "top_picks": [], "recommended": [], "borderline": [], "couldnt_process": []}
    veto_counts = {}

    for c in candidates:
        # Step 1: Veto pre-filter
        vetoed, veto_reason = check_veto(c)
        if vetoed:
            veto_counts[veto_reason] = veto_counts.get(veto_reason, 0) + 1
            results["vetoed"].append((c["id"], c["name"], veto_reason))
            if not args.dry_run:
                if c.get("notion_page_id"):
                    cur.execute(
                        "UPDATE candidates SET veto_reason = ?, tier = 'Vetoed' WHERE id = ?",
                        (veto_reason, c["id"]),
                    )
                else:
                    cur.execute(
                        "UPDATE candidates SET veto_reason = ?, tier = 'Vetoed', pipeline_state = 'vetoed' WHERE id = ?",
                        (veto_reason, c["id"]),
                    )
            continue

        # Auto-assign format_type if missing (e.g. newsletter events)
        if not c.get("format_type"):
            c["format_type"] = classify_format(c.get("name") or "", c.get("description") or "")
            if not args.dry_run:
                cur.execute(
                    "UPDATE candidates SET format_type = ? WHERE id = ?",
                    (c["format_type"], c["id"]),
                )

        # Second-pass format verification (regex patterns for Social meetup)
        if c.get("format_type") == "Social meetup":
            verified = verify_format(c)
            if verified != "Social meetup":
                c["format_type"] = verified
                if not args.dry_run:
                    cur.execute(
                        "UPDATE candidates SET format_type = ? WHERE id = ?",
                        (verified, c["id"]),
                    )

        # Step 1b: Null-description pre-filter
        if not (c.get("description") or "").strip():
            if not args.dry_run and not c.get("llm_reviewed") and not c.get("notion_page_id"):
                cur.execute(
                    "UPDATE candidates SET tier = 'Couldn''t Process', pipeline_state = 'pending_llm' WHERE id = ?",
                    (c["id"],),
                )
            results["couldnt_process"].append((c["id"], c["name"]))
            continue

        # Step 2: Algorithmic scoring
        score, signals, needs_llm = score_candidate(c)
        tier = assign_tier(score)

        signals_json = json.dumps([s[0] for s in signals])
        signal_detail = ", ".join(f"{s[0]}={s[1]:+d}" for s in signals)

        if tier == "Top Picks":
            results["top_picks"].append((c["id"], c["name"], score, signal_detail))
        elif tier == "Recommended":
            results["recommended"].append((c["id"], c["name"], score, signal_detail))
        else:
            results["borderline"].append((c["id"], c["name"], score))

        if not args.dry_run:
            if args.rescore or c.get("notion_page_id"):
                cur.execute(
                    """UPDATE candidates
                       SET score = ?, tier = ?, signals_fired = ?
                       WHERE id = ?""",
                    (score, tier, signals_json, c["id"]),
                )
            else:
                cur.execute(
                    """UPDATE candidates
                       SET score = ?, tier = ?, signals_fired = ?, pipeline_state = 'pending_llm'
                       WHERE id = ?""",
                    (score, tier, signals_json, c["id"]),
                )

    if not args.dry_run:
        conn.commit()

    conn.close()

    # --- Report ---
    print("=" * 60)
    print("SCORING RESULTS")
    print("=" * 60)

    if results["vetoed"]:
        print(f"\nVETOED ({len(results['vetoed'])}):\n")
        for cid, name, reason in results["vetoed"]:
            print(f"  [{cid}] {name}: {reason}")
        print(f"\n  Veto breakdown: {veto_counts}")

    if results["top_picks"]:
        print(f"\nTOP PICKS ({len(results['top_picks'])}):\n")
        for cid, name, score, detail in results["top_picks"]:
            print(f"  [{cid}] {name} (score={score})")
            print(f"       {detail}")

    if results["recommended"]:
        print(f"\nRECOMMENDED ({len(results['recommended'])}):\n")
        for cid, name, score, detail in results["recommended"]:
            print(f"  [{cid}] {name} (score={score})")
            print(f"       {detail}")

    if results["borderline"]:
        print(f"\nBORDERLINE ({len(results['borderline'])}):\n")
        for cid, name, score in results["borderline"]:
            print(f"  [{cid}] {name} (score={score})")

    if results["couldnt_process"]:
        print(f"\nCOULDN'T PROCESS ({len(results['couldnt_process'])}):\n")
        for cid, name in results["couldnt_process"]:
            print(f"  [{cid}] {name}: no description")

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {len(results['vetoed'])} vetoed, {len(results['top_picks'])} Top Picks, "
          f"{len(results['recommended'])} Recommended, {len(results['borderline'])} Borderline, "
          f"{len(results['couldnt_process'])} Couldn't Process")


if __name__ == "__main__":
    main()
