#!/usr/bin/env python3
"""
Move processed newsletter emails to a "to be deleted" folder in Gmail.
Reads message IDs from .emails_to_label.json, applies the label and
removes the threads from the inbox.

Used by the label-emails.yml GitHub Action. OAuth credentials come from
the GMAIL_TOKEN_JSON environment variable (an Actions secret, never kept
in .env: see .env.example). The target label is configurable via
ER_GMAIL_LABEL_NAME (default "for review-to be deleted") and is created
if it doesn't exist yet, so no hardcoded label id is needed.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from erlib.config import NTFY_TOPIC  # noqa: E402

LABEL_NAME = os.environ.get("ER_GMAIL_LABEL_NAME", "for review-to be deleted")


def get_gmail_service():
    token_json = os.environ.get("GMAIL_TOKEN_JSON")
    if not token_json:
        print("ERROR: GMAIL_TOKEN_JSON env var not set", file=sys.stderr)
        sys.exit(1)

    token_data = json.loads(token_json)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    # Always refresh: the stored access token is likely expired (secrets are static)
    if creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            msg = f"Gmail label Action: token refresh failed ({e}). Re-auth needed."
            print(f"ERROR: {msg}", file=sys.stderr)
            if NTFY_TOPIC:
                try:
                    req = urllib.request.Request(
                        f"https://ntfy.sh/{NTFY_TOPIC}",
                        data=msg.encode(),
                        headers={"Title": "Event Recommender: Gmail token expired"},
                    )
                    urllib.request.urlopen(req)
                except Exception:
                    pass
            sys.exit(1)
    return build("gmail", "v1", credentials=creds)


def resolve_label_id(service, name: str) -> str:
    """Return the id of the named label, creating the label if absent."""
    existing = service.users().labels().list(userId="me").execute()
    for label in existing.get("labels", []):
        if label.get("name") == name:
            return label["id"]
    created = service.users().labels().create(
        userId="me", body={"name": name},
    ).execute()
    print(f"Created Gmail label '{name}'")
    return created["id"]


def main():
    label_file = ".emails_to_label.json"
    if not os.path.exists(label_file):
        print("No .emails_to_label.json found: nothing to move.")
        return

    with open(label_file) as f:
        data = json.load(f)

    message_ids = data.get("message_ids", []) if isinstance(data, dict) else data

    if not message_ids:
        print("Empty list: nothing to move.")
        return

    print(f"Moving {len(message_ids)} email(s) to '{LABEL_NAME}' folder...")
    service = get_gmail_service()
    label_id = resolve_label_id(service, LABEL_NAME)
    success = 0
    errors = 0

    for mid in message_ids:
        try:
            service.users().threads().modify(
                userId="me",
                id=mid,
                body={"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
            ).execute()
            success += 1
        except Exception as e:
            print(f"  FAIL {mid}: {e}", file=sys.stderr)
            errors += 1

    print(f"Done: {success} moved, {errors} errors.")
    os.remove(label_file)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
