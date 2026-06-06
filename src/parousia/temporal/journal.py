"""Temporal journal — agent bio and research milestone storage.

Separate from calendar events. Immutable entries recording what the agent
researched, decided, or shipped.
"""

from datetime import datetime
from typing import Optional

from .db import TemporalDB


class JournalManager:
    """Wrapper class for temporal journal operations."""
    
    def __init__(self, db: TemporalDB):
        self.db = db


def add_entry(
    db: TemporalDB,
    agent_id: str,
    title: str,
    description: Optional[str] = None,
    entry_type: str = "milestone",
    occurred_at: Optional[str] = None,
    tags: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Insert into temporal_journal table via db.insert_journal().
    
    If occurred_at is None, use current UTC datetime.
    Returns journal_id.
    """
    if occurred_at is None:
        occurred_at = datetime.utcnow().isoformat()
    
    entry = {
        "agent_id": agent_id,
        "title": title,
        "description": description,
        "entry_type": entry_type,
        "occurred_at": occurred_at,
        "tags": tags,
        "metadata": metadata,
    }
    
    return db.insert_journal(entry)


def get_recent(db: TemporalDB, agent_id: str, limit: int = 5) -> list[dict]:
    """Return recent journal entries via db.get_journal().
    
    Returns entries in reverse chronological order (most recent first).
    """
    return db.get_journal(agent_id, limit)


def count_entries(db: TemporalDB, agent_id: str) -> int:
    """Return count via db.count_journal()."""
    return db.count_journal(agent_id)


def days_since_last(db: TemporalDB, agent_id: str) -> Optional[int]:
    """Days since most recent journal entry, None if empty."""
    return db.days_since_last_journal(agent_id)


def generate_pulse_prompt(db: TemporalDB, agent_id: str) -> str:
    """Template: 'It has been {days} days since your last temporal milestone review.
    You have {events} calendar events. Your journal has {entries} entries.
    Consider: did you complete research, ship something, or reach a milestone?
    Use nominate_milestone to record it.'
    
    Uses db.count_events(), db.count_journal(), days_since_last().
    """
    days = days_since_last(db, agent_id)
    events = db.count_events(agent_id)
    entries = count_entries(db, agent_id)
    
    if days is None:
        days_text = "∞"
    else:
        days_text = str(days)
    
    return (
        f"It has been {days_text} days since your last temporal milestone review. "
        f"You have {events} calendar events. Your journal has {entries} entries. "
        f"Consider: did you complete research, ship something, or reach a milestone? "
        f"Use nominate_milestone to record it."
    )


__all__ = [
    "JournalManager",
    "add_entry", 
    "get_recent", 
    "count_entries", 
    "days_since_last", 
    "generate_pulse_prompt"
]
