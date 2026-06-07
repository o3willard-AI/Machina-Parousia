"""Tests for human-in-the-loop approval queue."""

import pytest
import fakeredis
from parousia.guard.approval_queue import ApprovalQueue


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis()


@pytest.fixture
def queue(redis_client):
    return ApprovalQueue(redis_client)


def test_enqueue_returns_approval_id(queue):
    aid = queue.enqueue("hermes", "test@example.com", "Subject", "Body", "hermes@domain.com")
    assert aid
    assert len(aid) == 12


def test_list_pending(queue):
    queue.enqueue("hermes", "a@b.com", "S1", "B1", "h@d.com")
    queue.enqueue("mr-krabs", "c@d.com", "S2", "B2", "mk@d.com")
    items = queue.list_pending()
    assert len(items) == 2
    assert items[0]["status"] == "pending"


def test_approve(queue):
    aid = queue.enqueue("hermes", "test@example.com", "Subject", "Body", "h@d.com")
    item = queue.approve(aid)
    assert item is not None
    assert item["status"] == "approved"
    assert item["approved_at"] is not None
    # Queue should be empty after approval
    assert len(queue.list_pending()) == 0


def test_reject(queue):
    aid = queue.enqueue("hermes", "test@example.com", "Subject", "Body", "h@d.com")
    item = queue.reject(aid, "Spam")
    assert item["status"] == "rejected"
    assert item["reject_reason"] == "Spam"
    assert len(queue.list_pending()) == 0


def test_double_approve_is_noop(queue):
    aid = queue.enqueue("hermes", "test@example.com", "Subject", "Body", "h@d.com")
    first = queue.approve(aid)
    assert first is not None
    second = queue.approve(aid)
    assert second is None  # Already processed


def test_double_reject_is_noop(queue):
    aid = queue.enqueue("hermes", "test@example.com", "Subject", "Body", "h@d.com")
    first = queue.reject(aid, "reason")
    assert first is not None
    second = queue.reject(aid)
    assert second is None


def test_get_item(queue):
    aid = queue.enqueue("hermes", "x@y.com", "Sub", "Bod", "h@d.com")
    item = queue.get_item(aid)
    assert item is not None
    assert item["agent_id"] == "hermes"
    assert item["to"] == "x@y.com"


def test_get_nonexistent_item(queue):
    assert queue.get_item("nonexistent") is None


def test_list_pending_empty(queue):
    assert queue.list_pending() == []


def test_reply_to_saved(queue):
    aid = queue.enqueue("hermes", "x@y.com", "Sub", "Bod", "h@d.com", reply_to="admin@d.com")
    item = queue.get_item(aid)
    assert item["reply_to"] == "admin@d.com"
