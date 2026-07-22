"""Text and URL normalisation: the one copy.

TYPO_TABLE and normalise_name were forked five times across the repo
(fetch_meetup, dedup_candidates, reconcile_notion, feedback_digest,
verify_newsletter_extraction) and merge_multiday carried a weaker
strip/lower-only variant that missed curly-quote title variants.
"""
from __future__ import annotations

import re
import unicodedata

# Typographic → ASCII mappings for Unicode normalisation.
TYPO_TABLE = str.maketrans({
    "‘": "'",   # LEFT SINGLE QUOTATION MARK
    "’": "'",   # RIGHT SINGLE QUOTATION MARK
    "“": '"',   # LEFT DOUBLE QUOTATION MARK
    "”": '"',   # RIGHT DOUBLE QUOTATION MARK
    "—": "-",   # EM DASH
    "–": "-",   # EN DASH
    " ": " ",   # NARROW NO-BREAK SPACE
    "​": "",    # ZERO WIDTH SPACE
    "͏": "",    # COMBINING GRAPHEME JOINER
    "﻿": "",    # ZERO WIDTH NO-BREAK SPACE (BOM)
})


def normalise_name(s: str | None) -> str:
    """Normalise a name/title for matching.

    NFKD (NBSP→space, ellipsis→'...', ligatures expanded), typographic→ASCII,
    lowercase, strip, collapse whitespace.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.translate(TYPO_TABLE)
    s = s.lower().strip()
    return re.sub(r"\s+", " ", s)


def normalise_luma_url(url: str | None) -> str | None:
    """Canonicalise lu.ma / luma.com URLs to lu.ma, stripping query params."""
    if not url:
        return url
    if url.startswith("https://luma.com/"):
        url = url.replace("https://luma.com/", "https://lu.ma/", 1)
    if url.startswith("https://lu.ma/") and "?" in url:
        url = url.split("?")[0]
    return url


def slug_from_luma_url(url: str | None) -> str | None:
    """Return the event slug from a lu.ma or luma.com URL, or None."""
    canonical = normalise_luma_url(url)
    if canonical and canonical.startswith("https://lu.ma/"):
        slug = canonical[len("https://lu.ma/"):]
        if slug and "/" not in slug:
            return slug
    return None
