"""Tests for the temporal DSL serializer."""

from datetime import datetime, timedelta, timezone

import pytest

from parousia.temporal.db import TemporalDB
from parousia.temporal.serializer import TemporalSerializer


class TestTemporalSerializer:
    """Test the temporal DSL serializer."""

    @pytest.fixture
    def db(self):
        db = TemporalDB(db_path=":memory:")
        db.connect()
        db.create_tables()
        yield db
        db.close()

    @pytest.fixture
    def serializer(self, db):
        return TemporalSerializer(db)

    @pytest.fixture
    def populated_db(self, db):
        """Seed events for 'hermes' across past, present, and future."""
        now = datetime.now(timezone.utc)
        # Past completed events (within last 24h for standard mode past_days=1)
        db.insert_event({
            "agent_id": "hermes", "title": "Kickoff w/ Sarah",
            "start_time": (now - timedelta(hours=12)).isoformat(),
            "end_time": (now - timedelta(hours=11)).isoformat(),
            "status": "completed",
        })
        db.insert_event({
            "agent_id": "hermes", "title": "Review PR #402",
            "start_time": (now - timedelta(hours=6)).isoformat(),
            "end_time": (now - timedelta(hours=5)).isoformat(),
            "status": "completed",
        })
        # Future confirmed events (within 3 days for standard mode future_days=3)
        db.insert_event({
            "agent_id": "hermes", "title": "Sync w/ Product",
            "start_time": (now + timedelta(days=1)).isoformat(),
            "end_time": (now + timedelta(days=1, hours=1)).isoformat(),
            "flexibility": "high", "status": "confirmed",
        })
        db.insert_event({
            "agent_id": "hermes", "title": "Deep Work: Architecture",
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=1, minutes=30)).isoformat(),
            "flexibility": "low", "status": "confirmed",
        })
        # Timer
        db.insert_event({
            "agent_id": "hermes", "title": "Refactor script",
            "start_time": now.isoformat(),
            "event_type": "timer", "status": "confirmed",
            "metadata": {"duration_minutes": 45},
        })
        # Journal entries
        db.insert_journal({
            "agent_id": "hermes", "title": "Shipped Parousia Phase 1",
            "occurred_at": (now - timedelta(days=4)).isoformat(),
        })
        db.insert_journal({
            "agent_id": "hermes", "title": "Research: AWS SES vs Postfix",
            "occurred_at": (now - timedelta(days=1)).isoformat(),
        })
        return db

    # ── DSL output ─────────────────────────────────────

    def test_standard_mode(self, serializer, populated_db):
        dsl = serializer.to_dsl("hermes", "standard")
        assert "!NOW:" in dsl
        assert "#PAST_WINDOW" in dsl
        assert "#PLANNED_WINDOW" in dsl
        assert "#TIMERS_ALARMS" in dsl
        assert "#JOURNAL" in dsl

    def test_planning_mode_no_past(self, serializer, populated_db):
        dsl = serializer.to_dsl("hermes", "planning")
        assert "#PAST_WINDOW" not in dsl  # past_days=0
        assert "#PLANNED_WINDOW" in dsl
        assert "#JOURNAL" not in dsl       # include_journal=False

    def test_retrospective_mode_no_future(self, serializer, populated_db):
        dsl = serializer.to_dsl("hermes", "retrospective")
        assert "#PAST_WINDOW" in dsl
        assert "#PLANNED_WINDOW" not in dsl  # future_days=0
        assert "#JOURNAL" in dsl

    def test_empty_agent_minimal_dsl(self, serializer, db):
        dsl = serializer.to_dsl("nobody", "standard")
        assert "!NOW:" in dsl
        assert "#PAST_WINDOW" not in dsl
        assert "#PLANNED_WINDOW" not in dsl
        assert "#TIMERS_ALARMS" not in dsl
        assert "#JOURNAL" not in dsl

    def test_token_count_under_200(self, serializer, populated_db):
        dsl = serializer.to_dsl("hermes", "standard")
        tokens = serializer.measure_tokens(dsl)
        assert tokens < 200, f"Token count {tokens} exceeds 200"

    # ── ID formatting ──────────────────────────────────

    def test_short_ids_in_dsl(self, serializer, populated_db):
        """IDs in DSL are stripped of agent_id prefix (hermes:e1 → e1)."""
        dsl = serializer.to_dsl("hermes", "standard")
        assert "[id:e1]" in dsl or "[id:e2]" in dsl or "[id:e3]" in dsl or "[id:e4]" in dsl
        assert "hermes:" not in dsl.split("#PAST_WINDOW")[0]  # header is clean

    # ── ID in !NOW header ──────────────────────────────

    def test_now_header_format(self, serializer, db):
        dsl = serializer.to_dsl("hermes", "standard")
        assert dsl.startswith("!NOW: ")
        assert "| DOMAIN: GENERAL_CORP" in dsl

    # ── Flexibility tags ───────────────────────────────

    def test_flexibility_on_future_events(self, serializer, populated_db):
        dsl = serializer.to_dsl("hermes", "standard")
        # Future events should have [F:high] or [F:low]
        assert "[F:high]" in dsl
        assert "[F:low]" in dsl

    def test_no_flexibility_on_past_events(self, serializer, populated_db):
        dsl = serializer.to_dsl("hermes", "retrospective")
        assert "[F:" not in dsl  # past events don't show flexibility

    # ── Conflict detection ─────────────────────────────

    def test_get_conflicts_no_overlap(self, serializer, db):
        # Two non-overlapping events
        db.insert_event({"agent_id": "hermes", "title": "A", "start_time": "2026-06-15T10:00:00", "end_time": "2026-06-15T11:00:00"})
        db.insert_event({"agent_id": "hermes", "title": "B", "start_time": "2026-06-15T12:00:00", "end_time": "2026-06-15T13:00:00"})
        conflicts = serializer.get_conflicts("hermes")
        assert conflicts == []

    def test_get_conflicts_overlap(self, serializer, db):
        db.insert_event({"agent_id": "hermes", "title": "A", "start_time": "2026-06-15T10:00:00", "end_time": "2026-06-15T11:30:00"})
        db.insert_event({"agent_id": "hermes", "title": "B", "start_time": "2026-06-15T11:00:00", "end_time": "2026-06-15T12:00:00"})
        conflicts = serializer.get_conflicts("hermes")
        assert len(conflicts) == 1
        c = conflicts[0]
        assert "e1" in (c["event_a"], c["event_b"])
        assert "e2" in (c["event_a"], c["event_b"])

    def test_get_conflicts_agent_isolation(self, serializer, db):
        db.insert_event({"agent_id": "hermes", "title": "H", "start_time": "2026-06-15T10:00:00", "end_time": "2026-06-15T12:00:00"})
        db.insert_event({"agent_id": "claude", "title": "C", "start_time": "2026-06-15T11:00:00", "end_time": "2026-06-15T13:00:00"})
        conflicts = serializer.get_conflicts("hermes")
        assert conflicts == []  # Claude's events are invisible to hermes

    # ── Invalid mode fallback ──────────────────────────

    def test_invalid_mode_falls_back_to_standard(self, serializer, populated_db):
        dsl = serializer.to_dsl("hermes", "garbage_mode")
        assert "#PAST_WINDOW" in dsl
        assert "#PLANNED_WINDOW" in dsl

    # ── measure_tokens ─────────────────────────────────

    def test_measure_tokens(self, serializer, db):
        dsl = "abc123"  # 6 chars
        assert serializer.measure_tokens(dsl) == 1  # 6/4 = 1
        dsl = "abcdefgh"  # 8 chars
        assert serializer.measure_tokens(dsl) == 2  # 8/4 = 2
