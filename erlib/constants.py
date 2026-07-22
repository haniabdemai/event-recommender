"""Pipeline vocabulary: tier names, source names, and shared caps.

The string VALUES are frozen: they live in the SQLite DB and the Notion
database. Renaming one is a data migration, not an edit here.

pipeline_state values ('pending_llm', 'vetoed', ..., 'written') are
documented in CLAUDE.md §Database semantics. A PipelineState
class lived here until 2026-07-12 with zero production consumers: trimmed
rather than churning every string literal in the pipeline (Phase 2 card
"Tidy the shared library's unused corners").
"""
from __future__ import annotations

TIERS = ("Top Picks", "Recommended", "Borderline", "Not Recommended")

SOURCES = ("Meetup", "Luma", "Newsletter", "Venue", "manual")


# Borderline cap (enforced by validate_llm_output, warned by llm_sense_check):
# demote lowest-Python-score Borderlines when more than BORDERLINE_CAP of
# accepted candidates are Borderline and more than MIN_FOR_CAP were accepted.
BORDERLINE_CAP = 0.30
MIN_FOR_CAP = 10
