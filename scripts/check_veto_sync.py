#!/usr/bin/env python3
"""CI guard: the veto taxonomy lives on three hand-maintained surfaces:
score_candidates.py VETO_PATTERNS (+ hardcoded check_veto branches), the
SENSE_CHECK_INSTRUCTION letters in llm_sense_check.py, and the Hard
Vetoes section of the taste profile (references/taste-profile.md if you
have personalised it, else the shipped taste-profile-template.md).
Nothing kept them in sync before this guard existed: an audit found the
surfaces had silently drifted.

MAPPING below is the single registry: every Python veto key maps to an
LLM letter or is declared PYTHON_ONLY with a reason; every letter is
claimed by a mapping entry or declared LLM_ONLY; every letter-mapped
concept must appear in the taste profile (profile marker substring).
Add to the registry when adding a veto: an unexplained asymmetry fails
CI, naming what drifted.

Exit 0 = in sync; 1 = drift (each problem printed).
"""
import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PYTHON_ONLY = "PYTHON_ONLY"

# python_key -> (letter | PYTHON_ONLY, reason-or-profile-marker)
# For letter-mapped keys the second field is ignored (the LETTERS table
# below carries the profile marker). For PYTHON_ONLY keys it documents WHY
# no LLM letter exists.
MAPPING = {
    "token_craft_workshop": ("N", ""),
    "networking_drinks": (PYTHON_ONLY, "keyword-detectable; LLM judges crowd/vibe contextually"),
    "corporate_tech": ("L", ""),
    "generic_social_dating": ("B", ""),
    "life_drawing": (PYTHON_ONLY, "a recurring format the example persona tried and bounced off; no generic LLM concept"),
    "wellness_upsell": (PYTHON_ONLY, "price-anchored upsell pattern; LLM assesses price-vs-value directly"),
    "immersive_walkthrough": ("M", ""),
    "classical_concert": ("D", ""),
    "language_learning": ("H", ""),
    "expat_international": ("I", ""),
    "writing_groups": ("C", ""),
    "tabletop_gaming": ("G", ""),
    "technical_developer_training": ("E", ""),
    "startup_investment": ("F", ""),
    "dating_events": ("B", ""),
    "wrong_sports": ("A", ""),
    "sport_viewing": ("A", ""),
    "discussion_group": ("C", ""),
    "public_speaking": ("K", ""),
    "wrong_age": (PYTHON_ONLY, "over-40s/50s framing; LLM letter S covers only 30s-40s"),
    "wrong_age_30s_40s": ("S", ""),
    "young_professionals": ("T", ""),
    "pub_quiz": ("O", ""),
    "karaoke": ("O", ""),
    "escape_room": ("O", ""),
    "pub_crawl": ("O", ""),
    "dj_practice": ("P", ""),
    "london_walks": ("Q", ""),
    "blocked_organiser": ("V", ""),
    # check_veto hardcoded branches (not VETO_PATTERNS keys)
    "singles_in_title_or_group": ("B", ""),
    "cost_over_30": (PYTHON_ONLY, "numeric threshold; LLM letter would duplicate parse_cost"),
    "park_hangout_far": ("W", ""),
}

# Letters with no Python counterpart: contextual judgments only the LLM
# can make (audit list, verified 2026-07-05).
LLM_ONLY_LETTERS = {
    "J": "identity-group fit needs a contextual read against the profile's My Communities section",
    "R": "group-exhibition-visit format needs reading the event structure",
    "U": "professional-community crowd fit is contextual",
}

# letter -> substring that must appear (case-insensitive) in the taste
# profile's Hard Vetoes section. Deleting a veto concept from the profile
# without touching code fails here.
LETTER_PROFILE_MARKERS = {
    "A": "sport",
    "B": "dating",
    "C": "book club",
    "D": "classical",
    "E": "technical training",
    "F": "startup",
    "G": "tabletop",
    "H": "language",
    "I": "international",
    "J": "identity",
    "K": "public speaking",
    "L": "corporate",
    "M": "walk-through",
    "N": "craft workshop",
    "O": "pub quiz",
    "P": "dj",
    "Q": "walks inside london",
    "R": "group exhibition",
    "S": "30s",
    "T": "young professionals",
    "U": "professional communities",
    "V": "hype collective",
    "W": "park",
}


def python_veto_surface(score_text: str) -> set[str]:
    """VETO_PATTERNS keys + hardcoded check_veto return reasons."""
    block = re.search(r"VETO_PATTERNS = \{(.*?)\n\}", score_text, re.S)
    keys = set(re.findall(r'^    "([a-z0-9_]+)": \[', block.group(1), re.M)) if block else set()
    keys |= set(re.findall(r'return True, "([a-z0-9_]+)"', score_text))
    return keys


def llm_letters(llm_text: str) -> set[str]:
    m = re.search(r'SENSE_CHECK_INSTRUCTION = """(.*?)"""', llm_text, re.S)
    if not m:
        return set()
    return set(re.findall(r"^\s{2}([A-Z])\.\s", m.group(1), re.M))


def profile_veto_text(profile_text: str) -> str:
    m = re.search(r"## Hard Vetoes(.*?)(?=^## |\Z)", profile_text, re.M | re.S)
    return m.group(1).lower() if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--score-file", default=str(REPO / "pipeline/score_candidates.py"))
    ap.add_argument("--llm-file", default=str(REPO / "pipeline/llm_sense_check.py"))
    personal = REPO / "references/taste-profile.md"
    template = REPO / "references/taste-profile-template.md"
    ap.add_argument(
        "--profile-file",
        default=str(personal if personal.exists() else template),
    )
    args = ap.parse_args()

    problems = []
    keys = python_veto_surface(Path(args.score_file).read_text())
    letters = llm_letters(Path(args.llm_file).read_text())
    profile = profile_veto_text(Path(args.profile_file).read_text())

    # Every Python veto has a mapping entry; no stale mapping entries.
    for key in sorted(keys - set(MAPPING)):
        problems.append(f"Python veto '{key}' has no entry in check_veto_sync MAPPING")
    for key in sorted(set(MAPPING) - keys):
        problems.append(f"MAPPING entry '{key}' no longer exists in score_candidates")

    # Every letter is claimed (by a mapping or LLM_ONLY); none are stale.
    claimed = {v[0] for v in MAPPING.values() if v[0] != PYTHON_ONLY}
    for letter in sorted(letters - claimed - set(LLM_ONLY_LETTERS)):
        problems.append(f"LLM letter '{letter}' is neither mapped to a Python veto nor in LLM_ONLY_LETTERS")
    for letter in sorted((claimed | set(LLM_ONLY_LETTERS)) - letters):
        problems.append(f"Letter '{letter}' referenced by the registry is missing from SENSE_CHECK_INSTRUCTION")

    # Every letter concept still appears in the taste profile.
    if not profile:
        problems.append("Hard Vetoes section not found in the taste profile")
    else:
        for letter in sorted(letters):
            marker = LETTER_PROFILE_MARKERS.get(letter)
            if marker is None:
                problems.append(f"Letter '{letter}' has no profile marker in LETTER_PROFILE_MARKERS")
            elif marker not in profile:
                problems.append(
                    f"Letter '{letter}' marker {marker!r} not found in the taste profile Hard Vetoes section")

    if problems:
        print("Veto sync FAILED: the three veto surfaces have drifted:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"Veto sync OK ({len(keys)} Python vetoes, {len(letters)} LLM letters, "
          f"{len(LLM_ONLY_LETTERS)} LLM-only, "
          f"{sum(1 for v in MAPPING.values() if v[0] == PYTHON_ONLY)} Python-only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
