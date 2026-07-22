"""Google OAuth token refresh: shared by sync_to_gcal and feedback_digest.

Transport is injectable so tests run offline.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    transport=None,
    timeout: float = 15,
) -> str:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = transport or urllib.request.urlopen
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read())["access_token"]
