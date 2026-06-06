"""Temporal database schema and connection management.

Supports SQLite (default) and PostgreSQL (optional, via psycopg2).
All tables are agent-scoped with agent_id as a foreign key.
"""

import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

DEFAULT_DB_PATH = "/var/lib/parousia/temporal.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS temporal_events (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    title         TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT,
    flexibility   TEXT DEFAULT 'high',
    event_type    TEXT DEFAULT 'event',
    status        TEXT DEFAULT 'confirmed',
    source        TEXT DEFAULT 'manual',
    stakeholders  TEXT,
    metadata      TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS temporal_journal (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    entry_type    TEXT DEFAULT 'milestone',
    occurred_at   TEXT NOT NULL,
    tags          TEXT,
    metadata      TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_agent_time
    ON temporal_events(agent_id, start_time);

CREATE INDEX IF NOT EXISTS idx_journal_agent_time
    ON temporal_journal(agent_id, occurred_at);
"""


class TemporalDB:
    """Database layer for Parousia temporal presence.

    Default backend: SQLite at /var/lib/parousia/temporal.db.
    PostgreSQL support via postgres_url (deferred to future story).
    All queries are agent-scoped — agent_id is always filtered.
    """

    def __init__(self, db_path: Optional[str] = None, postgres_url: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.postgres_url = postgres_url
        self._conn = None

    def connect(self):
        """Open connection and enable WAL mode for SQLite."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Create parent directory for file-based DBs
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        return self

    def create_tables(self):
        """Create tables if not exist (idempotent)."""
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        return self

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Events ─────────────────────────────────────────

    def get_events(
        self,
        agent_id: str,
        start_range: Optional[str] = None,
        end_range: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return events for an agent, optionally filtered by time range and status."""
        query = "SELECT * FROM temporal_events WHERE agent_id = ?"
        params: list = [agent_id]

        if start_range:
            query += " AND start_time >= ?"
            params.append(start_range)
        if end_range:
            query += " AND start_time <= ?"
            params.append(end_range)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY start_time ASC LIMIT ?"
        params.append(limit)

        return [dict(row) for row in self._conn.execute(query, params).fetchall()]

    def insert_event(self, event: dict) -> str:
        """Insert an event row. Returns the generated short ID (e.g., 'e1')."""
        event_id = self._next_id("e", "temporal_events", event["agent_id"])
        self._conn.execute(
            """INSERT INTO temporal_events
               (id, agent_id, title, start_time, end_time,
                flexibility, event_type, status, source, stakeholders, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event["agent_id"],
                event["title"],
                event["start_time"],
                event.get("end_time"),
                event.get("flexibility", "high"),
                event.get("event_type", "event"),
                event.get("status", "confirmed"),
                event.get("source", "manual"),
                event.get("stakeholders"),
                json.dumps(event["metadata"]) if event.get("metadata") else None,
            ),
        )
        self._conn.commit()
        return event_id

    def update_event(self, event_id: str, updates: dict):
        """Update event fields by ID. Only allowed columns are modified."""
        allowed = {
            "title", "start_time", "end_time", "flexibility",
            "event_type", "status", "source", "stakeholders", "metadata",
        }
        sets = {k: v for k, v in updates.items() if k in allowed}
        if not sets:
            return

        if "metadata" in sets and sets["metadata"] is not None:
            sets["metadata"] = json.dumps(sets["metadata"])
        sets["updated_at"] = datetime.utcnow().isoformat()

        set_clause = ", ".join(f"{k}=?" for k in sets)
        self._conn.execute(
            f"UPDATE temporal_events SET {set_clause} WHERE id=?",
            list(sets.values()) + [event_id],
        )
        self._conn.commit()

    # ── Journal ────────────────────────────────────────

    def get_journal(self, agent_id: str, limit: int = 5) -> list[dict]:
        """Return recent journal entries for an agent, most recent first."""
        rows = self._conn.execute(
            "SELECT * FROM temporal_journal WHERE agent_id=? ORDER BY occurred_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_journal(self, entry: dict) -> str:
        """Insert a journal entry. Returns the generated short ID (e.g., 'j1')."""
        journal_id = self._next_id("j", "temporal_journal", entry["agent_id"])
        self._conn.execute(
            """INSERT INTO temporal_journal
               (id, agent_id, title, description, entry_type, occurred_at, tags, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                journal_id,
                entry["agent_id"],
                entry["title"],
                entry.get("description"),
                entry.get("entry_type", "milestone"),
                entry["occurred_at"],
                entry.get("tags"),
                json.dumps(entry["metadata"]) if entry.get("metadata") else None,
            ),
        )
        self._conn.commit()
        return journal_id

    # ── Count helpers ──────────────────────────────────

    def count_events(self, agent_id: str) -> int:
        """Return the number of events for an agent."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM temporal_events WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return row[0] if row else 0

    def count_journal(self, agent_id: str) -> int:
        """Return the number of journal entries for an agent."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM temporal_journal WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return row[0] if row else 0

    def days_since_last_journal(self, agent_id: str) -> Optional[int]:
        """Return days since the most recent journal entry, or None if empty."""
        row = self._conn.execute(
            "SELECT occurred_at FROM temporal_journal WHERE agent_id=? ORDER BY occurred_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if not row:
            return None
        last_date = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        delta = datetime.utcnow() - last_date
        return delta.days

    # ── ID generation ──────────────────────────────────

    def _next_id(self, prefix: str, table: str, agent_id: str) -> str:
        """Generate next sequential short ID scoped to agent: 'hermes:e1', 'claude:e2'."""
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return f"{agent_id}:{prefix}{row[0] + 1}"


__all__ = ["TemporalDB", "DEFAULT_DB_PATH"]
