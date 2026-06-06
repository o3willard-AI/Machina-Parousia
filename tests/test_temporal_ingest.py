"""Tests for temporal ingest pipeline."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from parousia.temporal.db import TemporalDB
from parousia.temporal.ingest import TemporalIngest


# ── Fixtures ──────────────────────────────────────────

@pytest.fixture
def db():
    db = TemporalDB(db_path=":memory:")
    db.connect()
    db.create_tables()
    yield db
    db.close()


@pytest.fixture
def ingest(db):
    return TemporalIngest(db)


# ── Sample ICS data ───────────────────────────────────

SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:test-001@example.com
DTSTART:20260615T100000Z
DTEND:20260615T110000Z
SUMMARY:Team Sync
ORGANIZER:mailto:alice@example.com
ATTENDEE:mailto:bob@example.com
END:VEVENT
END:VCALENDAR"""

SAMPLE_ICS_NO_UID = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260615T140000Z
SUMMARY:No UID Event
END:VEVENT
END:VCALENDAR"""

SAMPLE_ICS_INVALID = "this is not valid ics data at all"


# ── .ics parsing ──────────────────────────────────────

class TestICSvParse:
    def test_parse_ics_vevent(self, ingest, db):
        result = ingest.parse_ics(SAMPLE_ICS, "hermes")
        assert len(result["event_ids"]) == 1
        assert result["errors"] == []
        events = db.get_events("hermes")
        assert events[0]["title"] == "Team Sync"
        assert events[0]["start_time"] == "2026-06-15T10:00:00+00:00"
        assert "bob@example.com" in events[0]["stakeholders"]

    def test_parse_ics_idempotent(self, ingest, db):
        result1 = ingest.parse_ics(SAMPLE_ICS, "hermes")
        result2 = ingest.parse_ics(SAMPLE_ICS, "hermes")
        assert len(result2["event_ids"]) == 1  # returned existing ID
        assert db.count_events("hermes") == 1   # only one row

    def test_parse_ics_no_uid_still_inserts(self, ingest, db):
        result = ingest.parse_ics(SAMPLE_ICS_NO_UID, "hermes")
        assert len(result["event_ids"]) == 1
        assert db.count_events("hermes") == 1

    def test_parse_ics_broken(self, ingest):
        result = ingest.parse_ics(SAMPLE_ICS_INVALID, "hermes")
        assert result["event_ids"] == []
        assert len(result["errors"]) > 0

    def test_parse_ics_with_rrule(self, ingest, db):
        ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:recur@test.com
DTSTART:20260615T100000Z
SUMMARY:Weekly Standup
RRULE:FREQ=WEEKLY;COUNT=10
END:VEVENT
END:VCALENDAR"""
        result = ingest.parse_ics(ics, "hermes")
        assert len(result["event_ids"]) == 1
        events = db.get_events("hermes")
        meta = json.loads(events[0]["metadata"])
        assert "rrule" in meta
        assert "FREQ=WEEKLY" in meta["rrule"]

    def test_parse_ics_agent_isolation(self, ingest, db):
        ingest.parse_ics(SAMPLE_ICS, "hermes")
        hermes_count = db.count_events("hermes")
        claude_count = db.count_events("claude")
        assert hermes_count == 1
        assert claude_count == 0


# ── JSON parsing ──────────────────────────────────────

class TestJSONParse:
    def test_parse_json_valid(self, ingest, db):
        eid = ingest.parse_json({
            "title": "Coffee Meeting",
            "start_time": "2026-06-15T14:00:00",
        }, "hermes")
        assert eid == "hermes:e1"
        events = db.get_events("hermes")
        assert events[0]["title"] == "Coffee Meeting"
        assert events[0]["source"] == "json_import"

    def test_parse_json_missing_title(self, ingest):
        with pytest.raises(ValueError, match="Missing required fields"):
            ingest.parse_json({"start_time": "2026-06-15T14:00:00"}, "hermes")

    def test_parse_json_missing_start_time(self, ingest):
        with pytest.raises(ValueError, match="Missing required fields"):
            ingest.parse_json({"title": "No Time"}, "hermes")

    def test_parse_json_with_optional_fields(self, ingest, db):
        eid = ingest.parse_json({
            "title": "Full Event",
            "start_time": "2026-06-15T14:00:00",
            "end_time": "2026-06-15T15:00:00",
            "flexibility": "low",
            "stakeholders": "alice@x.com, bob@x.com",
            "metadata": {"priority": 1},
        }, "hermes")
        events = db.get_events("hermes")
        e = events[0]
        assert e["flexibility"] == "low"
        assert e["end_time"] == "2026-06-15T15:00:00"
        assert "alice@x.com" in e["stakeholders"]


# ── NL parsing ────────────────────────────────────────

class TestNLParse:
    def test_parse_nl_passthrough(self, ingest):
        result = ingest.parse_nl("coffee tomorrow at 2pm", "hermes")
        assert result["passthrough"] is True
        assert result["text"] == "coffee tomorrow at 2pm"
        assert result["agent_id"] == "hermes"

    def test_parse_nl_with_endpoint(self, ingest, db):
        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "title": "Coffee",
                "start_time": "2026-06-16T14:00:00",
            }
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            result = ingest.parse_nl(
                "coffee tomorrow at 2pm", "hermes",
                llm_endpoint="http://localhost:8001/parse",
            )
            assert result["parsed"] is True
            assert result["event_id"] == "hermes:e1"
            events = db.get_events("hermes")
            assert events[0]["title"] == "Coffee"

    def test_parse_nl_endpoint_failure_fallback(self, ingest):
        with patch("requests.post") as mock_post:
            mock_post.side_effect = Exception("Connection refused")
            result = ingest.parse_nl(
                "meeting tomorrow", "hermes",
                llm_endpoint="http://dead:9999/parse",
            )
            assert result["passthrough"] is True


# ── Email bridge (integration with existing ingest) ──

class TestEmailBridge:
    """Verify the ingest module can be used from the email pipeline."""

    def test_ics_in_email_pipeline_integration(self, ingest, db):
        """Simulate what happens when an email with .ics attachment arrives."""
        # This is the code that would go in guard/ingest.py after MIME parsing
        result = ingest.parse_ics(SAMPLE_ICS, "hermes")
        assert len(result["event_ids"]) == 1
        assert result["errors"] == []
        # Verify the event is in the DB
        events = db.get_events("hermes")
        assert events[0]["source"] == "ics_import"
        assert events[0]["title"] == "Team Sync"
