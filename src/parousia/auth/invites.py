"""Invite key store — one-time keys that gate public onboarding.

Invite keys are created by a human sponsor (via CLI or admin API) and consumed
exactly once during onboarding. Every key records its sponsor for traceability.
"""

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class InviteKey:
    invite_code: str
    sponsor_id: str
    sponsor_method: str = "manual"
    sponsor_contact: str = ""
    note: str = ""
    status: str = "unused"
    used_by: str = ""
    max_uses: int = 1
    use_count: int = 0
    created_at: str = ""
    used_at: str = ""
    expires_at: str = ""


INVITE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS invite_keys (
        invite_code     TEXT PRIMARY KEY,
        sponsor_id      TEXT NOT NULL,
        sponsor_method  TEXT NOT NULL DEFAULT 'manual',
        sponsor_contact TEXT NOT NULL DEFAULT '',
        note            TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'unused',
        used_by         TEXT NOT NULL DEFAULT '',
        max_uses        INTEGER NOT NULL DEFAULT 1,
        use_count       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        used_at         TEXT NOT NULL DEFAULT '',
        expires_at      TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_invite_status ON invite_keys(status);
    CREATE INDEX IF NOT EXISTS idx_invite_sponsor ON invite_keys(sponsor_id);
"""


class InviteStore:
    """Manages invite keys in the same SQLite DB as accounts."""

    def __init__(self, db_path: str = "/var/lib/parousia/accounts.db"):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(INVITE_TABLE_SQL)
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()

    @staticmethod
    def generate_code() -> str:
        return f"po_inv_{uuid.uuid4().hex[:24]}"

    # ── CRUD ───────────────────────────────────────

    def create(
        self,
        sponsor_id: str,
        sponsor_method: str = "manual",
        sponsor_contact: str = "",
        note: str = "",
        max_uses: int = 1,
        expires_at: str = "",
    ) -> InviteKey:
        """Create a new invite key. Returns the key object with the raw code."""
        code = self.generate_code()
        now = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """INSERT INTO invite_keys
               (invite_code, sponsor_id, sponsor_method, sponsor_contact,
                note, max_uses, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, sponsor_id, sponsor_method, sponsor_contact,
             note, max_uses, now, expires_at),
        )
        self._conn.commit()
        return self.get(code)

    def get(self, invite_code: str) -> Optional[InviteKey]:
        row = self._conn.execute(
            "SELECT * FROM invite_keys WHERE invite_code = ?", (invite_code,)
        ).fetchone()
        if not row:
            return None
        return InviteKey(
            invite_code=row["invite_code"],
            sponsor_id=row["sponsor_id"],
            sponsor_method=row["sponsor_method"],
            sponsor_contact=row["sponsor_contact"],
            note=row["note"],
            status=row["status"],
            used_by=row["used_by"],
            max_uses=row["max_uses"],
            use_count=row["use_count"],
            created_at=row["created_at"],
            used_at=row["used_at"],
            expires_at=row["expires_at"],
        )

    def validate(self, invite_code: str) -> tuple[bool, str]:
        """Check if an invite code is valid. Returns (ok, reason)."""
        invite = self.get(invite_code)
        if not invite:
            return False, "Invalid invite code"

        if invite.status == "revoked":
            return False, "Invite code has been revoked"

        if invite.status == "used" and invite.use_count >= invite.max_uses:
            return False, "Invite code has already been used"

        if invite.expires_at:
            now = datetime.now(timezone.utc).isoformat()
            if invite.expires_at < now:
                return False, "Invite code has expired"

        return True, "ok"

    def consume(self, invite_code: str, account_id: str) -> bool:
        """Mark an invite code as used (or increment use_count). Returns True on success."""
        ok, _ = self.validate(invite_code)
        if not ok:
            return False

        now = datetime.now(timezone.utc).isoformat()
        new_count = (self.get(invite_code).use_count or 0) + 1
        new_status = "used" if new_count >= (self.get(invite_code).max_uses or 1) else "unused"

        self._conn.execute(
            """UPDATE invite_keys
               SET status = ?, use_count = ?, used_by = ?, used_at = ?
               WHERE invite_code = ?""",
            (new_status, new_count, account_id, now, invite_code),
        )
        self._conn.commit()
        return True

    def list_invites(self, status: Optional[str] = None, limit: int = 50) -> list[InviteKey]:
        """List invite keys, optionally filtered by status."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM invite_keys WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM invite_keys ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [InviteKey(
            invite_code=r["invite_code"],
            sponsor_id=r["sponsor_id"],
            sponsor_method=r["sponsor_method"],
            sponsor_contact=r["sponsor_contact"],
            note=r["note"],
            status=r["status"],
            used_by=r["used_by"],
            max_uses=r["max_uses"],
            use_count=r["use_count"],
            created_at=r["created_at"],
            used_at=r["used_at"],
            expires_at=r["expires_at"],
        ) for r in rows]

    def revoke(self, invite_code: str) -> bool:
        cur = self._conn.execute(
            "UPDATE invite_keys SET status = 'revoked' WHERE invite_code = ? AND status = 'unused'",
            (invite_code,),
        )
        self._conn.commit()
        return cur.rowcount > 0
