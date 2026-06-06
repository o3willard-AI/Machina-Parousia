"""Tests for parousia-guard temporal CLI commands."""

import json
import os

import pytest
from click.testing import CliRunner

from parousia.cli.temporal import temporal_group
from parousia.temporal.db import TemporalDB


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Create an in-memory temporal DB for CLI testing."""
    db_path = str(tmp_path / "temporal_test.db")
    monkeypatch.setenv("PAROUSIA_TEMPORAL_DB", db_path)
    db = TemporalDB(db_path=db_path)
    db.connect()
    db.create_tables()
    yield db
    db.close()


# ═══════════════════════════════════════════════════════════════════════════
# setup
# ═══════════════════════════════════════════════════════════════════════════

def test_setup_creates_db(runner, temp_db, tmp_path):
    """parousia-guard temporal setup → DB initialised."""
    temp_db.close()  # let setup open its own connection
    result = runner.invoke(temporal_group, ["setup", "--force"])
    assert result.exit_code == 0
    assert "initialized" in result.output.lower()


def test_setup_idempotent(runner, temp_db):
    """Running setup twice does not error."""
    result = runner.invoke(temporal_group, ["setup"])
    assert result.exit_code == 0


# ═══════════════════════════════════════════════════════════════════════════
# validate
# ═══════════════════════════════════════════════════════════════════════════

def test_validate_passes(runner, temp_db):
    """validate exits 0 on healthy system."""
    result = runner.invoke(temporal_group, ["validate", "--agent-id", "hermes"])
    assert result.exit_code == 0
    assert "passed" in result.output


def test_validate_no_db(runner, tmp_path, monkeypatch):
    """validate exits 1 when DB does not exist."""
    monkeypatch.setenv("PAROUSIA_TEMPORAL_DB", str(tmp_path / "nonexistent.db"))
    result = runner.invoke(temporal_group, ["validate"])
    assert result.exit_code != 0


# ═══════════════════════════════════════════════════════════════════════════
# status
# ═══════════════════════════════════════════════════════════════════════════

def test_status_shows_counts(runner, temp_db):
    """status displays event/journal counts."""
    result = runner.invoke(temporal_group, ["status", "--agent-id", "hermes"])
    assert result.exit_code == 0
    assert "Events:" in result.output


def test_status_json_output(runner, temp_db):
    """status --json outputs valid JSON with counts."""
    result = runner.invoke(temporal_group, ["status", "--agent-id", "hermes", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "event_count" in data
    assert "journal_count" in data
    assert isinstance(data["event_count"], int)


# ═══════════════════════════════════════════════════════════════════════════
# export
# ═══════════════════════════════════════════════════════════════════════════

def test_export_no_events(runner, temp_db):
    """export with no events shows a warning."""
    result = runner.invoke(temporal_group, ["export", "--agent-id", "hermes"])
    assert result.exit_code == 0
    assert "No events" in result.output


def test_export_ics_format(runner, temp_db):
    """export --format ics produces valid iCalendar output."""
    temp_db.insert_event({
        "id": "placeholder", "agent_id": "hermes", "title": "Test Event",
        "start_time": "2026-06-10T10:00:00",
        "end_time": "2026-06-10T11:00:00",
    })
    result = runner.invoke(temporal_group, ["export", "--agent-id", "hermes", "--format", "ics"])
    assert result.exit_code == 0
    assert "BEGIN:VCALENDAR" in result.output


def test_export_single_event(runner, temp_db):
    """export --event-id <id> returns single event."""
    eid = temp_db.insert_event({
        "id": "placeholder", "agent_id": "hermes", "title": "Test",
        "start_time": "2026-06-10T10:00:00",
    })
    result = runner.invoke(temporal_group, ["export", "--agent-id", "hermes", "--event-id", eid])
    assert result.exit_code == 0
    assert "BEGIN:VCALENDAR" in result.output


def test_export_nonexistent_event(runner, temp_db):
    """export --event-id nonexistent exits 1."""
    result = runner.invoke(temporal_group, ["export", "--event-id", "nonexistent"])
    assert result.exit_code == 1


# ═══════════════════════════════════════════════════════════════════════════
# ingest
# ═══════════════════════════════════════════════════════════════════════════

def test_ingest_requires_input(runner, temp_db):
    """ingest without --ics or --json exits 1."""
    result = runner.invoke(temporal_group, ["ingest"])
    assert result.exit_code == 1


def test_ingest_ics_file_not_found(runner, temp_db):
    """ingest --ics nonexistent exits 1."""
    result = runner.invoke(temporal_group, ["ingest", "--ics", "/nonexistent.ics"])
    assert result.exit_code == 1


def test_ingest_json(runner, temp_db):
    """ingest --json inserts a single event."""
    payload = json.dumps({
        "title": "JSON Event", "start_time": "2026-06-15T09:00:00"
    })
    result = runner.invoke(temporal_group, ["ingest", "--json", payload, "--agent-id", "hermes"])
    assert result.exit_code == 0
    assert "ingested" in result.output.lower()


# ═══════════════════════════════════════════════════════════════════════════
# pulse
# ═══════════════════════════════════════════════════════════════════════════

def test_pulse_dry_run(runner, temp_db):
    """pulse --dry-run prints prompt without sending."""
    result = runner.invoke(temporal_group, ["pulse", "--agent-id", "hermes", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.output.lower()
    assert "days" in result.output.lower()


def test_pulse_generates_prompt(runner, temp_db):
    """pulse without --dry-run still exits 0 and outputs prompt."""
    result = runner.invoke(temporal_group, ["pulse", "--agent-id", "hermes"])
    assert result.exit_code == 0
    assert "days" in result.output.lower()


# ═══════════════════════════════════════════════════════════════════════════
# db
# ═══════════════════════════════════════════════════════════════════════════

def test_db_stats(runner, temp_db):
    """db --stats shows row counts."""
    result = runner.invoke(temporal_group, ["db", "--stats"])
    assert result.exit_code == 0
    assert "temporal_events" in result.output
    assert "temporal_journal" in result.output


def test_db_vacuum(runner, temp_db):
    """db --vacuum succeeds on SQLite."""
    result = runner.invoke(temporal_group, ["db", "--vacuum"])
    assert result.exit_code == 0
    assert "VACUUM" in result.output
