#!/usr/bin/env python3
"""
Apply user-approved digest findings to scoring_overrides.json.

Reads the Scoring Feedback Notion database for approved findings,
writes matching entries to scoring_overrides.json, marks them as Applied.

Usage:
    NOTION_TOKEN=... FEEDBACK_DB_ID=... python3 apply_findings.py \
        [--db PATH] [--dry-run] [--smoke-test]
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
import json
import os
import sqlite3
import sys
from pathlib import Path

from erlib import db as erdb
from erlib.config import DB_PATH
from erlib.notion import NotionClient

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
OVERRIDES_PATH = SCRIPT_DIR / "scoring_overrides.json"

AUTO_APPLY_TYPES = {"organiser", "series"}


def query_approved_findings(token: str, db_id: str) -> list[dict]:
    """Query Notion for findings with Status = Approved."""
    body = {
        "filter": {
            "and": [
                {"property": "Status", "select": {"equals": "Approved"}},
                {"property": "Row Type", "select": {"equals": "Finding"}},
            ]
        },
    }
    client = NotionClient(token)
    findings = []
    # paginate: a busy fortnight can exceed one page; the old single-request
    # version silently dropped findings past 100 (audit P2).
    for page in client.paginate("POST", f"/databases/{db_id}/query", body):
        props = page.get("properties", {})
        finding_key_parts = props.get("Finding key", {}).get("rich_text", [])
        finding_key = finding_key_parts[0]["text"]["content"] if finding_key_parts else ""
        ftype_options = props.get("Finding Type", {}).get("multi_select", [])
        if ftype_options:
            ftype = ftype_options[0]["name"]
        else:
            ftype = props.get("Finding Type", {}).get("select", {}).get("name", "")
        title_parts = props.get("Month", {}).get("title", [])
        title = title_parts[0]["text"]["content"] if title_parts else ""

        findings.append({
            "page_id": page["id"],
            "finding_key": finding_key,
            "type": ftype.lower().replace(" ", "_").replace("-", "_"),
            "title": title,
        })
    return findings


def load_current_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {"version": 1, "known_organisers": [], "known_series_patterns": []}
    try:
        with open(OVERRIDES_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "known_organisers": [], "known_series_patterns": []}


def apply_finding(finding: dict, overrides: dict,
                  conn: sqlite3.Connection) -> bool:
    """Apply a single approved finding to the overrides dict.
    Returns True if applied, False if not applicable."""
    ftype = finding["type"]
    key = finding["finding_key"]

    if ftype not in AUTO_APPLY_TYPES:
        print(f"  SKIP (manual review): {key} ({ftype})")
        return False

    row = conn.execute(
        "SELECT target_name FROM digest_findings WHERE finding_key = ?",
        (key,),
    ).fetchone()
    if not row:
        print(f"  SKIP (not in SQLite): {key}")
        return False

    target = row["target_name"].strip().lower()

    if ftype == "organiser":
        if target not in overrides.get("known_organisers", []):
            overrides.setdefault("known_organisers", []).append(target)
            print(f"  APPLIED: {target} -> known_organisers")
            return True
        print(f"  SKIP (already present): {target}")
        return False

    if ftype == "series":
        if target not in overrides.get("known_series_patterns", []):
            overrides.setdefault("known_series_patterns", []).append(target)
            print(f"  APPLIED: {target} -> known_series_patterns")
            return True
        print(f"  SKIP (already present): {target}")
        return False

    return False


def mark_applied(token: str, page_id: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY RUN] Would mark {page_id} as Applied")
        return
    body = {"properties": {"Status": {"select": {"name": "Applied"}}}}
    NotionClient(token).request("PATCH", f"/pages/{page_id}", body)


def save_overrides(overrides: dict, dry_run: bool) -> bool:
    """Write overrides to file. Returns True if file changed."""
    if dry_run:
        print(f"[DRY RUN] Would write overrides: "
              f"{len(overrides.get('known_organisers', []))} organisers, "
              f"{len(overrides.get('known_series_patterns', []))} series")
        return False

    old_content = ""
    if OVERRIDES_PATH.exists():
        old_content = OVERRIDES_PATH.read_text()

    overrides["version"] = 1
    new_content = json.dumps(overrides, indent=2) + "\n"

    if new_content == old_content:
        return False

    with open(OVERRIDES_PATH, "w") as f:
        f.write(new_content)
    return True


def _smoke_tests() -> int:
    import tempfile

    failures = 0

    def check(label, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL: {label}: got {got!r}, want {want!r}")
            failures += 1
        else:
            print(f"  OK  {label}")

    print("apply_findings smoke tests:")

    # load_current_overrides with missing file
    result = load_current_overrides()
    check("missing file returns empty", result["known_organisers"], [])

    # apply_finding: organiser
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115  path must outlive handle
    tmp_conn = sqlite3.connect(tmp_db.name)
    tmp_conn.row_factory = sqlite3.Row
    tmp_conn.execute("""CREATE TABLE digest_findings (
        finding_key TEXT, target_name TEXT)""")
    tmp_conn.execute(
        "INSERT INTO digest_findings VALUES (?, ?)",
        ("organiser:builder beers", "Builder Beers"),
    )
    tmp_conn.execute(
        "INSERT INTO digest_findings VALUES (?, ?)",
        ("series:ramen space", "Ramen Space"),
    )
    tmp_conn.commit()

    overrides = {"version": 1, "known_organisers": [], "known_series_patterns": []}
    finding = {"type": "organiser", "finding_key": "organiser:builder beers",
               "title": "Add 'Builder Beers'", "page_id": "test"}
    applied = apply_finding(finding, overrides, tmp_conn)
    check("organiser applied", applied, True)
    check("organiser in list", "builder beers" in overrides["known_organisers"], True)

    # Duplicate apply
    applied2 = apply_finding(finding, overrides, tmp_conn)
    check("duplicate not applied", applied2, False)

    # Series
    finding_s = {"type": "series", "finding_key": "series:ramen space",
                 "title": "Add 'Ramen Space'", "page_id": "test"}
    applied_s = apply_finding(finding_s, overrides, tmp_conn)
    check("series applied", applied_s, True)
    check("series in list", "ramen space" in overrides["known_series_patterns"], True)

    # Non-auto-apply type
    finding_veto = {"type": "veto_exemption", "finding_key": "veto:x",
                    "title": "Review veto", "page_id": "test"}
    applied3 = apply_finding(finding_veto, overrides, tmp_conn)
    check("veto not auto-applied", applied3, False)

    # Missing from SQLite
    finding_missing = {"type": "organiser", "finding_key": "organiser:nonexistent",
                       "title": "Unknown", "page_id": "test"}
    applied4 = apply_finding(finding_missing, overrides, tmp_conn)
    check("missing from SQLite not applied", applied4, False)

    # save_overrides
    tmp_override = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)  # noqa: SIM115  path must outlive handle
    tmp_override.close()
    _orig_path = OVERRIDES_PATH
    # Can't easily test save_overrides without monkeypatching the path,
    # so just verify the dict structure
    check("overrides has organisers", "known_organisers" in overrides, True)
    check("overrides has series", "known_series_patterns" in overrides, True)
    check("overrides org count", len(overrides["known_organisers"]), 1)
    check("overrides series count", len(overrides["known_series_patterns"]), 1)

    tmp_conn.close()
    os.unlink(tmp_db.name)
    os.unlink(tmp_override.name)

    if failures:
        print(f"\n{failures} smoke test(s) FAILED")
        return 1
    print("\nPASS: all apply_findings smoke tests passed")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Apply approved digest findings")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--feedback-db-id", type=str, default=None)
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    args = parser.parse_args()

    if args.smoke_test:
        sys.exit(_smoke_tests())

    notion_token = os.environ.get("NOTION_TOKEN")
    findings_db_id = args.feedback_db_id or os.environ.get("FEEDBACK_DB_ID")

    if not notion_token or not findings_db_id:
        print("ERROR: NOTION_TOKEN and FEEDBACK_DB_ID required.", file=sys.stderr)
        sys.exit(1)

    print("Querying Notion for approved findings...")
    approved = query_approved_findings(notion_token, findings_db_id)
    print(f"Found {len(approved)} approved finding(s)")

    if not approved:
        print("Nothing to apply.")
        return

    overrides = load_current_overrides()
    conn = erdb.connect(args.db)

    applied_count = 0
    for finding in approved:
        if apply_finding(finding, overrides, conn):
            mark_applied(notion_token, finding["page_id"], args.dry_run)
            applied_count += 1

    conn.close()

    if applied_count > 0:
        changed = save_overrides(overrides, args.dry_run)
        if changed:
            print(f"scoring_overrides.json updated ({applied_count} addition(s))")
        else:
            print("scoring_overrides.json unchanged (all already present)")
    else:
        print("No auto-applicable findings to apply")


if __name__ == "__main__":
    main()
