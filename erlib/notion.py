"""The ONE Notion client for the whole pipeline.

Every script talks to Notion through this module: bounded 429 backoff
(no unbounded recursion), pagination that actually follows next_cursor
(several scripts silently dropped rows past 100), shared property
extractors, and UTF-16-safe truncation (Notion counts rich-text limits
in UTF-16 code units, not Python characters).

The transport is injectable so the whole module tests offline
(tests/test_erlib_notion.py).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from .config import NOTION_API, NOTION_VERSION
import contextlib


class NotionError(RuntimeError):
    """Notion API failure after bounded retries (or non-retryable error)."""


def _default_transport(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)


class NotionClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        transport=None,
        sleep=time.sleep,
        timeout: float = 30,
    ):
        self.token = token or os.environ.get("NOTION_TOKEN", "")
        if not self.token:
            raise NotionError("NOTION_TOKEN is not set")
        self._transport = transport or _default_transport
        self._sleep = sleep
        self._timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        max_retries: int = 3,
    ) -> dict:
        """One API call. Retries 429 up to max_retries times (Retry-After,
        capped at 60s), then raises NotionError. Any other HTTP error raises
        immediately."""
        url = f"{NOTION_API}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Notion-Version", NOTION_VERSION)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with self._transport(req, self._timeout) as resp:
                    body = resp.read().decode()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max_retries:
                    # Retry-After may be an HTTP-date (RFC 7231), not seconds
                    try:
                        retry_after = min(float(e.headers.get("Retry-After", "2") or "2"), 60.0)
                    except ValueError:
                        retry_after = 2.0
                    self._sleep(retry_after)
                    continue
                detail = ""
                with contextlib.suppress(Exception):
                    detail = e.read().decode()
                suffix = f" after {max_retries} retries" if e.code == 429 else ""
                raise NotionError(
                    f"Notion {method} {path} -> HTTP {e.code}{suffix}: {detail}"
                ) from None
            except urllib.error.URLError as e:
                raise NotionError(f"Notion {method} {path} -> network error: {e.reason}") from None
            except (TimeoutError, OSError) as e:
                # Read-phase failures (socket timeout after connect, reset,
                # incomplete read) must surface as NotionError too: callers
                # promise soft-fail semantics on "Notion hiccups".
                raise NotionError(f"Notion {method} {path} -> transport error: {e}") from None
        raise NotionError(f"Notion {method} {path} -> retries exhausted")  # pragma: no cover

    def paginate(
        self, method: str, path: str, payload: dict | None = None
    ) -> Iterator[dict]:
        """Yield every result across all pages (has_more/next_cursor)."""
        base = dict(payload or {})
        cursor: str | None = None
        while True:
            if cursor is None:
                resp = self.request(method, path, base or None)
            elif method.upper() == "GET":
                sep = "&" if "?" in path else "?"
                resp = self.request(method, f"{path}{sep}start_cursor={cursor}")
            else:
                resp = self.request(method, path, {**base, "start_cursor": cursor})
            yield from resp.get("results", [])
            if not resp.get("has_more"):
                return
            cursor = resp.get("next_cursor")
            if not cursor:
                return


# --- Property extractors (take the property dict, tolerate None) ---

def extract_select(prop) -> str | None:
    if not prop:
        return None
    sel = prop.get("select")
    return sel.get("name") if sel else None


def extract_rich_text(prop) -> str:
    if not prop:
        return ""
    return "".join(seg.get("plain_text", "") for seg in prop.get("rich_text", []))


def extract_checkbox(prop) -> bool:
    return bool(prop.get("checkbox")) if prop else False


def extract_date(prop) -> str | None:
    if not prop:
        return None
    d = prop.get("date")
    return d.get("start") if d else None


# Notion API max: 2000 UTF-16 code units per rich_text object.
NOTION_TEXT_LIMIT = 2000


def truncate_utf16(text: str, limit: int = NOTION_TEXT_LIMIT) -> str:
    """Truncate to at most `limit` UTF-16 code units (Notion's unit).

    A surrogate pair split at the boundary is dropped whole rather than
    leaving half a character.
    """
    if not text:
        return ""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[: limit * 2].decode("utf-16-le", errors="ignore")


def truncate_with_ellipsis(text: str, limit: int = NOTION_TEXT_LIMIT) -> str:
    """Truncate to Notion's rich_text limit, appending … only when cut.

    Notion counts UTF-16 code units, not Python characters: emoji count
    as 2. Counting len(s) let emoji-heavy titles through and Notion
    rejected the write with HTTP 400 (audit P2).
    """
    if not text:
        return ""
    cut = truncate_utf16(text, limit)
    if cut == text:
        return text
    return truncate_utf16(text, limit - 1) + "…"
