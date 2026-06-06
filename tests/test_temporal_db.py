"""Tests for temporal database schema and storage layer."""

import json
from datetime import datetime, timedelta

import pytest

from parousia.temporal.db import TemporalDB


class TestTemporalDB:
    """Test the temporal database schema and storage layer."""

    @pytest.fixture
    def db(self):
        """Create an in-memory SQLite database for testing."""
        db = TemporalDB(db_path=":memory:")
        db.connect()
        db.create_tables()
        yield db
        db.close()

    # ── Schema ─────────────────────────────────────────

    def test_create_tables_creates_both_tables(self, db):
        tables = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = [r[0] for r in tables]
        assert "temporal_events" in names
        assert "temporal_journal" in names

    def test_create_tables_is_idempotent(self, db):
        # Second call should not raise
        db.create_tables()

    def test_wal_mode_enabled(self, db):
        """WAL mode is enabled for file-based DBs. In-memory uses 'memory' mode."""
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.upper() in ("WAL", "MEMORY")

    # ── Events — insert / query ────────────────────────

    def test_insert_event_returns_event_id(self, db):
        eid = db.insert_event({
            "agent_id": "hermes",
            "title": "Test Event",
            "start_time": "2026-06-15T10:00:00",
        })
        assert eid == "hermes:e1"

    def test_get_events_agent_isolation(self, db):
        db.insert_event({"agent_id": "hermes", "title": "H event", "start_time": "2026-06-15T10:00:00"})
        db.insert_event({"agent_id": "claude", "title": "C event", "start_time": "2026-06-15T11:00:00"})
        hermes_events = db.get_events("hermes")
        assert len(hermes_events) == 1
        assert hermes_events[0]["title"] == "H event"

    def test_get_events_time_filtering(self, db):
        db.insert_event({"agent_id": "hermes", "title": "Early", "start_time": "2026-01-01T10:00:00"})
        db.insert_event({"agent_id": "hermes", "title": "Late", "start_time": "2026-12-01T10:00:00"})
        filtered = db.get_events("hermes", start_range="2026-06-01", end_range="2026-12-31")
        assert len(filtered) == 1
        assert filtered[0]["title"] == "Late"

    def test_get_events_status_filter(self, db):
        db.insert_event({"agent_id": "hermes", "title": "Active", "start_time": "2026-06-15T10:00:00", "status": "confirmed"})
        db.insert_event({"agent_id": "hermes", "title": "Done", "start_time": "2026-06-15T10:00:00", "status": "completed"})
        active = db.get_events("hermes", status="confirmed")
        assert len(active) == 1
        assert active[0]["title"] == "Active"

    # ── Events — update ────────────────────────────────

    def test_update_event_changes_status(self, db):
        eid = db.insert_event({"agent_id": "hermes", "title": "Test", "start_time": "2026-06-15T10:00:00"})
        db.update_event(eid, {"status": "cancelled"})
        events = db.get_events("hermes")
        assert events[0]["status"] == "cancelled"

    def test_update_event_ignores_unknown_fields(self, db):
        eid = db.insert_event({"agent_id": "hermes", "title": "Safe", "start_time": "2026-06-15T10:00:00"})
        db.update_event(eid, {"nonexistent_field": "should be ignored"})
        events = db.get_events("hermes")
        assert events[0]["title"] == "Safe"

    # ── Journal — insert / query ───────────────────────

    def test_insert_journal_returns_journal_id(self, db):
        jid = db.insert_journal({
            "agent_id": "hermes",
            "title": "Shipped v1",
            "occurred_at": "2026-06-10",
        })
        assert jid == "hermes:j1"

    def test_get_journal_returns_recent_entries(self, db):
        db.insert_journal({"agent_id": "hermes", "title": "Old", "occurred_at": "2026-01-01"})
        db.insert_journal({"agent_id": "hermes", "title": "New", "occurred_at": "2026-06-10"})
        entries = db.get_journal("hermes", limit=5)
        assert len(entries) == 2
        assert entries[0]["title"] == "New"  # most recent first

    def test_get_journal_agent_isolation(self, db):
        db.insert_journal({"agent_id": "hermes", "title": "H journal", "occurred_at": "2026-06-10"})
        db.insert_journal({"agent_id": "claude", "title": "C journal", "occurred_at": "2026-06-10"})
        hermes_j = db.get_journal("hermes")
        assert len(hermes_j) == 1
        assert hermes_j[0]["title"] == "H journal"

    # ── Count helpers ──────────────────────────────────

    def test_count_events(self, db):
        db.insert_event({"agent_id": "hermes", "title": "A", "start_time": "2026-06-15T10:00:00"})
        db.insert_event({"agent_id": "hermes", "title": "B", "start_time": "2026-06-15T11:00:00"})
        db.insert_event({"agent_id": "claude", "title": "C", "start_time": "2026-06-15T12:00:00"})
        assert db.count_events("hermes") == 2

    def test_count_journal(self, db):
        db.insert_journal({"agent_id": "hermes", "title": "A", "occurred_at": "2026-06-01"})
        db.insert_journal({"agent_id": "hermes", "title": "B", "occurred_at": "2026-06-02"})
        assert db.count_journal("hermes") == 2

    def test_days_since_last_journal(self, db):
        ten_days_ago = (datetime.utcnow() - timedelta(days=10)).isoformat()
        db.insert_journal({"agent_id": "hermes", "title": "Old", "occurred_at": ten_days_ago})
        days = db.days_since_last_journal("hermes")
        assert days == 10

    def test_days_since_last_journal_empty(self, db):
        assert db.days_since_last_journal("hermes") is None

    # ── ID generation ──────────────────────────────────

    def test_next_id_sequential_per_agent(self, db):
        e1 = db.insert_event({"agent_id": "hermes", "title": "E1", "start_time": "2026-06-15T10:00:00"})
        e2 = db.insert_event({"agent_id": "hermes", "title": "E2", "start_time": "2026-06-15T11:00:00"})
        e3 = db.insert_event({"agent_id": "claude", "title": "C1", "start_time": "2026-06-15T12:00:00"})
        e4 = db.insert_event({"agent_id": "claude", "title": "C2", "start_time": "2026-06-15T13:00:00"})
        assert e1 == "hermes:e1"
        assert e2 == "hermes:e2"
        assert e3 == "claude:e1"  # separate agent restarts at e1
        assert e4 == "claude:e2"

    # ── Metadata roundtrip ─────────────────────────────

    def test_metadata_json_roundtrip(self, db):
        eid = db.insert_event({
            "agent_id": "hermes",
            "title": "JSON test",
            "start_time": "2026-06-15T10:00:00",
            "metadata": {"uid": "abc123", "priority": 1},
        })
        events = db.get_events("hermes")
        meta = json.loads(events[0]["metadata"])
        assert meta["uid"] == "abc123"
        assert meta["priority"] == 1

    # ── Default values ─────────────────────────────────

    def test_default_values_are_set(self, db):
        eid = db.insert_event({"agent_id": "hermes", "title": "Defaults", "start_time": "2026-06-15T10:00:00"})
        events = db.get_events("hermes")
        e = events[0]
        assert e["flexibility"] == "high"
        assert e["status"] == "confirmed"
        assert e["event_type"] == "event"
        assert e["source"] == "manual"
