#!/usr/bin/env python3
"""One-command Notion bootstrap: create the two databases the pipeline writes to.

Creates, under a Notion page you own:
  1. "Event Recommendations": the board your picks land on (write_notion.py)
  2. "Pipeline Runs"        : the weekly run log (Step 7 of the scheduled task)

Usage:
    NOTION_TOKEN=... python3 scripts/bootstrap_notion.py --parent-page <page-id>
    NOTION_TOKEN=... python3 scripts/bootstrap_notion.py --parent-page <page-id> --skip-runs

Prerequisites:
  - Create an internal integration at notion.so/my-integrations and copy its
    token into NOTION_TOKEN.
  - Create (or pick) a Notion page and connect the integration to it
    (page ••• menu → Connections → your integration).
  - Pass that page's id (the 32-hex-char part of its URL) as --parent-page.

On success it prints the database ids to put in your .env:
    NOTION_DATABASE_ID=...   (Event Recommendations: used by write_notion.py)
    PIPELINE_RUNS_DB_ID=...  (Pipeline Runs: used by the scheduled task)

One manual step the API cannot do: the "Place" property (map pins on cards)
is not a creatable property type in Notion's public API. If you want the
map preview, add a "Place" property to Event Recommendations by hand in the
Notion UI afterwards. Everything else works without it: write_notion.py
only attaches Place when the property exists.
"""
import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from erlib.notion import NotionClient, NotionError  # noqa: E402

# Canonical Type options: keep in sync with classify_format() in
# fetch_meetup.py and the FORMAT TYPE VERIFICATION list in llm_sense_check.py.
FORMAT_TYPES = [
    "Social meetup", "Workshop", "Talk", "Outdoor", "Creative social",
    "Concert", "Exhibition", "Wellness", "Immersive experience", "Film",
    "Book event", "Other",
]

SOURCES = ["Meetup", "Luma", "Eventbrite", "Newsletter", "Venue website", "manual"]

TIERS = ["Top Picks", "Recommended", "Borderline", "Couldn't Process"]

VERDICTS = ["Going", "Maybe", "Not Going", "Undecided"]


def _select(options: list[str]) -> dict:
    return {"select": {"options": [{"name": o} for o in options]}}


RECOMMENDATIONS_PROPERTIES = {
    "Event": {"title": {}},
    "Date": {"date": {}},
    "Time of day": {"rich_text": {}},
    "Month": _select([]),
    "Tier": _select(TIERS),
    "Score": {"number": {}},
    "Type": _select(FORMAT_TYPES),
    "Added": {"date": {}},
    "Batch": _select([]),
    "Venue": {"rich_text": {}},
    "Cost": {"rich_text": {}},
    "Description": {"rich_text": {}},
    "Link": {"url": {}},
    "Source": _select(SOURCES),
    "Travel from home": {"rich_text": {}},
    # Triage fields: you set these; sync_verdicts.py reads them back into
    # SQLite so the feedback digest can learn from your decisions.
    "Verdict": _select(VERDICTS),
    "Reason it failed": _select([]),
    "Notes": {"rich_text": {}},
    "To delete": {"checkbox": {}},
}

RUNS_PROPERTIES = {
    "Batch": {"title": {}},
    "Batch date": {"date": {}},
    "Status": _select(["Success", "Partial", "Failed"]),
    "Events added": {"number": {}},
    "Issues": {"rich_text": {}},
    "Sources": {"rich_text": {}},
    "Pipeline": {"rich_text": {}},
    "Tier breakdown": {"rich_text": {}},
    "Disagreements": {"rich_text": {}},
    "Trigger": _select(["Scheduled", "Manual"]),
    "Duration": {"rich_text": {}},
    "Post-mortem": {"url": {}},
}


def create_database(client: NotionClient, parent_page: str, title: str,
                    properties: dict) -> str:
    resp = client.request("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": properties,
    })
    return resp["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parent-page", required=True,
                    help="Page id the databases are created under "
                         "(connect your integration to this page first)")
    ap.add_argument("--skip-runs", action="store_true",
                    help="Only create Event Recommendations, not Pipeline Runs")
    args = ap.parse_args()

    try:
        client = NotionClient()
    except NotionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        rec_id = create_database(client, args.parent_page,
                                 "Event Recommendations",
                                 RECOMMENDATIONS_PROPERTIES)
        print(f"Created 'Event Recommendations': {rec_id}")
        runs_id = None
        if not args.skip_runs:
            runs_id = create_database(client, args.parent_page,
                                      "Pipeline Runs", RUNS_PROPERTIES)
            print(f"Created 'Pipeline Runs':        {runs_id}")
    except NotionError as e:
        print(f"ERROR: Notion API call failed: {e}", file=sys.stderr)
        print("Check that the integration is connected to the parent page "
              "(page ••• menu → Connections).", file=sys.stderr)
        return 1

    print("\nAdd to your .env:")
    print(f"NOTION_DATABASE_ID={rec_id}")
    if runs_id:
        print(f"PIPELINE_RUNS_DB_ID={runs_id}")
    print("\nOptional manual step: add a 'Place' property in the Notion UI "
          "if you want map pins (not creatable via the API).")
    return 0


def _self_test() -> int:
    """Offline checks: payload shapes and property coverage vs write_notion."""
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    # Every property write_notion.py emits must exist in the bootstrap schema.
    from pipeline import write_notion  # noqa: PLC0415
    emitted = {"Event", "Date", "Time of day", "Month", "Tier", "Score",
               "Type", "Added", "Batch", "Venue", "Cost", "Description",
               "Link", "Source", "Travel from home", "Verdict"}
    missing = emitted - set(RECOMMENDATIONS_PROPERTIES)
    check("write_notion properties covered", missing, set())
    check("title property is Event",
          "title" in RECOMMENDATIONS_PROPERTIES["Event"], True)
    check("tier options match tier_label",
          [o["name"] for o in RECOMMENDATIONS_PROPERTIES["Tier"]["select"]["options"]],
          TIERS)
    for t in TIERS:
        try:
            write_notion.tier_label(t)
        except ValueError:
            ok = False
            print(f"  FAIL tier {t!r} rejected by write_notion.tier_label")
    # sync_verdicts allowlist must equal the Verdict options.
    from pipeline import sync_verdicts  # noqa: PLC0415
    check("verdict options match sync_verdicts allowlist",
          set(VERDICTS), sync_verdicts.VERDICT_ALLOWLIST)
    check("runs title property is Batch",
          "title" in RUNS_PROPERTIES["Batch"], True)
    print("All passed" if ok else "FAILURES", file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
