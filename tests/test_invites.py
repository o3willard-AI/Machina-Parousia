"""Tests for InviteStore — create, validate, consume, list, revoke."""

import pytest
from parousia.auth.invites import InviteStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test_invites.db"
    s = InviteStore(str(db))
    s.connect()
    yield s
    s.close()


class TestInviteStore:
    def test_generate_code_format(self, store):
        code = store.generate_code()
        assert code.startswith("po_inv_")
        assert len(code) == 31  # "po_inv_" (7) + 24 hex chars

    def test_create(self, store):
        key = store.create(sponsor_id="stephen", note="For Bob")
        assert key.invite_code.startswith("po_inv_")
        assert key.sponsor_id == "stephen"
        assert key.note == "For Bob"
        assert key.status == "unused"
        assert key.use_count == 0
        assert key.max_uses == 1
        assert key.created_at != ""

    def test_create_with_max_uses(self, store):
        key = store.create(sponsor_id="stephen", max_uses=5)
        assert key.max_uses == 5

    def test_create_with_expiry(self, store):
        from datetime import datetime, timezone, timedelta
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        key = store.create(sponsor_id="stephen", expires_at=expires)
        assert key.expires_at == expires

    def test_get(self, store):
        created = store.create(sponsor_id="test")
        fetched = store.get(created.invite_code)
        assert fetched is not None
        assert fetched.invite_code == created.invite_code

    def test_get_nonexistent(self, store):
        assert store.get("po_inv_no_such_key") is None

    def test_validate_valid(self, store):
        key = store.create(sponsor_id="stephen")
        ok, reason = store.validate(key.invite_code)
        assert ok is True
        assert reason == "ok"

    def test_validate_invalid(self, store):
        ok, reason = store.validate("po_inv_nonexistent")
        assert ok is False
        assert "Invalid" in reason

    def test_validate_revoked(self, store):
        key = store.create(sponsor_id="test")
        store.revoke(key.invite_code)
        ok, reason = store.validate(key.invite_code)
        assert ok is False
        assert "revoked" in reason

    def test_validate_expired(self, store):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        key = store.create(sponsor_id="test", expires_at=past)
        ok, reason = store.validate(key.invite_code)
        assert ok is False
        assert "expired" in reason

    def test_consume(self, store):
        key = store.create(sponsor_id="test")
        result = store.consume(key.invite_code, "agent-x")
        assert result is True

        updated = store.get(key.invite_code)
        assert updated.status == "used"
        assert updated.used_by == "agent-x"
        assert updated.use_count == 1
        assert updated.used_at != ""

    def test_consume_already_used(self, store):
        key = store.create(sponsor_id="test")
        store.consume(key.invite_code, "agent-a")
        result = store.consume(key.invite_code, "agent-b")
        assert result is False

    def test_consume_multi_use(self, store):
        key = store.create(sponsor_id="test", max_uses=3)
        assert store.consume(key.invite_code, "agent-1") is True
        assert store.get(key.invite_code).use_count == 1
        assert store.get(key.invite_code).status == "unused"  # not fully consumed yet

        assert store.consume(key.invite_code, "agent-2") is True
        assert store.get(key.invite_code).use_count == 2
        assert store.get(key.invite_code).status == "unused"

        assert store.consume(key.invite_code, "agent-3") is True
        assert store.get(key.invite_code).use_count == 3
        assert store.get(key.invite_code).status == "used"  # fully consumed

        # Fourth use fails
        assert store.consume(key.invite_code, "agent-4") is False

    def test_list_all(self, store):
        store.create(sponsor_id="a")
        store.create(sponsor_id="b")
        store.create(sponsor_id="c")
        all_keys = store.list_invites()
        assert len(all_keys) == 3

    def test_list_by_status(self, store):
        key = store.create(sponsor_id="a")
        store.create(sponsor_id="b")
        store.consume(key.invite_code, "agent-x")

        unused = store.list_invites(status="unused")
        used = store.list_invites(status="used")
        assert len(unused) == 1
        assert len(used) == 1

    def test_revoke(self, store):
        key = store.create(sponsor_id="test")
        result = store.revoke(key.invite_code)
        assert result is True
        assert store.get(key.invite_code).status == "revoked"

    def test_revoke_already_used(self, store):
        key = store.create(sponsor_id="test")
        store.consume(key.invite_code, "agent-x")
        result = store.revoke(key.invite_code)
        assert result is False  # can't revoke a used key

    def test_revoke_nonexistent(self, store):
        result = store.revoke("po_inv_nonexistent")
        assert result is False
