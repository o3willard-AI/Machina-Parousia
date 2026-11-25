"""MP-6 (part A): couple the browser to the inbox — wait_for_email.

Enrollment routinely needs the confirmation email (double opt-in). Parousia
owns BOTH the inbox and the browser: ``wait_for_email`` polls the caller's
own (account-scoped) inbox until a message matches, and surfaces the links
from its body so the agent can ``browse_to`` the confirmation link
directly — no hand-glued halves.
"""

import asyncio
import re
from typing import Awaitable, Callable, Optional

from parousia.inbox.inbox_store import InboxStore

# Poll cadence: bounded poll loop, never blocks the gateway's event loop
# (``await asyncio.sleep`` yields). 2s between checks matches the brief's
# bounded-loop guidance.
DEFAULT_POLL_SECONDS = 2.0

# timeout_ms bounds: default 30s, hard cap 120s (the brief's cap).
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 120_000

# http(s) URLs in plain text / HTML bodies.
_LINK_RE = re.compile(r"https?://[^\s'\"<>\[\]]+")

# Trailing punctuation commonly glued onto links in text bodies; stripped
# so sentence-ending links still resolve.
_LINK_TRAILING_PUNCT = ".,;:!?)]}"

# Scan window per poll: the newest messages are what an enrollment waits
# for; a bounded window keeps each poll cheap on busy inboxes.
_SCAN_LIMIT = 50


def extract_links(body_text: str, body_html: Optional[str] = None) -> list[str]:
    """Extract http(s) URLs from a message body (text first, then HTML).

    Returns unique links in order of first appearance. HTML is scanned as
    a second source (``<a href>`` values may not appear in the text part);
    href attributes are handled by the same URL pattern — tags themselves
    never match because ``<`` and ``>`` are excluded from the pattern.
    """
    out: list[str] = []
    seen: set[str] = set()

    def scan(source: Optional[str]) -> None:
        if not source:
            return
        for match in _LINK_RE.findall(source):
            link = match.rstrip(_LINK_TRAILING_PUNCT)
            if link and link not in seen:
                seen.add(link)
                out.append(link)

    scan(body_text)
    scan(body_html)
    return out


def _matches(message, pattern_lower: str) -> bool:
    """Case-insensitive substring match on subject OR sender."""
    if pattern_lower in (message.subject or "").lower():
        return True
    if pattern_lower in (message.sender or "").lower():
        return True
    return False


async def wait_for_email(
    inbox_store: InboxStore,
    agent_id: str,
    pattern: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> dict:
    """Poll the agent's own inbox until a message matches ``pattern``.

    Args:
        inbox_store: the store to poll (the caller's own, account-scoped —
            ``list_messages`` filters by ``agent_id``, so another agent's
            matching mail is invisible here by construction).
        agent_id: which inbox to poll.
        pattern: case-insensitive substring matched against subject OR
            sender.
        timeout_ms: overall bound (default 30s, hard cap 120s).
        poll_seconds: cadence between checks.
        sleep: injectable sleep (tests pass a no-op so the loop is
            instant); defaults to ``asyncio.sleep``.

    Returns:
        ``{"found": True, "message": {...}, "links": [...]}`` on match —
        the message carries id/sender/subject/body/received_at plus the
        extracted ``links`` so the agent can ``browse_to`` the
        confirmation link directly — or ``{"found": False}`` on timeout.
    """
    if not pattern or not pattern.strip():
        return {"error": "pattern is required"}
    _sleep = sleep or asyncio.sleep
    deadline = asyncio.get_event_loop().time() + max(
        0, min(int(timeout_ms or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)
    ) / 1000.0

    pattern_lower = pattern.strip().lower()
    while True:
        # Account-scoped by construction: list_messages(agent_id) filters
        # the store to this agent's mail only.
        messages = inbox_store.list_messages(
            agent_id, limit=_SCAN_LIMIT, unread_only=False
        )
        for message in messages:
            if _matches(message, pattern_lower):
                links = extract_links(message.body_text, message.body_html)
                return {
                    "found": True,
                    "message": {
                        "id": message.id,
                        "sender": message.sender,
                        "recipient": message.recipient,
                        "subject": message.subject,
                        "body_text": message.body_text,
                        "body_html": message.body_html,
                        "received_at": message.received_at,
                        "read": message.read,
                        "archived": message.archived,
                    },
                    "links": links,
                }
        if asyncio.get_event_loop().time() >= deadline:
            return {"found": False}
        await _sleep(poll_seconds)