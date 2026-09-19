import pytest
from parousia.auth.accounts import AccountStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.db"
    s = AccountStore(str(db))
    s.connect()
    yield s
    s.close()


def test_hash_and_verify(store):
    """hash_key produces a bcrypt hash; verify_key checks it correctly."""
    key = "po_test_abc123"
    h = store.hash_key(key)
    assert h != key
    assert store.verify_key(key, h)
    assert not store.verify_key("wrong_key", h)


def test_generate_key_format(store):
    """generate_key returns 'po_' + 32 hex chars."""
    key = store.generate_key()
    assert key.startswith("po_")
    assert len(key) == 35


def test_create_and_get(store):
    """create_account stores an account and returns it with a raw key."""
    account, raw_key = store.create_account("agent-1", tier="free")
    assert account.account_id == "agent-1"
    assert account.tier == "free"
    assert account.status == "active"
    assert raw_key.startswith("po_")
    fetched = store.get_account("agent-1")
    assert fetched is not None
    assert fetched.account_id == "agent-1"


def test_get_account_returns_sponsor_after_create(store):
    """create_account persists the sponsor; get_account returns it.

    Mutation check: if the sponsor column/field is removed, sponsor_id and
    sponsor_contact come back empty and this assertion fails.
    """
    account, _ = store.create_account(
        "agent-sponsor",
        sponsor_id="sponsor-mark",
        sponsor_contact="mark@corp.example",
    )
    assert account.sponsor_id == "sponsor-mark"
    assert account.sponsor_contact == "mark@corp.example"

    fetched = store.get_account("agent-sponsor")
    assert fetched is not None
    assert fetched.sponsor_id == "sponsor-mark"
    assert fetched.sponsor_contact == "mark@corp.example"


def test_create_account_sponsor_defaults_empty(store):
    """Sponsor fields default to empty when not provided."""
    account, _ = store.create_account("agent-nosponsor")
    assert account.sponsor_id == ""
    assert account.sponsor_contact == ""


def test_migrate_adds_sponsor_columns_keeps_rows(tmp_path):
    """A pre-sponsor accounts.db is migrated in place.

    Build the OLD accounts table (no sponsor columns), insert a live row, then
    open it through AccountStore. The migration must add both sponsor columns
    without dropping or corrupting the existing row.

    Mutation check: if the migration ALTERs aren't conditional (or don't run),
    the column assertion below fails.
    """
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
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
    """)
    key_hash = AccountStore.hash_key("po_legacy_key")
    conn.execute(
        "INSERT INTO accounts (account_id, api_key_hash, created_at) VALUES (?, ?, ?)",
        ("legacy-1", key_hash, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = AccountStore(str(db))
    store.connect()  # runs migration in place

    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(accounts)").fetchall()}
    assert "sponsor_id" in cols
    assert "sponsor_contact" in cols

    legacy = store.get_account("legacy-1")
    assert legacy is not None
    assert legacy.account_id == "legacy-1"
    # Existing row survives; new sponsor columns default to empty.
    assert legacy.sponsor_id == ""
    assert legacy.sponsor_contact == ""

    # Migration is idempotent — reconnecting does not error or duplicate.
    store.close()
    store2 = AccountStore(str(db))
    store2.connect()
    cols2 = {r[1] for r in store2._conn.execute("PRAGMA table_info(accounts)").fetchall()}
    assert "sponsor_id" in cols2
    assert "sponsor_contact" in cols2
    assert store2.get_account("legacy-1") is not None
    store2.close()


# ── Authentication ──────────────────────────────

def test_authenticate_success(store):
    """authenticate with the correct key returns the Account."""
    _, raw_key = store.create_account("agent-auth")
    account = store.authenticate(raw_key)
    assert account is not None
    assert account.account_id == "agent-auth"


def test_authenticate_wrong_key(store):
    """authenticate with a bad key returns None."""
    store.create_account("agent-auth")
    account = store.authenticate("po_nonexistent_key_000")
    assert account is None


def test_authenticate_suspended(store):
    """authenticate returns account even for suspended accounts; caller checks status."""
    _, raw_key = store.create_account("agent-sus")
    store.set_status("agent-sus", "suspended")
    account = store.authenticate(raw_key)
    assert account is not None
    assert account.account_id == "agent-sus"
    assert account.status == "suspended"


# ── Key rotation ───────────────────────────────

def test_rotate_key_success(store):
    """rotate_key returns a new key; old key stops working."""
    _, old_key = store.create_account("agent-rot")
    new_key = store.rotate_key("agent-rot")
    assert new_key is not None
    assert new_key != old_key
    assert store.authenticate(old_key) is None
    assert store.authenticate(new_key) is not None


def test_rotate_key_nonexistent(store):
    """rotate_key on a nonexistent account returns None."""
    assert store.rotate_key("nobody") is None


# ── Account lifecycle ──────────────────────────

def test_account_exists(store):
    """account_exists returns True/False correctly."""
    assert not store.account_exists("nobody")
    store.create_account("somebody")
    assert store.account_exists("somebody")


def test_create_duplicate_raises(store):
    """create_account with a duplicate account_id raises IntegrityError."""
    store.create_account("dupe")
    with pytest.raises(Exception):
        store.create_account("dupe")
