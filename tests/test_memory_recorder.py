"""Unit tests for Parousia MemoryRecorder and fact formatters."""
import json
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from parousia.memory.recorder import (
    MemoryRecorder,
    _FACT_FORMATTERS,
    _fmt_send_email,
    _fmt_check_inbox,
    _fmt_schedule_event,
    _fmt_cancel_event,
    _fmt_set_timer_alarm,
    _fmt_nominate_milestone,
    _fmt_resolve_conflicts,
    _fmt_get_temporal_context,
    _fmt_browse_to,
    _fmt_interact,
    _fmt_extract_page_state,
)
from parousia.memory.config import Mem0Config


# ── Fact Formatter Tests ──────────────────────────

class TestSendEmailFormatter:
    def test_success(self):
        result = _fmt_send_email(
            {"to": "a@b.com", "subject": "Hello"},
            {"sent": True, "message_id": "abc"},
            "test-agent",
        )
        assert 'a@b.com' in result
        assert 'Hello' in result

    def test_failure(self):
        result = _fmt_send_email(
            {"to": "a@b.com", "subject": "Hi"},
            {"sent": False, "error": "timeout"},
            "test-agent",
        )
        assert "Failed" in result
        assert "timeout" in result

    def test_queued_for_approval(self):
        result = _fmt_send_email(
            {"to": "a@b.com", "subject": "Approval needed"},
            {"sent": False, "queued_for_approval": True},
            "test-agent",
        )
        assert "Queued" in result
        assert "approval" in result


class TestCheckInboxFormatter:
    def test_with_unread(self):
        result = _fmt_check_inbox(
            {},
            {"messages": [
                {"sender": "alice@x.com", "read": False},
                {"sender": "bob@x.com", "read": True},
                {"sender": "alice@x.com", "read": False},
            ]},
            "test-agent",
        )
        assert result is not None
        assert "2 unread" in result
        assert "alice" in result

    def test_empty(self):
        result = _fmt_check_inbox({}, {"messages": []}, "test-agent")
        assert result is None

    def test_all_read(self):
        result = _fmt_check_inbox(
            {},
            {"messages": [{"sender": "x@y.com", "read": True}]},
            "test-agent",
        )
        assert result is None


class TestScheduleEventFormatter:
    def test_with_conflicts(self):
        result = _fmt_schedule_event(
            {"title": "Standup", "start_time": "2026-06-23T09:00:00Z", "flexibility": "low"},
            {"conflicts": [{"id": 1}], "recorded": True},
            "test-agent",
        )
        assert "Standup" in result
        assert "low flexibility" in result
        assert "1 conflict" in result

    def test_no_conflicts(self):
        result = _fmt_schedule_event(
            {"title": "Review", "start_time": "2026-06-24T10:00:00Z", "flexibility": "high"},
            {"conflicts": [], "recorded": True},
            "test-agent",
        )
        assert "Review" in result
        assert "conflict" not in result


class TestCancelEventFormatter:
    def test_cancel(self):
        result = _fmt_cancel_event(
            {"event_id": "evt-123"},
            {"title": "My Event"},
            "test-agent",
        )
        assert "Cancelled" in result
        assert "My Event" in result

    def test_cancel_no_title_in_result(self):
        result = _fmt_cancel_event(
            {"event_id": "evt-456"},
            {},
            "test-agent",
        )
        assert "Cancelled" in result
        assert "evt-456" in result


class TestSetTimerAlarmFormatter:
    def test_timer(self):
        result = _fmt_set_timer_alarm(
            {"title": "Check build"},
            {"type": "timer", "remaining_seconds": 1800},
            "test-agent",
        )
        assert "Check build" in result
        assert "30 min" in result


class TestNominateMilestoneFormatter:
    def test_with_date(self):
        result = _fmt_nominate_milestone(
            {"title": "v1.0", "entry_type": "release", "occurred_at": "2026-06-20"},
            {},
            "test-agent",
        )
        assert "v1.0" in result
        assert "release" in result
        assert "2026-06-20" in result

    def test_without_date(self):
        result = _fmt_nominate_milestone(
            {"title": "Decision X", "entry_type": "decision"},
            {},
            "test-agent",
        )
        assert "Decision X" in result
        assert "decision" in result


class TestResolveConflictsFormatter:
    def test_resolve(self):
        result = _fmt_resolve_conflicts(
            {},
            {"moved": 3, "skipped": 1},
            "test-agent",
        )
        assert "3 moved" in result
        assert "1 skipped" in result


class TestGetTemporalContextFormatter:
    def test_skipped(self):
        result = _fmt_get_temporal_context({}, {}, "test-agent")
        assert result is None


class TestBrowseToFormatter:
    def test_success(self):
        result = _fmt_browse_to(
            {"url": "https://example.com"},
            {},
            "test-agent",
        )
        assert "https://example.com" in result

    def test_error_skipped(self):
        result = _fmt_browse_to(
            {"url": "https://fail.com"},
            {"error": "timeout"},
            "test-agent",
        )
        assert result is None


class TestInteractFormatter:
    def test_click(self):
        result = _fmt_interact(
            {"action": "click", "id": "btn-submit"},
            {},
            "test-agent",
        )
        assert "btn-submit" in result

    def test_type(self):
        result = _fmt_interact(
            {"action": "type", "id": "input-email", "text": "hello@world.com"},
            {},
            "test-agent",
        )
        assert "input-email" in result
        assert "hello@world.com" in result

    def test_error_skipped(self):
        result = _fmt_interact(
            {"action": "click", "id": "btn"},
            {"error": "not found"},
            "test-agent",
        )
        assert result is None


class TestExtractPageStateFormatter:
    def test_skipped(self):
        result = _fmt_extract_page_state({}, {}, "test-agent")
        assert result is None


# ── Formatter Registry Tests ──────────────────────

def test_all_formatters_registered():
    """Verify all 11 tools have formatters."""
    expected = {
        "send_email", "check_inbox",
        "schedule_event", "cancel_event", "set_timer_alarm",
        "nominate_milestone", "resolve_conflicts", "get_temporal_context",
        "browse_to", "interact", "extract_page_state",
    }
    assert set(_FACT_FORMATTERS.keys()) == expected


# ── MemoryRecorder Tests ──────────────────────────

class TestMemoryRecorder:
    def test_record_tool_call_unknown_tool(self):
        """Unknown tool names are silently skipped."""
        recorder = MemoryRecorder(Mem0Config())
        recorder.record_tool_call("nonexistent_tool", {}, {}, "agent1")
        # Should not raise

    def test_user_id_prefixed(self, monkeypatch):
        """Verify user_id uses parousia- prefix."""
        recorder = MemoryRecorder(Mem0Config(user_id_prefix="parousia-"))
        mock_memory = MagicMock()
        recorder._get_memory = lambda: mock_memory

        recorder.record_tool_call(
            "schedule_event",
            {"title": "Test", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
            {"conflicts": [], "recorded": True},
            "hermes",
        )
        # Wait for background thread
        if recorder._sync_thread:
            recorder._sync_thread.join(timeout=2.0)

        args, kwargs = mock_memory.add.call_args
        assert kwargs["user_id"] == "parousia-hermes"
        assert kwargs["agent_id"] == "parousia"

    def test_background_thread_is_daemon(self):
        """Verify writes happen on a daemon thread."""
        recorder = MemoryRecorder(Mem0Config())
        mock_memory = MagicMock()
        recorder._get_memory = lambda: mock_memory

        recorder.record_tool_call(
            "schedule_event",
            {"title": "T", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
            {"conflicts": [], "recorded": True},
            "agent",
        )
        assert recorder._sync_thread is not None
        assert recorder._sync_thread.daemon is True
        recorder._sync_thread.join(timeout=2.0)

    def test_fire_and_forget_returns_immediately(self):
        """record_tool_call returns without waiting for Mem0."""
        recorder = MemoryRecorder(Mem0Config())
        # Mock _get_memory to block for 2 seconds
        latch = threading.Event()

        def slow_memory():
            latch.wait()
            return MagicMock()

        recorder._get_memory = slow_memory

        start = time.monotonic()
        recorder.record_tool_call(
            "schedule_event",
            {"title": "T", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
            {"conflicts": [], "recorded": True},
            "agent",
        )
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"record_tool_call took {elapsed:.2f}s, expected < 0.5s"
        latch.set()  # Release the thread
        if recorder._sync_thread:
            recorder._sync_thread.join(timeout=2.0)

    def test_circuit_breaker_opens(self):
        """After 5 failures, circuit breaker opens and skips writes."""
        recorder = MemoryRecorder(Mem0Config())
        call_count = [0]

        def failing_memory():
            call_count[0] += 1
            raise RuntimeError("Mem0 down")

        recorder._get_memory = failing_memory

        # Cause 5 failures
        for i in range(5):
            recorder.record_tool_call(
                "schedule_event",
                {"title": f"T{i}", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
                {"conflicts": [], "recorded": True},
                "agent",
            )
            if recorder._sync_thread:
                recorder._sync_thread.join(timeout=2.0)

        assert recorder._consecutive_failures == 5
        assert recorder._is_breaker_open()

        # 6th call should be skipped without calling memory
        before = call_count[0]
        recorder.record_tool_call(
            "schedule_event",
            {"title": "T6", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
            {"conflicts": [], "recorded": True},
            "agent",
        )
        assert call_count[0] == before  # No new call

    def test_circuit_breaker_resets(self):
        """After cooldown, writes resume."""
        recorder = MemoryRecorder(Mem0Config())
        call_count = [0]

        def failing_memory():
            call_count[0] += 1
            raise RuntimeError("Mem0 down")

        recorder._get_memory = failing_memory

        # Cause 5 failures
        for i in range(5):
            recorder.record_tool_call(
                "schedule_event",
                {"title": f"T{i}", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
                {"conflicts": [], "recorded": True},
                "agent",
            )
            if recorder._sync_thread:
                recorder._sync_thread.join(timeout=2.0)

        assert recorder._is_breaker_open()

        # Manually reset breaker (simulate cooldown)
        recorder._breaker_open_until = 0.0

        # Now a call should go through (though it will fail again)
        recorder.record_tool_call(
            "schedule_event",
            {"title": "Post-recovery", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
            {"conflicts": [], "recorded": True},
            "agent",
        )
        if recorder._sync_thread:
            recorder._sync_thread.join(timeout=2.0)
        # Should have been attempted
        assert call_count[0] == 6

    def test_shutdown(self):
        """Shutdown waits for pending writes."""
        recorder = MemoryRecorder(Mem0Config())
        latch = threading.Event()
        done = threading.Event()

        def slow_memory():
            latch.wait()
            done.set()
            return MagicMock()

        recorder._get_memory = slow_memory

        recorder.record_tool_call(
            "schedule_event",
            {"title": "T", "start_time": "2026-01-01T00:00:00Z", "flexibility": "high"},
            {"conflicts": [], "recorded": True},
            "agent",
        )
        assert recorder._sync_thread is not None
        latch.set()
        recorder.shutdown()
        assert done.is_set() or not recorder._sync_thread.is_alive()
