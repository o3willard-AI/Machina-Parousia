"""Tests for MCP temporal tools."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from parousia.config import ParousiaConfig
from parousia.temporal.db import TemporalDB
from parousia.temporal.tools import (
    ALL_TEMPORAL_SCHEMAS,
    TemporalToolHandlers,
)


class TestTemporalToolSchemas:
    """Verify all 5 temporal tool schemas are present and valid."""

    def test_all_five_schemas_present(self):
        names = [s["name"] for s in ALL_TEMPORAL_SCHEMAS]
        assert "get_temporal_context" in names
        assert "schedule_event" in names
        assert "cancel_event" in names
        assert "set_timer_alarm" in names
        assert "nominate_milestone" in names

    def test_get_temporal_context_schema(self):
        schema = next(s for s in ALL_TEMPORAL_SCHEMAS if s["name"] == "get_temporal_context")
        assert "inputSchema" in schema
        assert "mode" in str(schema["inputSchema"])

    def test_schedule_event_schema_has_required(self):
        schema = next(s for s in ALL_TEMPORAL_SCHEMAS if s["name"] == "schedule_event")
        required = schema["inputSchema"].get("required", [])
        assert "title" in required
        assert "start_time" in required

    def test_cancel_event_schema_has_event_id_required(self):
        schema = next(s for s in ALL_TEMPORAL_SCHEMAS if s["name"] == "cancel_event")
        assert "event_id" in schema["inputSchema"]["required"]

    def test_nominate_milestone_schema(self):
        schema = next(s for s in ALL_TEMPORAL_SCHEMAS if s["name"] == "nominate_milestone")
        required = schema["inputSchema"]["required"]
        assert "title" in required
        assert "occurred_at" in required


class TestTemporalToolHandlers:
    """Test handler implementations for all 5 temporal tools."""

    @pytest.fixture
    def config(self):
        return ParousiaConfig(domain="test.example.com")

    @pytest.fixture
    def db(self):
        db = TemporalDB(db_path=":memory:")
        db.connect()
        db.create_tables()
        yield db
        db.close()

    @pytest.fixture
    def handlers(self, config, db):
        return TemporalToolHandlers(config, db)

    @pytest.fixture
    def agent_id(self):
        return "hermes"

    # ── get_temporal_context ───────────────────────────

    def test_get_temporal_context_standard(self, handlers, db, agent_id):
        db.insert_event({"agent_id": agent_id, "title": "Test", "start_time": "2026-06-15T10:00:00"})
        result = json.loads(handlers.dispatch("get_temporal_context", {}, agent_id))
        assert "context" in result
        assert result["mode"] == "standard"
        assert "!NOW:" in result["context"]

    def test_get_temporal_context_with_mode(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("get_temporal_context", {"mode": "planning"}, agent_id))
        assert result["mode"] == "planning"

    def test_get_temporal_context_includes_conflicts(self, handlers, db, agent_id):
        db.insert_event({"agent_id": agent_id, "title": "A", "start_time": "2026-06-15T10:00:00", "end_time": "2026-06-15T12:00:00"})
        db.insert_event({"agent_id": agent_id, "title": "B", "start_time": "2026-06-15T11:00:00", "end_time": "2026-06-15T13:00:00"})
        result = json.loads(handlers.dispatch("get_temporal_context", {}, agent_id))
        assert len(result["conflicts"]) == 1

    # ── schedule_event ─────────────────────────────────

    def test_schedule_event_succeeds(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("schedule_event", {
            "title": "Team Sync",
            "start_time": "2026-06-15T10:00:00",
        }, agent_id))
        assert result["scheduled"] is True
        assert result["event_id"] == "e1"
        assert result["title"] == "Team Sync"
        # Verify in DB
        events = db.get_events(agent_id)
        assert events[0]["title"] == "Team Sync"

    def test_schedule_event_with_end_time(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("schedule_event", {
            "title": "Long Meeting",
            "start_time": "2026-06-15T10:00:00",
            "end_time": "2026-06-15T12:00:00",
        }, agent_id))
        assert result["scheduled"] is True
        events = db.get_events(agent_id)
        assert events[0]["end_time"] == "2026-06-15T12:00:00"

    def test_schedule_event_detects_conflicts(self, handlers, db, agent_id):
        db.insert_event({"agent_id": agent_id, "title": "Existing", "start_time": "2026-06-15T10:00:00", "end_time": "2026-06-15T12:00:00"})
        result = json.loads(handlers.dispatch("schedule_event", {
            "title": "Overlapping",
            "start_time": "2026-06-15T11:00:00",
        }, agent_id))
        assert len(result["conflicts"]) > 0

    # ── cancel_event ───────────────────────────────────

    def test_cancel_event_succeeds(self, handlers, db, agent_id):
        db.insert_event({"agent_id": agent_id, "title": "To Cancel", "start_time": "2026-06-15T10:00:00"})
        result = json.loads(handlers.dispatch("cancel_event", {"event_id": "e1"}, agent_id))
        assert result["cancelled"] is True
        assert result["title"] == "To Cancel"
        events = db.get_events(agent_id)
        assert events[0]["status"] == "cancelled"

    def test_cancel_event_not_found(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("cancel_event", {"event_id": "e99"}, agent_id))
        assert result["cancelled"] is False
        assert "error" in result

    def test_cancel_event_with_full_id(self, handlers, db, agent_id):
        db.insert_event({"agent_id": agent_id, "title": "Full ID", "start_time": "2026-06-15T10:00:00"})
        result = json.loads(handlers.dispatch("cancel_event", {"event_id": "hermes:e1"}, agent_id))
        assert result["cancelled"] is True

    # ── set_timer_alarm ────────────────────────────────

    def test_set_timer_succeeds(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("set_timer_alarm", {
            "title": "Refactor",
            "duration_minutes": 30,
        }, agent_id))
        assert result["set"] is True
        assert result["type"] == "timer"
        assert result["remaining_seconds"] > 0

    def test_set_alarm_succeeds(self, handlers, db, agent_id):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        result = json.loads(handlers.dispatch("set_timer_alarm", {
            "title": "Meeting alarm",
            "trigger_at": future,
        }, agent_id))
        assert result["set"] is True
        assert result["type"] == "alarm"

    def test_set_timer_alarm_rejects_both(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("set_timer_alarm", {
            "title": "Bad",
            "duration_minutes": 10,
            "trigger_at": "2026-06-15T10:00:00",
        }, agent_id))
        assert result["set"] is False

    def test_set_timer_alarm_rejects_neither(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("set_timer_alarm", {
            "title": "Bad",
        }, agent_id))
        assert result["set"] is False

    # ── nominate_milestone ─────────────────────────────

    def test_nominate_milestone_succeeds(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("nominate_milestone", {
            "title": "Shipped v1",
            "occurred_at": "2026-06-10",
            "entry_type": "shipped",
        }, agent_id))
        assert result["recorded"] is True
        assert result["journal_id"] == "j1"
        journal = db.get_journal(agent_id)
        assert journal[0]["title"] == "Shipped v1"
        assert journal[0]["entry_type"] == "shipped"

    def test_nominate_milestone_defaults(self, handlers, db, agent_id):
        result = json.loads(handlers.dispatch("nominate_milestone", {
            "title": "Default type",
            "occurred_at": "2026-06-10",
        }, agent_id))
        assert result["recorded"] is True
        journal = db.get_journal(agent_id)
        assert journal[0]["entry_type"] == "milestone"

    # ── Agent isolation ────────────────────────────────

    def test_agent_isolation_in_tools(self, handlers, db):
        """Agent A's events should not appear in Agent B's temporal context."""
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        db.insert_event({"agent_id": "hermes", "title": "H secret", "start_time": tomorrow})
        db.insert_event({"agent_id": "claude", "title": "C public", "start_time": tomorrow})

        hermes_result = json.loads(handlers.dispatch("get_temporal_context", {}, "hermes"))
        claude_result = json.loads(handlers.dispatch("get_temporal_context", {}, "claude"))

        assert "H secret" in hermes_result["context"]
        assert "C public" not in hermes_result["context"]
        assert "C public" in claude_result["context"]
        assert "H secret" not in claude_result["context"]

    # ── Unknown tool ───────────────────────────────────

    def test_unknown_tool_returns_error(self, handlers, agent_id):
        result = json.loads(handlers.dispatch("nonexistent_tool", {}, agent_id))
        assert "error" in result
