"""Postfix pipe ingest — reads raw email from stdin, parses MIME, POSTs to REST server.

Called by Postfix alias: agent: "|/usr/local/bin/parousia-guard ingest"
"""

import email
import json
import sys
import urllib.request
from email.policy import default


def main():
    """Read raw RFC 822 email from stdin, parse, and forward to REST ingress."""
    raw_email = sys.stdin.read()
    if not raw_email.strip():
        print("Empty stdin — no email content", file=sys.stderr)
        sys.exit(0)  # Don't bounce — Postfix may retry on non-zero

    try:
        msg = email.message_from_string(raw_email, policy=default)
    except Exception as e:
        print(f"Failed to parse MIME: {e}", file=sys.stderr)
        sys.exit(75)  # EX_TEMPFAIL — Postfix will retry

    # Extract headers
    sender = str(msg.get("From", ""))
    recipient = str(msg.get("To", ""))
    subject = str(msg.get("Subject", ""))

    # Extract agent_id from recipient (local part before @)
    agent_id = recipient.split("@")[0].strip().lower() if "@" in recipient else recipient.strip().lower()

    # Extract plain text body
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    body = part.get_content()
                except Exception:
                    # If get_content() fails, try get_payload(decode=True)
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")
                break
    else:
        try:
            body = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")

    # Build payload for REST endpoint
    payload = {
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "body": body.strip() if body else "",
        "agent_id": agent_id,
        "raw_mime": raw_email,
    }

    # POST to local REST server
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:8080/ingest",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                sys.exit(0)
            else:
                print(f"REST server returned {resp.status}", file=sys.stderr)
                sys.exit(75)
    except urllib.error.URLError as e:
        print(f"REST server unreachable: {e}", file=sys.stderr)
        sys.exit(75)  # EX_TEMPFAIL — Postfix retries
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(75)
