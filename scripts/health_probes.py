"""Credential probes for weekly_run.sh health-check (WP9.2).

Each probe answers one question: is this credential VALID: and the CLI
exits:
  0  credential verified
  1  credential BAD (rejected, expired, revoked, or missing scopes)
  2  probe SKIPPED (creds not in env, or endpoint unreachable: the
     sandbox egress proxy blocks api.github.com and api.notion.com, so
     unreachable is a normal condition here, never a failure)

Usage: python3 scripts/health_probes.py github-scopes|notion|google|gmail

Transports are injectable so tests run offline (tests/test_health_probes.py).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.config import REPO_SLUG  # noqa: E402
from erlib.google_auth import refresh_access_token  # noqa: E402
from erlib.notion import NotionClient, NotionError  # noqa: E402

OK, BAD, SKIP = 0, 1, 2
REQUIRED_PAT_SCOPES = {"repo", "workflow"}


def probe_github_scopes(token: str, *, transport=None) -> tuple[int, str]:
    """Verify the PAT authenticates AND carries repo+workflow scopes."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO_SLUG}")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    opener = transport or urllib.request.urlopen
    try:
        with opener(req, timeout=15) as resp:
            scopes_header = resp.headers.get("x-oauth-scopes")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return BAD, "GitHub PAT rejected (HTTP 401): expired or revoked"
        # 403 here is almost always the sandbox proxy, not GitHub;
        # git ls-remote (step 1 of health-check) already verified auth.
        return SKIP, f"api.github.com returned HTTP {e.code}: likely proxy-blocked"
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        return SKIP, f"api.github.com unreachable ({reason})"
    if scopes_header is None:
        # Fine-grained PATs carry no scopes header; auth itself succeeded.
        return OK, "PAT auth OK (fine-grained token: no scopes header)"
    scopes = {s.strip() for s in scopes_header.split(",") if s.strip()}
    missing = REQUIRED_PAT_SCOPES - scopes
    if missing:
        return BAD, f"GitHub PAT missing scope(s): {', '.join(sorted(missing))}"
    return OK, f"PAT scopes OK ({scopes_header})"


def probe_notion(token: str, *, transport=None) -> tuple[int, str]:
    """Verify the Notion integration token with GET /users/me."""
    client = NotionClient(token, transport=transport, timeout=15)
    try:
        me = client.request("GET", "/users/me")
    except NotionError as e:
        msg = str(e)
        if "HTTP 401" in msg:
            return BAD, "Notion token rejected (HTTP 401): expired or revoked"
        if "network error" in msg:
            return SKIP, "api.notion.com unreachable (sandbox blocks it; validated at write time)"
        return SKIP, f"Notion probe inconclusive: {msg[:120]}"
    name = me.get("name", "?")
    return OK, f"Notion token OK (integration: {name})"


def probe_google(
    client_id: str, client_secret: str, refresh_token: str, *, transport=None
) -> tuple[int, str]:
    """Verify the Google refresh token still mints access tokens."""
    try:
        refresh_access_token(client_id, client_secret, refresh_token, transport=transport)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            return BAD, f"Google refresh token rejected (HTTP {e.code}): re-auth needed"
        return SKIP, f"oauth2.googleapis.com returned HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        return SKIP, f"oauth2.googleapis.com unreachable ({reason})"
    return OK, "Google refresh token OK (access token minted)"


def probe_gmail(token_json: str, *, transport=None) -> tuple[int, str]:
    """Verify GMAIL_TOKEN_JSON parses and its refresh token still mints
    access tokens. Replaces the YAML heredoc that lived in label-emails.yml."""
    try:
        data = json.loads(token_json)
    except json.JSONDecodeError as e:
        return BAD, f"GMAIL_TOKEN_JSON is not valid JSON ({e})"
    if not isinstance(data, dict):
        return BAD, f"GMAIL_TOKEN_JSON must be a JSON object, got {type(data).__name__}"
    if not data.get("refresh_token"):
        return BAD, "GMAIL_TOKEN_JSON has no refresh_token: re-auth needed"
    if not (data.get("client_id") and data.get("client_secret")):
        return BAD, "GMAIL_TOKEN_JSON missing client_id/client_secret"
    try:
        refresh_access_token(
            data["client_id"], data["client_secret"], data["refresh_token"],
            transport=transport,
        )
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            return BAD, f"Gmail refresh token rejected (HTTP {e.code}): re-auth needed"
        return SKIP, f"oauth2.googleapis.com returned HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        return SKIP, f"oauth2.googleapis.com unreachable ({reason})"
    return OK, "Gmail refresh token OK (access token minted)"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("github-scopes", "notion", "google", "gmail"):
        print("usage: health_probes.py github-scopes|notion|google|gmail", file=sys.stderr)
        return SKIP
    which = argv[1]
    if which == "github-scopes":
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            print("SKIP: GITHUB_TOKEN not set in env")
            return SKIP
        code, msg = probe_github_scopes(token)
    elif which == "notion":
        token = os.environ.get("NOTION_TOKEN")
        if not token:
            print("SKIP: NOTION_TOKEN not set in env (GitHub Actions secret: validated at write time)")
            return SKIP
        code, msg = probe_notion(token)
    elif which == "gmail":
        token_json = os.environ.get("GMAIL_TOKEN_JSON")
        if not token_json:
            print("SKIP: GMAIL_TOKEN_JSON not set in env (GitHub Actions secret: probed in label-emails.yml)")
            return SKIP
        code, msg = probe_gmail(token_json)
    else:
        cid = os.environ.get("GOOGLE_CLIENT_ID")
        secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        rt = os.environ.get("GOOGLE_REFRESH_TOKEN")
        if not (cid and secret and rt):
            print("SKIP: Google OAuth creds not in env (GitHub Actions secrets: probed in-Action)")
            return SKIP
        code, msg = probe_google(cid, secret, rt)
    prefix = {OK: "OK", BAD: "FAIL", SKIP: "SKIP"}[code]
    print(f"{prefix}: {msg}")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # noqa: BLE001  a crashed probe must not read as BAD credential
        # Exit 1 means "credential rejected" to weekly_run.sh and fires a
        # high-priority ntfy naming the credential. An internal error
        # (import breakage, unexpected response shape) is a SKIP: the
        # credential was never actually tested.
        print(f"SKIP: probe crashed ({type(e).__name__}: {e}): credential not tested")
        sys.exit(SKIP)
