"""Offline tests for scripts/health_probes.py (WP9.2).

Every network path is exercised through injected transports: no real
HTTP. Pins the OK / BAD / SKIP contract that weekly_run.sh health-check
maps onto exit codes and per-credential ntfy alerts.
"""
import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.health_probes import (  # noqa: E402
    BAD,
    OK,
    SKIP,
    probe_github_scopes,
    probe_gmail,
    probe_google,
    probe_notion,
)


class FakeResponse:
    def __init__(self, body: dict | None = None, headers: dict | None = None):
        self._body = json.dumps(body or {}).encode()
        self.headers = _Headers(headers or {})

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Headers(dict):
    def get(self, key, default=None):  # header lookup is case-insensitive
        for k, v in self.items():
            if k.lower() == str(key).lower():
                return v
        return default


def http_error(code: int, body: bytes = b"{}"):
    return urllib.error.HTTPError(
        "https://x", code, "err", hdrs=None, fp=io.BytesIO(body)
    )


# --- GitHub scopes ---------------------------------------------------------

def test_github_scopes_ok():
    def transport(req, timeout=None):
        return FakeResponse(headers={"x-oauth-scopes": "repo, workflow, gist"})

    code, msg = probe_github_scopes("ghp_x", transport=transport)
    assert code == OK and "repo" in msg


def test_github_scopes_missing_workflow():
    def transport(req, timeout=None):
        return FakeResponse(headers={"x-oauth-scopes": "repo"})

    code, msg = probe_github_scopes("ghp_x", transport=transport)
    assert code == BAD and "workflow" in msg


def test_github_401_is_bad():
    def transport(req, timeout=None):
        raise http_error(401)

    code, msg = probe_github_scopes("ghp_x", transport=transport)
    assert code == BAD and "401" in msg


def test_github_proxy_403_is_skip():
    def transport(req, timeout=None):
        raise http_error(403)

    code, _ = probe_github_scopes("ghp_x", transport=transport)
    assert code == SKIP


def test_github_unreachable_is_skip():
    def transport(req, timeout=None):
        raise urllib.error.URLError("proxy refused")

    code, msg = probe_github_scopes("ghp_x", transport=transport)
    assert code == SKIP and "unreachable" in msg


def test_github_fine_grained_no_header_is_ok():
    def transport(req, timeout=None):
        return FakeResponse(headers={})

    code, msg = probe_github_scopes("github_pat_x", transport=transport)
    assert code == OK and "fine-grained" in msg


# --- Notion ----------------------------------------------------------------

def test_notion_ok():
    def transport(req, timeout=None):
        assert "users/me" in req.full_url
        return FakeResponse(body={"name": "Event Recommender"})

    code, msg = probe_notion("secret_x", transport=transport)
    assert code == OK and "Event Recommender" in msg


def test_notion_401_is_bad():
    def transport(req, timeout=None):
        raise http_error(401)

    code, msg = probe_notion("secret_x", transport=transport)
    assert code == BAD and "401" in msg


def test_notion_unreachable_is_skip():
    def transport(req, timeout=None):
        raise urllib.error.URLError("blocked")

    code, msg = probe_notion("secret_x", transport=transport)
    assert code == SKIP and "write time" in msg


# --- Google ----------------------------------------------------------------

def test_google_ok():
    def transport(req, timeout=None):
        return FakeResponse(body={"access_token": "ya29.x"})

    code, msg = probe_google("cid", "sec", "rt", transport=transport)
    assert code == OK


def test_google_invalid_grant_is_bad():
    def transport(req, timeout=None):
        raise http_error(400, b'{"error": "invalid_grant"}')

    code, msg = probe_google("cid", "sec", "rt", transport=transport)
    assert code == BAD and "re-auth" in msg


def test_google_unreachable_is_skip():
    def transport(req, timeout=None):
        raise urllib.error.URLError("no route")

    code, _ = probe_google("cid", "sec", "rt", transport=transport)
    assert code == SKIP


# --- Gmail (GMAIL_TOKEN_JSON) ------------------------------------------------

GMAIL_JSON = json.dumps({
    "token": "stale", "refresh_token": "rt",
    "client_id": "cid", "client_secret": "sec",
})


def test_gmail_ok():
    def transport(req, timeout=None):
        return FakeResponse(body={"access_token": "ya29.x"})

    code, msg = probe_gmail(GMAIL_JSON, transport=transport)
    assert code == OK


def test_gmail_not_json_is_bad():
    code, msg = probe_gmail("{not json", transport=None)
    assert code == BAD and "not valid JSON" in msg


def test_gmail_json_scalar_is_bad():
    # Double-encoded secret (a JSON string) must be BAD, not a crash
    code, msg = probe_gmail('"{\\"refresh_token\\": \\"rt\\"}"', transport=None)
    assert code == BAD and "JSON object" in msg


def test_gmail_no_refresh_token_is_bad():
    code, msg = probe_gmail(json.dumps({"client_id": "cid", "client_secret": "sec"}))
    assert code == BAD and "re-auth" in msg


def test_gmail_missing_client_creds_is_bad():
    code, msg = probe_gmail(json.dumps({"refresh_token": "rt"}))
    assert code == BAD and "client_id" in msg


def test_gmail_expired_is_bad():
    def transport(req, timeout=None):
        raise http_error(400, b'{"error": "invalid_grant"}')

    code, msg = probe_gmail(GMAIL_JSON, transport=transport)
    assert code == BAD and "re-auth" in msg


def test_gmail_unreachable_is_skip():
    def transport(req, timeout=None):
        raise urllib.error.URLError("no route")

    code, _ = probe_gmail(GMAIL_JSON, transport=transport)
    assert code == SKIP
