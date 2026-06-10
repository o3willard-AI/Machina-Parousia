"""Tests for MCP auth (Story D)."""

import tempfile
import os
from unittest.mock import patch

import pytest

from parousia.auth.accounts import AccountStore, Account
from parousia.auth.mcp_auth import (
    authenticate_mcp,
    get_auth_context,
    set_auth_context,
    _auth_context,
)


# ── mcp_auth.py unit tests ────────────────────────


def test_authenticate_valid_bearer_token():
    """authenticate_mcp returns account for valid Bearer token."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = AccountStore(db_path)
        store.connect()

        # Create an account to authenticate against
        account, api_key = store.create_account("test-agent", tier="free")
        assert account is not None

        # Authenticate with valid key
        result = authenticate_mcp(store, {"Authorization": f"Bearer {api_key}"})
        assert result is not None
        assert result.account_id == "test-agent"
        assert result.tier == "free"

    finally:
        os.unlink(db_path)


def test_authenticate_missing_header():
    """authenticate_mcp raises ValueError without Authorization header."""
    store = AccountStore(":memory:")
    store.connect()

    with pytest.raises(ValueError, match="Missing or invalid Authorization header"):
        authenticate_mcp(store, {})

    with pytest.raises(ValueError, match="Missing or invalid Authorization header"):
        authenticate_mcp(store, {"Authorization": "Basic invalid"})


def test_authenticate_invalid_key():
    """authenticate_mcp raises ValueError for invalid API key."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = AccountStore(db_path)
        store.connect()
        store.create_account("test-agent", tier="free")

        with pytest.raises(ValueError, match="Invalid API key"):
            authenticate_mcp(store, {"Authorization": "Bearer po_badkey"})

    finally:
        os.unlink(db_path)


def test_authenticate_suspended_account():
    """authenticate_mcp rejects suspended accounts."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = AccountStore(db_path)
        store.connect()
        account, api_key = store.create_account("suspended-agent", tier="free")
        store.set_status("suspended-agent", "suspended")

        with pytest.raises(ValueError, match="Account is suspended"):
            authenticate_mcp(store, {"Authorization": f"Bearer {api_key}"})

    finally:
        os.unlink(db_path)


def test_authenticate_lowercase_header():
    """authenticate_mcp handles lowercase 'authorization' header."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = AccountStore(db_path)
        store.connect()
        _, api_key = store.create_account("test-agent", tier="free")

        result = authenticate_mcp(store, {"authorization": f"Bearer {api_key}"})
        assert result.account_id == "test-agent"

    finally:
        os.unlink(db_path)


# ── Context variable tests ────────────────────────


def test_get_auth_context_default_none():
    """get_auth_context returns None when no context set."""
    # Reset context to ensure clean state
    token = _auth_context.set(None)
    try:
        assert get_auth_context() is None
    finally:
        _auth_context.reset(token)


def test_set_and_get_auth_context():
    """set_auth_context makes an account available to get_auth_context."""
    account = Account(account_id="ctx-agent", tier="paid")
    token = _auth_context.set(None)
    try:
        set_auth_context(account)
        result = get_auth_context()
        assert result is not None
        assert result.account_id == "ctx-agent"
        assert result.tier == "paid"
    finally:
        _auth_context.reset(token)


def test_auth_context_isolated_between_contexts():
    """Auth context is properly isolated between contextvars contexts."""
    import contextvars
    token = _auth_context.set(None)
    try:
        account_a = Account(account_id="agent-a")
        account_b = Account(account_id="agent-b")

        set_auth_context(account_a)
        assert get_auth_context().account_id == "agent-a"

        # Simulate a new context (like a new request)
        ctx = contextvars.copy_context()
        ctx.run(lambda: set_auth_context(account_b))
        result_b = ctx.run(lambda: get_auth_context())
        assert result_b.account_id == "agent-b"

        # Original context should still have agent-a
        assert get_auth_context().account_id == "agent-a"
    finally:
        _auth_context.reset(token)
