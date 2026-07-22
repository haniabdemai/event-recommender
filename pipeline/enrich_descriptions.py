#!/usr/bin/env python3
"""
Description enrichment for newsletter events with thin descriptions.

Candidates extracted from newsletter emails often have short marketing
blurbs (68-183 chars) instead of full event descriptions. This script
identifies those candidates and prepares batches for subagents to fetch
full descriptions from event pages.

Two strategies:
  Strategy A (url_follow): Candidate has a URL pointing to a specific event
  page (not a homepage). The subagent uses WebFetch, follows redirects, and
  extracts the description.

  Strategy B (web_search): Candidate has no URL or a homepage-only URL.
  The subagent uses WebSearch to find the event page by name + venue domain,
  then WebFetch to extract the description.

Usage:
    python3 enrich_descriptions.py --prepare
    python3 enrich_descriptions.py --apply results.json
    python3 enrich_descriptions.py --smoke-test
"""
from __future__ import annotations

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
import html
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402
from erlib.freshness import stamp  # noqa: E402
OUTPUT_FILE = SCRIPT_DIR / ".enrich_batches.json"
SUMMARY_FILE = SCRIPT_DIR / ".last_enrichment_summary.json"

THIN_THRESHOLD = 200
BATCH_SIZE = 10

# Newsletter click-tracking domains that block datacentre IPs: enrichment
# marks these proxy_blocked instead of burning retries. Add your senders'
# tracking domains here (fictional examples shipped).
PROXY_BLOCKED_DOMAINS = frozenset({
    "mailings.example-council.org",
    "e.riverfront-arts.example",
})

# Map organiser names (lowercase) to their venue website domain so thin
# newsletter blurbs can be enriched from the venue's event pages. Populate
# with YOUR newsletter senders (fictional examples shipped).
ORGANISER_TO_DOMAIN = {
    "riverfront arts": "riverfront-arts.example",
    "city gallery": "city-gallery.example",
    "warehouse live": "warehouse-live.example",
}

# Same mapping keyed by the candidate's source label, for rows whose
# organiser is empty but whose source names the sender.
SOURCE_TO_DOMAIN = {
    "riverfront": "riverfront-arts.example",
}


def _is_homepage_url(url: str) -> bool:
    """True if URL is just a domain root with no meaningful path."""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        return path == "" or path == "/"
    except Exception:
        return True


def _get_venue_domain(organiser: str | None, url: str | None,
                      source: str | None = None) -> str | None:
    """Derive the venue website domain from organiser name, URL, or source."""
    if organiser:
        domain = ORGANISER_TO_DOMAIN.get(organiser.strip().lower())
        if domain:
            return domain
    if url:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            for org_domain in ORGANISER_TO_DOMAIN.values():
                if org_domain and org_domain in host:
                    return org_domain
        except Exception:
            pass
    if source:
        domain = SOURCE_TO_DOMAIN.get(source.strip().lower())
        if domain:
            return domain
    return None


def _categorise(url: str | None) -> str:
    """Return 'url_follow', 'web_search', or 'proxy_blocked' based on URL."""
    if not url or not url.strip():
        return "web_search"
    if _is_homepage_url(url):
        return "web_search"
    try:
        host = urlparse(url).hostname or ""
        if any(host == d or host.endswith("." + d) for d in PROXY_BLOCKED_DOMAINS):
            return "proxy_blocked"
    except Exception:
        pass
    return "url_follow"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add description_source column if missing."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    if "description_source" not in existing:
        conn.execute(
            "ALTER TABLE candidates ADD COLUMN description_source TEXT"
        )
        print("MIGRATE: added column description_source")
        conn.commit()


def cmd_prepare(db_path: Path, backfill: bool = False) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    if backfill:
        rows = conn.execute("""
            SELECT id, name, url, description, organiser, source
            FROM candidates
            WHERE source NOT IN ('Meetup', 'Luma')
              AND COALESCE(end_date, date) >= date('now')
              AND notion_status = 'active'
              AND notion_page_id IS NOT NULL
              AND (description IS NULL OR description = ''
                   OR length(description) < ?)
              AND (description_source IS NULL
                   OR description_source NOT IN ('page', 'proxy_blocked'))
        """, (THIN_THRESHOLD,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, name, url, description, organiser, source
            FROM candidates
            WHERE pipeline_state = 'pending_llm'
              AND source = 'Newsletter'
              AND (description IS NULL OR description = ''
                   OR length(description) < ?)
              AND (description_source IS NULL
                   OR description_source != 'proxy_blocked')
        """, (THIN_THRESHOLD,)).fetchall()

    if not rows:
        conn.close()
        print("ENRICH_SKIP: no candidates need description enrichment")
        OUTPUT_FILE.write_text(json.dumps({"total": 0, "batches": []}))
        return 0

    candidates = []
    for r in rows:
        url = r["url"] or ""
        strategy = _categorise(url)
        # Both queries SELECT source: sqlite3.Row has no .get()
        venue_domain = _get_venue_domain(r["organiser"], url, r["source"])

        candidates.append({
            "id": r["id"],
            "name": r["name"],
            "url": url,
            "current_description": r["description"] or "",
            "current_length": len(r["description"] or ""),
            "strategy": strategy,
            "venue_domain": venue_domain,
        })

    blocked = [c for c in candidates if c["strategy"] == "proxy_blocked"]
    candidates = [c for c in candidates if c["strategy"] != "proxy_blocked"]

    if blocked:
        blocked_ids = [c["id"] for c in blocked]
        conn.execute(
            f"UPDATE candidates SET description_source = 'proxy_blocked' "
            f"WHERE id IN ({','.join('?' * len(blocked_ids))})",
            blocked_ids,
        )
        conn.commit()
        print(f"  Proxy-blocked (marked, skipped): {len(blocked)} "
              f"({', '.join(c['name'][:40] for c in blocked[:5])})")

    conn.close()

    batches = []
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i + BATCH_SIZE]
        batches.append({
            "batch_num": len(batches) + 1,
            "candidates": batch,
        })

    strategy_a = sum(1 for c in candidates if c["strategy"] == "url_follow")
    strategy_b = sum(1 for c in candidates if c["strategy"] == "web_search")

    output = {
        "total": len(candidates),
        "strategy_a_count": strategy_a,
        "strategy_b_count": strategy_b,
        "batches": batches,
        "subagent_prompt_template": _build_prompt_template(),
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False))

    print(f"ENRICH_PREPARE: {len(candidates)} candidates in {len(batches)} batch(es)")
    print(f"  Strategy A (URL follow): {strategy_a}")
    print(f"  Strategy B (Web search): {strategy_b}")
    return 0


def _build_prompt_template() -> str:
    return """\
You are enriching event descriptions. Each candidate below has a thin or \
missing description from a newsletter email. Your job is to get the FULL \
event description from the event's webpage.

For each candidate:

If strategy is "url_follow":
1. Use WebFetch on the URL.
2. If WebFetch reports a REDIRECT, make a second WebFetch call with the \
redirect URL.
3. Extract the event description from the page. Copy the main event \
description text verbatim: not navigation, ticket buttons, or boilerplate.

If strategy is "web_search":
1. Use WebSearch for: "EVENT_NAME" site:VENUE_DOMAIN
2. If a specific event page is found in the results, use WebFetch on that URL.
3. If no results with quotes, try without: EVENT_NAME site:VENUE_DOMAIN
4. Extract the event description from the page.

Rules:
- Extract ONLY the event description: what the event is about, who's \
performing, what to expect. Not ticket prices, accessibility info, or \
navigation text.
- If you cannot access the page or find the event, set description to null.
- Do not fabricate or paraphrase. Copy the description from the page.

CANDIDATES:
{candidates_json}

Return ONLY a JSON array. Include the candidate name so the pipeline can \
verify ID-name consistency:
[{{"id": <id>, "name": "<candidate name from input>", \
"description": "<full description or null>", \
"source_url": "<final URL you fetched from or null>"}}]
"""


def cmd_apply(db_path: Path, results_file: Path) -> int:
    if not results_file.exists():
        print(f"Results file not found: {results_file}", file=sys.stderr)
        return 2

    results = json.loads(results_file.read_text())
    if not isinstance(results, list):
        print("Results file must contain a JSON array.", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    enriched = 0
    skipped = 0
    failed = 0

    for item in results:
        cid = item.get("id")
        expected_name = item.get("name")
        new_desc = item.get("description")

        if cid is None:
            failed += 1
            print("  [None] SKIP: missing candidate ID in results")
            continue

        row = conn.execute(
            "SELECT name, description FROM candidates WHERE id = ?", (cid,)
        ).fetchone()

        if row is None:
            failed += 1
            print(f"  [{cid}] SKIP: candidate ID not found in database")
            continue

        if expected_name and row["name"] != expected_name:
            failed += 1
            print(f"  [{cid}] ID_MISMATCH: expected '{expected_name}', "
                  f"found '{row['name']}'. Batch file is stale: re-run --prepare.")
            continue

        if not new_desc or not isinstance(new_desc, str) or not new_desc.strip():
            failed += 1
            print(f"  [{cid}] SKIP: no description returned")
            continue

        new_desc = html.unescape(new_desc.strip())
        existing_len = len(row["description"]) if row["description"] else 0

        if len(new_desc) <= existing_len:
            skipped += 1
            print(f"  [{cid}] SKIP: enriched ({len(new_desc)}) not longer than existing ({existing_len})")
            continue

        conn.execute(
            "UPDATE candidates SET description = ?, description_source = 'page' WHERE id = ?",
            (new_desc, cid),
        )
        enriched += 1
        print(f"  [{cid}] ENRICHED: {existing_len} -> {len(new_desc)} chars")

    conn.commit()
    conn.close()

    summary = {
        "enriched": enriched,
        "skipped": skipped,
        "failed": failed,
    }
    SUMMARY_FILE.write_text(json.dumps(stamp(summary), indent=2))

    print(f"\nENRICH_APPLY: {enriched} enriched, {skipped} skipped, {failed} failed")
    return 0


# ---------------------------------------------------------------------------
# Smoke tests
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

    # URL categorisation
    check("homepage is web_search", _categorise("https://city-gallery.example"), "web_search")
    check("homepage with slash", _categorise("https://city-gallery.example/"), "web_search")
    check("empty is web_search", _categorise(""), "web_search")
    check("none is web_search", _categorise(None), "web_search")
    check("event page is url_follow",
          _categorise("https://www.riverfront-arts.example/whats-on/2026/event/foo"), "url_follow")
    check("tracking url is proxy_blocked (sandbox-blocked domain)",
          _categorise("https://e.riverfront-arts.example/c/AQiXig4QuYN2"), "proxy_blocked")
    check("second venue direct is url_follow",
          _categorise("https://www.warehouse-live.example/whats-on/foo"), "url_follow")

    # Homepage detection
    check("bare domain is homepage", _is_homepage_url("https://city-gallery.example"), True)
    check("domain with slash", _is_homepage_url("https://city-gallery.example/"), True)
    check("path is not homepage", _is_homepage_url("https://riverfront-arts.example/whats-on/foo"), False)
    check("tracking path is not homepage", _is_homepage_url("https://e.riverfront-arts.example/c/ABC"), False)
    check("www homepage", _is_homepage_url("https://www.city-gallery.example/"), True)

    # Venue domain mapping
    check("mapped organiser", _get_venue_domain("Riverfront Arts", None), "riverfront-arts.example")
    check("second organiser", _get_venue_domain("City Gallery", None), "city-gallery.example")
    check("third organiser", _get_venue_domain("Warehouse Live", None), "warehouse-live.example")
    
    check("unknown organiser", _get_venue_domain("Random Org", None), None)
    check("domain from url",
          _get_venue_domain(None, "https://e.riverfront-arts.example/c/foo"), "riverfront-arts.example")
    check("case insensitive", _get_venue_domain("RIVERFRONT ARTS", None), "riverfront-arts.example")

    # Source-to-domain fallback
    check("source fallback",
          _get_venue_domain(None, None, "Riverfront"), "riverfront-arts.example")
    check("source fallback case insensitive",
          _get_venue_domain(None, None, "riverfront"), "riverfront-arts.example")
    check("organiser takes priority over source",
          _get_venue_domain("Riverfront Arts", None, "Riverfront"), "riverfront-arts.example")
    check("unknown source returns None",
          _get_venue_domain(None, None, "Venue website"), None)

    # Prompt template builds without error
    prompt = _build_prompt_template()
    check("prompt template not empty", len(prompt) > 100, True)
    check("prompt has candidates placeholder", "{candidates_json}" in prompt, True)

    # Proxy-blocked domain detection
    check("proxy-blocked: council mailing domain",
          _categorise("https://mailings.example-council.org/c/AQiX"), "proxy_blocked")
    check("proxy-blocked: venue tracking domain",
          _categorise("https://e.riverfront-arts.example/c/AQiXig"), "proxy_blocked")
    check("not blocked: venue main site",
          _categorise("https://www.riverfront-arts.example/whats-on/2026/event/foo"), "url_follow")
    check("not blocked: luma",
          _categorise("https://lu.ma/some-event"), "url_follow")

    # ID-name mismatch detection in --apply
    import tempfile
    import io
    from contextlib import redirect_stdout

    tdb_enrich_path = Path(tempfile.mktemp(suffix=".db"))
    tdb_enrich = sqlite3.connect(tdb_enrich_path)
    tdb_enrich.execute("""CREATE TABLE candidates (
        id INTEGER PRIMARY KEY, name TEXT, description TEXT,
        description_source TEXT, pipeline_state TEXT DEFAULT 'pending_llm',
        source TEXT DEFAULT 'Newsletter', url TEXT, organiser TEXT,
        date TEXT, end_date TEXT, notion_status TEXT, notion_page_id TEXT)""")
    tdb_enrich.execute(
        "INSERT INTO candidates (id, name, description) "
        "VALUES (1, 'Real Event Name', NULL)")
    tdb_enrich.commit()
    tdb_enrich.close()

    mismatch_results = [{"id": 1, "name": "Wrong Event Name",
                         "description": "A long enough test description for enrichment"}]
    mismatch_path = Path(tempfile.mktemp(suffix=".json"))
    mismatch_path.write_text(json.dumps(mismatch_results))
    f_mm = io.StringIO()
    with redirect_stdout(f_mm):
        cmd_apply(tdb_enrich_path, mismatch_path)
    check("ID-name mismatch detected in apply",
          "ID_MISMATCH" in f_mm.getvalue(), True)

    correct_results = [{"id": 1, "name": "Real Event Name",
                        "description": "A long enough test description for enrichment purposes here"}]
    correct_path = Path(tempfile.mktemp(suffix=".json"))
    correct_path.write_text(json.dumps(correct_results))
    f_ok = io.StringIO()
    with redirect_stdout(f_ok):
        cmd_apply(tdb_enrich_path, correct_path)
    check("correct name accepted in apply",
          "ENRICHED" in f_ok.getvalue(), True)

    tdb_enrich_path.unlink(missing_ok=True)
    mismatch_path.unlink(missing_ok=True)
    correct_path.unlink(missing_ok=True)

    if not ok:
        return 1
    print(f"\nAll {31} smoke tests passed")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--prepare", action="store_true",
                        help="Find thin-description candidates and output batches")
    parser.add_argument("--backfill", action="store_true",
                        help="Include written/ready events, not just pending_llm")
    parser.add_argument("--apply", type=Path, metavar="RESULTS_FILE",
                        help="Apply enrichment results to SQLite")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    if args.smoke_test:
        sys.exit(_smoke_tests())
    elif args.prepare or args.backfill:
        sys.exit(cmd_prepare(args.db, backfill=args.backfill))
    elif args.apply:
        sys.exit(cmd_apply(args.db, args.apply))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
