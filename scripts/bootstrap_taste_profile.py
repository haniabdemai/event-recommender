#!/usr/bin/env python3
"""Draft your taste profile from your real history, not from scratch.

This is the one-off onboarding step. Instead of asking you to invent your
preferences cold, it reviews the events you have ACTUALLY attended (your
Google Calendar) and surfaces the patterns a taste profile is built from:
the venues and organisers you return to, the kinds of events you go to, how
far you travel, roughly what you pay. An agent session then turns that
summary into references/taste-profile.md and seeds the scoring lists, and
you review and approve. You never enumerate anything from scratch.

(This mirrors how the original private system was built: a one-off review
of real attendance history became the taste profile; the weekly pipeline
and the fortnightly feedback digest refine it from there.)

Usage:
    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=... \
      python3 scripts/bootstrap_taste_profile.py [--months 12] [--out PATH]

It prints a human-readable summary and writes a JSON payload (default
.taste_bootstrap.json) for the agent to draft your profile from. See
docs/taste-bootstrap.md for the full flow (including mining event emails).

No profile is written here: this gathers and summarises the evidence. The
drafting + your approval happen in the agent step. If you would rather not
connect your Calendar at all, skip this and use docs/taste-interview.md.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from pipeline.fetch_meetup import classify_format  # noqa: E402


# --- pure analysis (offline-testable) --------------------------------------

def title_stem(title: str) -> str:
    """A recurring-series key: title with trailing counters/dates stripped."""
    s = (title or "").lower().strip()
    s = re.sub(r"[#(].*$", "", s)          # drop "#41", "(June)", etc.
    s = re.sub(r"\b\d[\d:/.-]*\b", "", s)  # drop bare numbers/dates
    s = re.sub(r"\s+", " ", s).strip(" -–—:|")
    return s


def venue_frequency(events: list[dict]) -> Counter:
    c: Counter = Counter()
    for e in events:
        v = normalize_venue_local(e.get("location", ""))
        if v:
            c[v] += 1
    return c


def normalize_venue_local(s: str) -> str:
    """Light venue normalisation (avoids importing the heavy digest module
    for the offline self-test). Lowercase, strip a trailing address tail."""
    s = (s or "").strip().lower()
    s = s.split(",")[0].strip()  # "The Sound Room, Camden, London" -> "the sound room"
    return re.sub(r"\s+", " ", s)


def series_candidates(events: list[dict], min_count: int = 2) -> list[tuple[str, int]]:
    c: Counter = Counter(title_stem(e.get("title", "")) for e in events)
    return [(stem, n) for stem, n in c.most_common() if stem and n >= min_count]


def type_tally(events: list[dict]) -> list[tuple[str, int]]:
    c: Counter = Counter(classify_format(e.get("title", ""), "") for e in events)
    return c.most_common()


def bootstrap_payload(events: list[dict], months: int) -> dict:
    dates = sorted(e.get("date", "") for e in events if e.get("date"))
    return {
        "window_months": months,
        "events_attended": len(events),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "event_types": type_tally(events),
        "recurring_venues": venue_frequency(events).most_common(),
        "recurring_series": series_candidates(events),
        "attended": [
            {"title": e.get("title", ""), "date": e.get("date", ""),
             "location": e.get("location", "")}
            for e in sorted(events, key=lambda e: e.get("date", ""))
        ],
    }


def render_report(payload: dict) -> str:
    lines = []
    n = payload["events_attended"]
    rng = payload["date_range"]
    lines.append(f"# Taste bootstrap: {n} events attended"
                 + (f" ({rng[0]} to {rng[1]})" if rng else ""))
    lines.append("")
    lines.append("## Event types you actually go to")
    for t, c in payload["event_types"]:
        lines.append(f"- {t}: {c}")
    lines.append("")
    lines.append("## Places you return to (candidate known venues/organisers)")
    repeats = [(v, c) for v, c in payload["recurring_venues"] if c >= 2]
    for v, c in repeats or [("(none repeated yet)", 0)]:
        lines.append(f"- {v}: {c}" if c else f"- {v}")
    lines.append("")
    lines.append("## Recurring series (candidate KNOWN_SERIES_PATTERNS)")
    for stem, c in payload["recurring_series"] or [("(none yet)", 0)]:
        lines.append(f"- {stem}: {c}" if c else f"- {stem}")
    lines.append("")
    lines.append("## Every event (for the agent to read and cluster)")
    for e in payload["attended"]:
        loc = f" @ {e['location']}" if e["location"] else ""
        lines.append(f"- {e['date']}  {e['title']}{loc}")
    return "\n".join(lines)


# --- main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=12,
                    help="How far back to review your Calendar (default 12)")
    ap.add_argument("--out", default=str(_REPO / ".taste_bootstrap.json"),
                    help="Where to write the JSON payload for the agent step")
    args = ap.parse_args()

    import os  # noqa: PLC0415
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        print("Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN "
              "(the same Calendar OAuth the feedback digest uses). See the "
              "setup guide. Or skip this and use docs/taste-interview.md.",
              file=sys.stderr)
        return 1

    from pipeline.feedback_digest import (  # noqa: PLC0415
        CalendarError,
        dedup_calendar_events,
        fetch_calendar_events,
        refresh_calendar_token,
    )
    try:
        token = refresh_calendar_token(cid, secret, refresh)
        events = dedup_calendar_events(
            fetch_calendar_events(token, days_back=args.months * 30))
    except CalendarError as e:
        print(f"Calendar read failed: {e}", file=sys.stderr)
        return 2

    payload = bootstrap_payload(events, args.months)
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(render_report(payload))
    print(f"\nWrote {payload['events_attended']} events to {args.out}.",
          file=sys.stderr)
    print("Next: in your agent session, say 'draft my taste profile from "
          ".taste_bootstrap.json using docs/taste-bootstrap.md'.", file=sys.stderr)
    return 0


def _self_test() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    check("title_stem strips counter", title_stem("Silent Pages #41"), "silent pages")
    check("title_stem strips date", title_stem("Art Jam 12/06"), "art jam")
    check("title_stem strips paren", title_stem("Vibe Coding (June edition)"), "vibe coding")

    events = [
        {"title": "Silent Pages #40", "date": "2026-01-05", "location": "The Sound Room, Camden"},
        {"title": "Silent Pages #41", "date": "2026-02-05", "location": "The Sound Room, Camden"},
        {"title": "Bouldering Social", "date": "2026-02-10", "location": "The Boulder Room, Hackney"},
        {"title": "Pottery Taster", "date": "2026-03-01", "location": "Clay Studio"},
    ]
    p = bootstrap_payload(events, 6)
    check("counts events", p["events_attended"], 4)
    check("date range", p["date_range"], ["2026-01-05", "2026-03-01"])
    check("the sound room repeats", dict(p["recurring_venues"]).get("the sound room"), 2)
    check("series found", ("silent pages", 2) in p["recurring_series"], True)
    check("no false series", any(s == "pottery taster" for s, _ in p["recurring_series"]), False)
    report = render_report(p)
    check("report has attended list", "Silent Pages #40" in report, True)
    check("report has types section", "Event types you actually go to" in report, True)

    print("All passed" if ok else "FAILURES", file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
