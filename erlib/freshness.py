"""Data-driven run freshness: the ONE freshness check.

mtime comparison is meaningless inside GitHub Actions (checkout resets
every mtime to checkout time), and the two previous implementations
disagreed about a missing start marker (erlib.run_log._is_stale treated
it as fresh, pipeline_summary.is_fresh as stale). Unified semantics:

- is_fresh(path) is True only when the state file exists, carries a
  parseable ``timestamp_utc``, the ``.pipeline_start_time`` marker exists
  with a parseable timestamp, and the stamp is >= the run start.
- Anything unknowable: missing file, missing stamp, missing or garbled
  marker: is NOT fresh. A run report must never present data of unknown
  vintage as this run's output.

``timestamp_utc`` is the standard stamp key because ``.last_run_status.json``
already carried it before this module existed. Writers call stamp() on the
payload right before json.dump.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

WRITTEN_AT = "timestamp_utc"
START_MARKER = ".pipeline_start_time"


def _parse(ts: str) -> datetime:
    """Parse an ISO timestamp; naive values are assumed UTC."""
    dt = datetime.fromisoformat(ts.strip())
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stamp(payload: dict) -> dict:
    """Add the freshness timestamp to a JSON-state payload (in place)."""
    payload[WRITTEN_AT] = datetime.now(timezone.utc).isoformat()
    return payload


def run_start(start_marker: str | Path = START_MARKER) -> datetime | None:
    """The current run's start time, or None when unknowable."""
    try:
        return _parse(Path(start_marker).read_text())
    except (OSError, ValueError):
        return None


def is_fresh(path: str | Path, start_marker: str | Path = START_MARKER) -> bool:
    """True only when `path` was demonstrably written during this run."""
    start = run_start(start_marker)
    if start is None:
        return False
    try:
        payload = json.loads(Path(path).read_text())
        written = _parse(payload[WRITTEN_AT])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return written >= start
