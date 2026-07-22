"""Cross-source duplicate detection: the one engine.

fetch_meetup and fetch_luma carried near-identical forks; only Luma's
canonicalised lu.ma/luma.com URLs. This superset canonicalises Luma URLs
for every caller (a no-op for non-Luma URLs), so a Meetup run also
recognises a newsletter row that stored a luma.com link.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .normalise import normalise_luma_url, normalise_name


@dataclass
class Existing:
    """Snapshot of candidates already in the DB, for dedup lookups."""

    urls: set
    name_dates: set
    name_dates_normalised: set
    url_details: dict

    def add(self, candidate: dict) -> None:
        """Register a just-inserted candidate so later ones dedup against it."""
        url = candidate.get("url")
        if url:
            self.urls.add(url)
            canonical = normalise_luma_url(url)
            if canonical:
                self.urls.add(canonical)
        self.name_dates.add((candidate["name"], candidate["date"]))
        self.name_dates_normalised.add(
            (normalise_name(candidate["name"]), candidate["date"])
        )


def load_existing(conn: sqlite3.Connection) -> Existing:
    cur = conn.cursor()
    urls = set()
    url_details: dict = {}
    cur.execute(
        "SELECT url, id, date, pipeline_state, notion_page_id "
        "FROM candidates WHERE url IS NOT NULL AND url != ''"
    )
    for r in cur.fetchall():
        detail = {"id": r[1], "date": r[2], "pipeline_state": r[3],
                  "notion_page_id": r[4]}
        urls.add(r[0])
        url_details[r[0]] = detail
        canonical = normalise_luma_url(r[0])
        if canonical:
            urls.add(canonical)
            url_details[canonical] = detail
    cur.execute("SELECT name, date FROM candidates")
    rows = cur.fetchall()
    return Existing(
        urls=urls,
        name_dates={(r[0], r[1]) for r in rows},
        name_dates_normalised={(normalise_name(r[0]), r[1]) for r in rows},
        url_details=url_details,
    )


def is_duplicate(candidate: dict, existing: Existing) -> bool:
    url = candidate.get("url")
    if url and (url in existing.urls or normalise_luma_url(url) in existing.urls):
        return True
    if (candidate["name"], candidate["date"]) in existing.name_dates:
        return True
    return (
        normalise_name(candidate["name"]), candidate["date"]
    ) in existing.name_dates_normalised
