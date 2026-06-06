"""Tests for temporal journal functionality."""

import pytest
from datetime import datetime, timedelta
from parousia.temporal.db import TemporalDB
from parousia.temporal.journal import (
    add_entry,
    get_recent,
    count_entries,
    days_since_last,
    generate_pulse_prompt,
)


@pytest.fixture
def temporal_db():
    """Create an in-memory TemporalDB for testing."""
    db = TemporalDB(":memory:")
    db.connect().create_tables()
    yield db
    db.close()


def test_add_entry_returns_journal_id(temporal_db):
    """Test that add_entry returns a journal_id."""
    journal_id = add_entry(
        temporal_db,
        agent_id="hermes",
        title="Test milestone",
        description="Completed initial research",
    )
    
    assert journal_id is not None
    assert journal_id.startswith("hermes:j")


def test_get_recent_returns_latest_first(temporal_db):
    """Test that get_recent returns entries in reverse chronological order."""
    # Add entries at different times
    older_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    newer_time = datetime.utcnow().isoformat()
    
    add_entry(
        temporal_db,
        agent_id="hermes",
        title="Older entry",
        occurred_at=older_time,
    )
    
    add_entry(
        temporal_db,
        agent_id="hermes", 
        title="Newer entry",
        occurred_at=newer_time,
    )
    
    entries = get_recent(temporal_db, "hermes", limit=5)
    
    assert len(entries) == 2
    assert entries[0]["title"] == "Newer entry"
    assert entries[1]["title"] == "Older entry"


def test_count_entries(temporal_db):
    """Test count_entries returns correct count."""
    # Initially should be 0
    assert count_entries(temporal_db, "hermes") == 0
    
    # Add some entries
    add_entry(temporal_db, "hermes", "Entry 1")
    add_entry(temporal_db, "hermes", "Entry 2")
    add_entry(temporal_db, "claude", "Claude entry")  # Different agent
    
    # hermes should have 2, claude should have 1
    assert count_entries(temporal_db, "hermes") == 2
    assert count_entries(temporal_db, "claude") == 1


def test_days_since_last_returns_delta(temporal_db):
    """Test that days_since_last returns the correct delta."""
    # Add entry from 3 days ago
    three_days_ago = (datetime.utcnow() - timedelta(days=3)).isoformat()
    add_entry(
        temporal_db,
        agent_id="hermes",
        title="Old entry",
        occurred_at=three_days_ago,
    )
    
    days = days_since_last(temporal_db, "hermes")
    # Should be approximately 3 days (within reasonable tolerance)
    assert days is not None
    assert 2 <= days <= 4  # Allow some tolerance for test execution time


def test_days_since_last_empty_returns_none(temporal_db):
    """Test that days_since_last returns None for empty journal."""
    days = days_since_last(temporal_db, "hermes")
    assert days is None


def test_generate_pulse_prompt_includes_stats(temporal_db):
    """Test that generate_pulse_prompt includes all expected statistics."""
    # Add some test data
    add_entry(temporal_db, "hermes", "Test entry")
    
    # Add an event to test event counting
    temporal_db.insert_event({
        "agent_id": "hermes",
        "title": "Test event",
        "start_time": datetime.utcnow().isoformat(),
        "metadata": {}
    })
    
    prompt = generate_pulse_prompt(temporal_db, "hermes")
    
    assert "days since your last temporal milestone review" in prompt
    assert "calendar events" in prompt
    assert "journal has" in prompt
    assert "entries" in prompt
    assert "nominate_milestone to record it" in prompt
    
    # Should contain specific numbers
    assert "1 calendar events" in prompt
    assert "1 entries" in prompt


def test_agent_isolation(temporal_db):
    """Test that hermes entries are invisible to claude."""
    # Add entries for both agents
    hermes_id = add_entry(temporal_db, "hermes", "Hermes milestone")
    claude_id = add_entry(temporal_db, "claude", "Claude milestone")
    
    # Each agent should only see their own entries
    hermes_entries = get_recent(temporal_db, "hermes")
    claude_entries = get_recent(temporal_db, "claude")
    
    assert len(hermes_entries) == 1
    assert len(claude_entries) == 1
    
    assert hermes_entries[0]["title"] == "Hermes milestone"
    assert claude_entries[0]["title"] == "Claude milestone"
    
    # Counts should be isolated too
    assert count_entries(temporal_db, "hermes") == 1
    assert count_entries(temporal_db, "claude") == 1
    
    # IDs should be agent-specific
    assert hermes_id.startswith("hermes:")
    assert claude_id.startswith("claude:")


def test_add_entry_with_default_occurred_at(temporal_db):
    """Test that add_entry uses current time when occurred_at is None."""
    before_time = datetime.utcnow()
    
    journal_id = add_entry(
        temporal_db,
        agent_id="hermes",
        title="Default time entry",
    )
    
    after_time = datetime.utcnow()
    
    entries = get_recent(temporal_db, "hermes", limit=1)
    assert len(entries) == 1
    
    entry_time = datetime.fromisoformat(entries[0]["occurred_at"].replace("Z", "+00:00"))
    
    # Entry time should be between before and after
    assert before_time <= entry_time <= after_time


def test_add_entry_with_all_fields(temporal_db):
    """Test add_entry with all optional fields specified."""
    custom_time = datetime.utcnow().isoformat()
    custom_metadata = {"priority": "high", "project": "test"}
    
    journal_id = add_entry(
        temporal_db,
        agent_id="hermes",
        title="Full entry",
        description="Complete milestone description",
        entry_type="research",
        occurred_at=custom_time,
        tags="important,milestone",
        metadata=custom_metadata,
    )
    
    entries = get_recent(temporal_db, "hermes", limit=1)
    entry = entries[0]
    
    assert entry["title"] == "Full entry"
    assert entry["description"] == "Complete milestone description"
    assert entry["entry_type"] == "research"
    assert entry["occurred_at"] == custom_time
    assert entry["tags"] == "important,milestone"
    assert entry["metadata"] is not None


def test_generate_pulse_prompt_empty_journal(temporal_db):
    """Test generate_pulse_prompt with empty journal shows infinity symbol."""
    prompt = generate_pulse_prompt(temporal_db, "hermes")
    
    assert "∞ days since" in prompt
    assert "0 calendar events" in prompt
    assert "0 entries" in prompt