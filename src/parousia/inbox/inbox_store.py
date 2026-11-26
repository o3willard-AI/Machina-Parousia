import os
import sqlite3
import uuid
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


class InboxMessage(BaseModel):
    id: str           # uuid4 hex
    agent_id: str     # which agent this mail is for
    sender: str       # From header
    recipient: str    # To header  
    subject: str
    body_text: str    # plain-text body
    body_html: Optional[str] = None
    received_at: str  # ISO 8601
    read: bool = False   # default False
    archived: bool = False   # default False


class InboxStore:
    def __init__(self, db_path: str = "data/inbox.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize the database with required tables and indexes."""
        if self.db_path != ":memory:":
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        
        # Create inbox_messages table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbox_messages (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_text TEXT NOT NULL,
                body_html TEXT,
                received_at TEXT NOT NULL,
                read BOOLEAN NOT NULL DEFAULT 0,
                archived BOOLEAN NOT NULL DEFAULT 0
            )
        """)
        
        # Create indexes for performance
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_received ON inbox_messages(agent_id, received_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_read_archived ON inbox_messages(read, archived)")
        
        conn.commit()
        conn.close()
    
    def store_message(self, message: InboxMessage) -> str:
        """Store a message and return its ID."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO inbox_messages 
            (id, agent_id, sender, recipient, subject, body_text, body_html, received_at, read, archived)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message.id,
            message.agent_id,
            message.sender,
            message.recipient,
            message.subject,
            message.body_text,
            message.body_html,
            message.received_at,
            message.read,
            message.archived
        ))
        conn.commit()
        conn.close()
        return message.id
    
    def get_message(self, message_id: str) -> Optional[InboxMessage]:
        """Retrieve a message by ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT id, agent_id, sender, recipient, subject, body_text, body_html, received_at, read, archived FROM inbox_messages WHERE id = ?",
            (message_id,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return InboxMessage(
                id=row[0],
                agent_id=row[1],
                sender=row[2],
                recipient=row[3],
                subject=row[4],
                body_text=row[5],
                body_html=row[6],
                received_at=row[7],
                read=bool(row[8]),
                archived=bool(row[9])
            )
        return None
    
    def list_messages(self, agent_id: str, limit: int = 50, offset: int = 0, unread_only: bool = False, include_archived: bool = True) -> List[InboxMessage]:
        """List messages for an agent.

        ``include_archived=False`` excludes archived rows so archive() is an
        observable view state (the default check_inbox listing shrinks as a
        watcher archives handled mail, instead of re-listing it forever).
        """
        conn = sqlite3.connect(self.db_path)
        
        query = "SELECT id, agent_id, sender, recipient, subject, body_text, body_html, received_at, read, archived FROM inbox_messages WHERE agent_id = ?"
        params = [agent_id]
        
        if unread_only:
            query += " AND read = 0"
        if not include_archived:
            query += " AND archived = 0"
            
        query += " ORDER BY received_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        return [
            InboxMessage(
                id=row[0],
                agent_id=row[1],
                sender=row[2],
                recipient=row[3],
                subject=row[4],
                body_text=row[5],
                body_html=row[6],
                received_at=row[7],
                read=bool(row[8]),
                archived=bool(row[9])
            )
            for row in rows
        ]
    
    def mark_read(self, message_id: str) -> bool:
        """Mark a message as read."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "UPDATE inbox_messages SET read = 1 WHERE id = ?",
            (message_id,)
        )
        conn.commit()
        rows_affected = cursor.rowcount
        conn.close()
        return rows_affected > 0
    
    def archive(self, message_id: str) -> bool:
        """Archive a message."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "UPDATE inbox_messages SET archived = 1 WHERE id = ?",
            (message_id,)
        )
        conn.commit()
        rows_affected = cursor.rowcount
        conn.close()
        return rows_affected > 0

    def mark_read_bulk(self, agent_id: str, message_ids: List[str]) -> int:
        """Mark many messages read in one query, scoped to ``agent_id``.

        The ``agent_id`` predicate is defense-in-depth: even if the caller
        passed a foreign id, the UPDATE only touches rows it owns. Returns
        the number of rows changed (0 for an empty/foreign id list).
        """
        if not message_ids:
            return 0
        placeholders = ",".join("?" * len(message_ids))
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            f"UPDATE inbox_messages SET read = 1 WHERE agent_id = ? AND id IN ({placeholders})",
            [agent_id, *message_ids],
        )
        conn.commit()
        rows_affected = cursor.rowcount
        conn.close()
        return rows_affected

    def archive_bulk(self, agent_id: str, message_ids: List[str]) -> int:
        """Archive many messages in one query, scoped to ``agent_id``."""
        if not message_ids:
            return 0
        placeholders = ",".join("?" * len(message_ids))
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            f"UPDATE inbox_messages SET archived = 1 WHERE agent_id = ? AND id IN ({placeholders})",
            [agent_id, *message_ids],
        )
        conn.commit()
        rows_affected = cursor.rowcount
        conn.close()
        return rows_affected

    def delete(self, message_id: str) -> bool:
        """Delete a message by ID (MP-5). Returns whether a row was removed."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "DELETE FROM inbox_messages WHERE id = ?",
            (message_id,)
        )
        conn.commit()
        rows_affected = cursor.rowcount
        conn.close()
        return rows_affected > 0
    
    def count_unread(self, agent_id: str) -> int:
        """Count unread messages for an agent."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM inbox_messages WHERE agent_id = ? AND read = 0",
            (agent_id,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count