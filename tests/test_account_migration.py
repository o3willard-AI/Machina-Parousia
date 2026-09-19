"""In-place schema migration for pre-sponsor (legacy) accounts databases."""

import sqlite3

from parousia.auth.accounts import AccountStore


LEGACY_ACCOUNTS_SQL = """
    CREATE TABLE accounts (
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
        created_at TEXT NOT NULL
    );
"""


def _column_names(conn) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}


def test_migration_adds_sponsor_columns_in_place(tmp_path):
    """A pre-sponsor accounts.db gains sponsor columns; existing rows survive."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(LEGACY_ACCOUNTS_SQL)
    conn.execute(
        "INSERT INTO accounts (account_id, api_key_hash, created_at) "
        "VALUES (?, ?, ?)",
        ("legacy-agent", "hash_some_legacy_key", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()

    # Sanity: legacy schema really is missing the sponsor columns.
    assert "sponsor_id" not in _column_names(conn)
    assert "sponsor_contact" not in _column_names(conn)
    conn.close()

    # Opening through AccountStore must migrate the table in place.
    store = AccountStore(str(db))
    store.connect()

    assert "sponsor_id" in _column_names(store._conn)
    assert "sponsor_contact" in _column_names(store._conn)

    # Existing row survives; the new columns default to empty (no data loss).
    legacy = store.get_account("legacy-agent")
    assert legacy is not None
    assert legacy.account_id == "legacy-agent"
    assert legacy.sponsor_id == ""
    assert legacy.sponsor_contact == ""
    store.close()


def test_migration_idempotent_on_new_schema(tmp_path):
    """Connecting twice (fresh + reopened) never duplicates columns or rows."""
    db = tmp_path / "fresh.db"
    store = AccountStore(str(db))
    store.connect()

    # First connect creates the schema WITH the sponsor columns.
    assert "sponsor_id" in _column_names(store._conn)
    assert "sponsor_contact" in _column_names(store._conn)
    store.close()

    # Reopening an already-migrated DB must not error or duplicate columns.
    store2 = AccountStore(str(db))
    store2.connect()
    names = [row[1] for row in store2._conn.execute("PRAGMA table_info(accounts)").fetchall()]
    assert names.count("sponsor_id") == 1
    assert names.count("sponsor_contact") == 1
    store2.close()