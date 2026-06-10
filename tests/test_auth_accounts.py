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
