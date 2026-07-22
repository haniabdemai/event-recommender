#!/usr/bin/env python3
"""Offline tests for erlib.notion (WP3 task 3.3).

Run: python3 tests/test_erlib_notion.py
The transport is injected: no network is touched.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from erlib.notion import (  # noqa: E402
    NOTION_TEXT_LIMIT,
    NotionClient,
    NotionError,
    extract_checkbox,
    extract_date,
    extract_rich_text,
    extract_select,
    truncate_utf16,
    truncate_with_ellipsis,
)


class FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://api.notion.com/v1/x", code, "err", headers, io.BytesIO(b"{}")
    )


class ScriptedTransport:
    """Returns/raises each scripted step in order; repeats the last one."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, req, timeout):
        self.calls.append(req)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return FakeResponse(step)


def main() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  OK  {label}")
        else:
            ok = False
            print(f"  FAIL {label}: got {got!r}, want {want!r}")

    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)

    # 1. 429 then 200 -> succeeds, one sleep of the Retry-After value
    t = ScriptedTransport([http_error(429, "1"), {"ok": True}])
    c = NotionClient("tok", transport=t, sleep=fake_sleep)
    check("429-then-200 returns body", c.request("GET", "/pages/p1"), {"ok": True})
    check("one retry sleep, Retry-After honoured", sleeps, [1.0])

    # 2. Persistent 429 -> bounded: raises after max_retries, never recurses
    t = ScriptedTransport([http_error(429, "0")])
    c = NotionClient("tok", transport=t, sleep=fake_sleep)
    try:
        c.request("GET", "/pages/p1", max_retries=3)
        check("persistent 429 raises", "no exception", "NotionError")
    except NotionError as e:
        check("persistent 429 raises", True, True)
        check("error names 429 + retries", "429" in str(e) and "after 3 retries" in str(e), True)
    check("bounded attempts (1 + 3 retries)", len(t.calls), 4)

    # 3. Retry-After capped at 60s
    sleeps.clear()
    t = ScriptedTransport([http_error(429, "999"), {"ok": True}])
    c = NotionClient("tok", transport=t, sleep=fake_sleep)
    c.request("GET", "/x")
    check("Retry-After capped at 60", sleeps, [60.0])

    # 4. Non-429 HTTP error raises immediately, no sleep
    sleeps.clear()
    t = ScriptedTransport([http_error(404)])
    c = NotionClient("tok", transport=t, sleep=fake_sleep)
    try:
        c.request("GET", "/pages/gone")
        check("404 raises", "no exception", "NotionError")
    except NotionError as e:
        check("404 raises with code in message", "404" in str(e), True)
    check("404 does not retry", (len(t.calls), sleeps), (1, []))

    # 5. paginate follows has_more/next_cursor across 3 pages
    pages = [
        {"results": [{"i": 1}, {"i": 2}], "has_more": True, "next_cursor": "c2"},
        {"results": [{"i": 3}], "has_more": True, "next_cursor": "c3"},
        {"results": [{"i": 4}], "has_more": False, "next_cursor": None},
    ]

    class PagingTransport(ScriptedTransport):
        def __call__(self, req, timeout):
            self.calls.append(req)
            return FakeResponse(self.steps[len(self.calls) - 1])

    t = PagingTransport(pages)
    c = NotionClient("tok", transport=t, sleep=fake_sleep)
    got = list(c.paginate("POST", "/data_sources/x/query", {"page_size": 100}))
    check("paginate yields all rows", [g["i"] for g in got], [1, 2, 3, 4])
    check("paginate made 3 calls", len(t.calls), 3)
    second_payload = json.loads(t.calls[1].data.decode())
    check("cursor threaded into POST payload", second_payload.get("start_cursor"), "c2")

    # 6. missing token
    import os
    saved = os.environ.pop("NOTION_TOKEN", None)
    try:
        NotionClient()
        check("empty token raises", "no exception", "NotionError")
    except NotionError:
        check("empty token raises", True, True)
    finally:
        if saved is not None:
            os.environ["NOTION_TOKEN"] = saved

    # 7. truncate_utf16
    check("ascii unchanged", truncate_utf16("hello", 2000), "hello")
    emoji = "🎉" * 1500  # 3000 UTF-16 units
    out = truncate_utf16(emoji, 2000)
    check("emoji truncated to ≤2000 units", len(out.encode("utf-16-le")) <= 4000, True)
    check("no broken surrogate", out, "🎉" * 1000)
    check("None-ish gives empty", truncate_utf16(""), "")

    # 7b. truncate_with_ellipsis (the Phase 2 hoist from write_notion/_notion_text)
    check("ellipsis: short text unchanged", truncate_with_ellipsis("hello"), "hello")
    long = "x" * (NOTION_TEXT_LIMIT + 50)
    cut = truncate_with_ellipsis(long)
    check("ellipsis appended when cut", cut.endswith("…"), True)
    check("ellipsis result within limit",
          len(cut.encode("utf-16-le")) <= NOTION_TEXT_LIMIT * 2, True)
    check("ellipsis: exactly-at-limit unchanged",
          truncate_with_ellipsis("y" * NOTION_TEXT_LIMIT), "y" * NOTION_TEXT_LIMIT)
    check("ellipsis: empty gives empty", truncate_with_ellipsis(""), "")

    # 8. extractors
    check("select", extract_select({"select": {"name": "Going"}}), "Going")
    check("select empty", extract_select({"select": None}), None)
    check("select missing prop", extract_select(None), None)
    check("rich_text joins segments",
          extract_rich_text({"rich_text": [{"plain_text": "a"}, {"plain_text": "b"}]}), "ab")
    check("rich_text empty", extract_rich_text({"rich_text": []}), "")
    check("checkbox", extract_checkbox({"checkbox": True}), True)
    check("checkbox missing", extract_checkbox(None), False)
    check("date", extract_date({"date": {"start": "2026-07-04"}}), "2026-07-04")
    check("date empty", extract_date({"date": None}), None)

    print("OK: erlib.notion tests passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
