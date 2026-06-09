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
