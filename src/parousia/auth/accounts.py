"""Account store — SQLite-backed agent accounts with bcrypt key hashing."""
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt


@dataclass
class Account:
    account_id: str
    display_name: str = ""
    api_key_hash: str = ""
    tier: str = "free"
    status: str = "active"
    email: str = ""
    email_verified: bool = False
    created_at: str = ""
    last_seen_at: str = ""
    rate_limit_per_hour: int = 20
    browser_max_instances: int = 1
    storage_bytes_used: int = 0
    metadata: str = "{}"


DEFAULT_DB_PATH = "/var/lib/parousia/accounts.db"


class AccountStore:
    """Manages agent accounts in SQLite with bcrypt key hashing."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                api_key_hash TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'free',
                status TEXT NOT NULL DEFAULT 'active',
                email TEXT NOT NULL DEFAULT '',
                email_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL DEFAULT '',
                rate_limit_per_hour INTEGER NOT NULL DEFAULT 20,
                browser_max_instances INTEGER NOT NULL DEFAULT 1,
                storage_bytes_used INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS api_key_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
            CREATE INDEX IF NOT EXISTS idx_accounts_tier ON accounts(tier);
            CREATE INDEX IF NOT EXISTS idx_key_events_account ON api_key_events(account_id);
        """)
        self._conn.commit()

    # ── Key hashing ───────────────────────────────

    @staticmethod
    def hash_key(api_key: str) -> str:
        return bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_key(api_key: str, key_hash: str) -> bool:
        return bcrypt.checkpw(api_key.encode(), key_hash.encode())

    @staticmethod
    def generate_key() -> str:
        return f"po_{uuid.uuid4().hex}"

    # ── CRUD ─────────────────────────────────────

    def create_account(
        self, account_id: str, tier: str = "free",
        email: str = "", display_name: str = "",
    ):
        """Create an account and return (account, raw_api_key)."""
        api_key = self.generate_key()
        key_hash = self.hash_key(api_key)
        now = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """INSERT INTO accounts (account_id, display_name, api_key_hash,
               tier, email, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (account_id, display_name, key_hash, tier, email, now),
        )
        self._conn.execute(
            "INSERT INTO api_key_events (account_id, event_type, key_hash, created_at) "
            "VALUES (?, 'created', ?, ?)",
            (account_id, key_hash, now),
        )
        self._conn.commit()

        account = self.get_account(account_id)
        return account, api_key

    def get_account(self, account_id: str) -> Optional[Account]:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if not row:
            return None
        return Account(
            account_id=row["account_id"],
            display_name=row["display_name"],
            api_key_hash=row["api_key_hash"],
            tier=row["tier"],
            status=row["status"],
            email=row["email"],
            email_verified=bool(row["email_verified"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            rate_limit_per_hour=row["rate_limit_per_hour"],
            browser_max_instances=row["browser_max_instances"],
            storage_bytes_used=row["storage_bytes_used"],
            metadata=row["metadata"],
        )

    def close(self):
        if self._conn:
            self._conn.close()
