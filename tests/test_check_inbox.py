"""Tests for check_inbox MCP tool (Story F)."""

import json
import tempfile
import os
from unittest import mock

import pytest
from mcp.types import TextContent

from parousia.inbox.inbox_store import InboxStore, InboxMessage


@pytest.fixture
def test_inbox():
    """InboxStore backed by a temp SQLite DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    store = InboxStore(db_path)
    yield store
    os.unlink(db_path)


def make_message(**overrides):
    """Create an InboxMessage with sensible defaults."""
    from datetime import datetime
    defaults = {
        "id": "msg-1",
        "agent_id": "test-agent",
        "sender": "sender@example.com",
        "recipient": "test-agent@machinaparousia.ai",
        "subject": "Test Subject",
        "body_text": "Test body text",
        "received_at": datetime.utcnow().isoformat() + "Z",
        "read": False,
        "archived": False,
    }
    defaults.update(overrides)
    return InboxMessage(**defaults)


def test_check_inbox_returns_messages(test_inbox):
    """check_inbox handler returns stored messages."""
    store = test_inbox
    msg1 = make_message(id="msg-1")
    msg2 = make_message(id="msg-2", subject="Second message")
    store.store_message(msg1)
    store.store_message(msg2)

    messages = store.list_messages("test-agent", limit=10, unread_only=False)
    assert len(messages) == 2
    assert messages[0].id == "msg-2"  # newest first
    assert messages[1].id == "msg-1"


def test_check_inbox_unread_only(test_inbox):
    """unread_only=True filters read messages."""
    store = test_inbox
    store.store_message(make_message(id="unread", read=False))
    store.store_message(make_message(id="read", read=True))

    messages = store.list_messages("test-agent", limit=10, unread_only=True)
    assert len(messages) == 1
    assert messages[0].id == "unread"


def test_check_inbox_empty(test_inbox):
    """Query with no messages returns empty list."""
    store = test_inbox
    messages = store.list_messages("no-such-agent", limit=10, unread_only=False)
    assert messages == []
    assert len(messages) == 0


def test_check_inbox_limit(test_inbox):
    """limit parameter restricts result count."""
    store = test_inbox
    for i in range(5):
        store.store_message(make_message(id=f"msg-{i}"))

    messages = store.list_messages("test-agent", limit=3, unread_only=False)
    assert len(messages) == 3


def test_check_inbox_agent_isolation(test_inbox):
    """Messages are scoped to agent_id."""
    store = test_inbox
    store.store_message(make_message(id="a1", agent_id="agent-a", subject="Hello A"))
    store.store_message(make_message(id="b1", agent_id="agent-b", subject="Hello B"))

    a_messages = store.list_messages("agent-a", limit=10, unread_only=False)
    assert len(a_messages) == 1
    assert a_messages[0].subject == "Hello A"


def test_count_unread(test_inbox):
    """count_unread returns correct tally."""
    store = test_inbox
    store.store_message(make_message(id="u1", read=False))
    store.store_message(make_message(id="u2", read=False))
    store.store_message(make_message(id="r1", read=True))

    assert store.count_unread("test-agent") == 2
