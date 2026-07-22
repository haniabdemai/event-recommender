"""ISO datetime → London wall-clock. The one date parser.

Four sites parsed ISO datetimes with subtly different rules; Meetup's
parser kept the source offset's wall clock instead of converting to
Europe/London (audit: wall-clock inconsistency). Everything here converts
timezone-aware values to London; naive values are taken as already-local.
"""
from __future__ import annotations

from datetime import datetime

from .config import LONDON_TZ


def iso_to_london(iso: str | None) -> tuple[str, str]:
    """Parse ISO 8601 (Z suffix ok) → ('YYYY-MM-DD', 'HH:MM') in Europe/London.

    Returns ('', '') for missing/invalid input. Naive datetimes are assumed
    to already be London wall-clock.
    """
    if not iso:
        return "", ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "", ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(LONDON_TZ)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def end_date_if_different(start_iso: str | None, end_iso: str | None) -> str | None:
    """End date (London) when the event spans days; None for same-day/missing."""
    start_date, _ = iso_to_london(start_iso)
    if not start_date or not end_iso:
        return None
    end_date, _ = iso_to_london(end_iso)
    return end_date if end_date and end_date != start_date else None


def batch_label(iso_date: str) -> str:
    """'2026-04-30' -> '30 Apr': the human batch/email-date label.

    Raises ValueError on malformed input (callers decide their fallback).
    """
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%-d %b")
