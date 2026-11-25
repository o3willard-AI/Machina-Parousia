"""Tests for MCP outbound server and email sender."""
import json
import smtplib
from unittest.mock import MagicMock, patch

import pytest

from parousia.config import ParousiaConfig
from parousia.guard.email_sender import send_email
from parousia.guard.mcp_server import _build_server, _handle_send_email
from parousia.guard.rate_limiter import RateLimiter


def _fake_temporal_db():
    """Create a mock TemporalDB that connects to :memory:."""
    from parousia.temporal.db import TemporalDB
    db = TemporalDB(db_path=":memory:")
    db.connect()
    db.create_tables()
    return db


# ── Email sender ──


def test_send_email_returns_message_id():
    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_instance

        msg_id = send_email(
            to="human@example.com",
            subject="Test",
            body="Hello",
            from_addr="hermes@agents.test.com",
        )
        assert "@agents.test.com" in msg_id
        assert mock_instance.sendmail.called


def test_send_email_with_reply_to():
    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_instance

        msg_id = send_email(
            to="human@example.com",
            subject="Test",
            body="Hello",
            from_addr="hermes@agents.test.com",
            reply_to="admin@agents.test.com",
        )
        assert msg_id
        call_args = mock_instance.sendmail.call_args
        raw_message = call_args[0][2]
        assert "Reply-To: admin@agents.test.com" in raw_message


def test_send_email_smtp_failure():
    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_instance.sendmail.side_effect = smtplib.SMTPException("Connection refused")
        mock_smtp.return_value.__enter__.return_value = mock_instance

        with pytest.raises(smtplib.SMTPException):
            send_email(to="x@y.com", subject="T", body="B", from_addr="a@b.com")


# ── MCP server construction ──


@pytest.fixture
def mock_config():
    # MP-7: no legacy agents dict — AccountStore is the single registry.
    return ParousiaConfig(domain="agents.test.com")


@pytest.fixture
def mock_redis():
    import fakeredis
    return fakeredis.FakeRedis()


def test_mcp_server_builds_without_error(mock_config, mock_redis, monkeypatch):
    """Server constructs without exceptions."""
    monkeypatch.setattr("parousia.guard.mcp_server.load_config", lambda: mock_config)
    monkeypatch.setattr("parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: mock_redis)
    monkeypatch.setattr("parousia.guard.mcp_server.TemporalDB", lambda **kw: _fake_temporal_db())

    server, _account_store = _build_server()
    assert server.name == "parousia-guard-mcp"


@pytest.mark.xfail(reason="MCP SDK Server.tools is not a public attribute in this version")
def test_send_email_tool_schema_has_required_fields(mock_config, mock_redis, monkeypatch):
    """Tool inputSchema specifies required fields."""
    monkeypatch.setattr("parousia.guard.mcp_server.load_config", lambda: mock_config)
    monkeypatch.setattr("parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: mock_redis)
    monkeypatch.setattr("parousia.guard.mcp_server.TemporalDB", lambda **kw: _fake_temporal_db())

    server, _account_store = _build_server()
    tool = next((t for t in server.tools if t.name == "send_email"), None)
    assert tool is not None
    schema = tool.input_schema
    assert "required" in schema
    assert "to" in schema["required"]
    assert "subject" in schema["required"]
    assert "body" in schema["required"]


# ── Multi-agent routing tests ──


@pytest.fixture
def mock_rate_limiter():
    """Create a mock rate limiter that always allows."""
    rl = MagicMock(spec=RateLimiter)
    rl.check.return_value = (True, 99, 3600)
    return rl


@pytest.fixture
def mock_redis_client():
    """Create a mock redis client."""
    return MagicMock()


def test_send_email_from_agent_param(
    tmp_path, mock_rate_limiter, mock_redis_client, monkeypatch
):
    """MP-7: stdio from_agent resolves against AccountStore (the single registry)."""
    from parousia.auth.accounts import AccountStore

    config = ParousiaConfig(domain="agents.test.com")
    store = AccountStore(str(tmp_path / "route.db"))
    store.connect()
    try:
        store.ensure_account("agent-a", rate_limit_per_hour=100)
        store.ensure_account("agent-b", rate_limit_per_hour=100)
        monkeypatch.setattr(
            "parousia.guard.mcp_server._smtp_send", lambda **kw: "msg-id"
        )
        arguments = {
            "to": "recipient@example.com",
            "subject": "Test Subject",
            "body": "Test Body",
            "from_agent": "agent-b",
        }
        result = _run_send_email(
            arguments, config, mock_rate_limiter, mock_redis_client,
            account=None, account_store=store,
        )
        data = json.loads(result[0].text)
        assert data["sent"] is True, data
        assert data["message_id"] == "msg-id"
    finally:
        store.close()


def test_send_email_stdio_defaults_to_default_sentinel(
    mock_rate_limiter, mock_redis_client, monkeypatch
):
    """MP-7: no from_agent + no auth -> the "default" sentinel (NOT the first
    agent — the legacy first-configured-agent fallback is gone)."""
    config = ParousiaConfig(domain="agents.test.com")
    monkeypatch.setattr(
        "parousia.guard.mcp_server._smtp_send", lambda **kw: "msg-id"
    )
    arguments = {
        "to": "recipient@example.com",
        "subject": "Test Subject",
        "body": "Test Body",
    }
    result = _run_send_email(
        arguments, config, mock_rate_limiter, mock_redis_client,
        account=None, account_store=None,
    )
    data = json.loads(result[0].text)
    # The sentinel passes validation (agent_id == "default") and sends.
    assert data["sent"] is True, data


def test_send_email_unknown_agent(
    tmp_path, mock_rate_limiter, mock_redis_client
):
    """MP-7: unknown agent -> refused, with available_agents from AccountStore."""
    from parousia.auth.accounts import AccountStore

    config = ParousiaConfig(domain="agents.test.com")
    store = AccountStore(str(tmp_path / "unknown.db"))
    store.connect()
    try:
        store.ensure_account("agent-a", rate_limit_per_hour=100)
        arguments = {
            "to": "recipient@example.com",
            "subject": "Test Subject",
            "body": "Test Body",
            "from_agent": "nonexistent",
        }
        result = _run_send_email(
            arguments, config, mock_rate_limiter, mock_redis_client,
            account=None, account_store=store,
        )
        data = json.loads(result[0].text)
        assert data["sent"] is False
        assert "Unknown agent" in data["error"]
        # available_agents now comes from the AccountStore registry.
        assert data["available_agents"] == ["agent-a"]
    finally:
        store.close()


# ── send_email from_agent validation (dual-registry regression) ──
# Bug: _handle_send_email validated from_agent only against config.agents,
# so onboarded agents (in AccountStore but not config.agents) got
# "Unknown agent" and could not send.  These tests verify the fix.


@pytest.fixture
def account_store(tmp_path):
    """Real AccountStore on a temp SQLite DB."""
    from parousia.auth.accounts import AccountStore

    db = tmp_path / "mcp_accounts.db"
    store = AccountStore(str(db))
    store.connect()
    yield store
    store.close()


@pytest.fixture
def empty_config():
    """MP-7: configs carry no agent registry at all — AccountStore only."""
    return ParousiaConfig(domain="agents.test.com")


def _run_send_email(arguments, config, rate_limiter, redis_client, account=None, account_store=None):
    """Helper: synchronously invoke the async _handle_send_email."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _handle_send_email(arguments, config, rate_limiter, redis_client, account, account_store)
        )
    finally:
        loop.close()


def test_send_email_onsio_accountstore_only_agent(
    mock_rate_limiter, mock_redis_client, account_store, empty_config, monkeypatch
):
    """Onboarded agent (AccountStore, not config.agents) can send via from_agent."""
    monkeypatch.setattr("parousia.guard.mcp_server._smtp_send", lambda **kw: "msg-id")
    account_store.create_account("tina", tier="free")
    arguments = {
        "to": "human@example.com",
        "subject": "Hi",
        "body": "Hello",
        "from_agent": "tina",
    }
    result = _run_send_email(
        arguments, empty_config, mock_rate_limiter, mock_redis_client,
        account=None, account_store=account_store,
    )
    data = json.loads(result[0].text)
    assert data["sent"] is True


def test_send_email_authenticated_account_not_in_config(
    mock_rate_limiter, mock_redis_client, account_store, empty_config, monkeypatch
):
    """Authenticated caller whose account is only in AccountStore can send."""
    monkeypatch.setattr("parousia.guard.mcp_server._smtp_send", lambda **kw: "msg-id")
    _, raw_key = account_store.create_account("tina", tier="free")
    account = account_store.authenticate(raw_key)
    assert account is not None
    arguments = {
        "to": "human@example.com",
        "subject": "Hi",
        "body": "Hello",
    }
    result = _run_send_email(
        arguments, empty_config, mock_rate_limiter, mock_redis_client,
        account=account, account_store=account_store,
    )
    data = json.loads(result[0].text)
    assert data["sent"] is True
    assert data["message_id"] == "msg-id"


def test_send_email_suspended_account_rejected(
    mock_rate_limiter, mock_redis_client, account_store, empty_config, monkeypatch
):
    """Suspended AccountStore account → send_email error, not sent."""
    monkeypatch.setattr("parousia.guard.mcp_server._smtp_send", lambda **kw: "msg-id")
    account_store.create_account("tina", tier="free")
    account_store.set_status("tina", "suspended")
    account = account_store.get_account("tina")
    arguments = {
        "to": "human@example.com",
        "subject": "Hi",
        "body": "Hello",
    }
    result = _run_send_email(
        arguments, empty_config, mock_rate_limiter, mock_redis_client,
        account=account, account_store=account_store,
    )
    data = json.loads(result[0].text)
    assert data["sent"] is False
    assert "suspended" in data["error"]


def test_send_email_truly_unknown_agent(
    mock_rate_limiter, mock_redis_client, account_store, empty_config, monkeypatch
):
    """Agent in neither registry → 'Unknown agent' error."""
    monkeypatch.setattr("parousia.guard.mcp_server._smtp_send", lambda **kw: "msg-id")
    arguments = {
        "to": "human@example.com",
        "subject": "Hi",
        "body": "Hello",
        "from_agent": "ghost",
    }
    result = _run_send_email(
        arguments, empty_config, mock_rate_limiter, mock_redis_client,
        account=None, account_store=account_store,
    )
    data = json.loads(result[0].text)
    assert data["sent"] is False
    assert "Unknown agent" in data["error"]


def test_send_email_unmigrated_config_agent_refused(
    mock_rate_limiter, mock_redis_client, account_store, monkeypatch
):
    """MP-7 INVERSE of the old legacy fallback: an agent that exists only in
    a leftover YAML `agents:` block (NOT migrated into AccountStore) is
    REFUSED. That is the point of `parousia-guard migrate-agents` — after
    migrating, the same agent sends (the from_agent test above).
    """
    monkeypatch.setattr("parousia.guard.mcp_server._smtp_send", lambda **kw: "msg-id")
    config = ParousiaConfig(domain="agents.test.com")
    # mr-krabs was never migrated into account_store.
    assert account_store.get_account("mr-krabs") is None
    arguments = {
        "to": "human@example.com",
        "subject": "Hi",
        "body": "Hello",
        "from_agent": "mr-krabs",
    }
    result = _run_send_email(
        arguments, config, mock_rate_limiter, mock_redis_client,
        account=None, account_store=account_store,
    )
    data = json.loads(result[0].text)
    assert data["sent"] is False
    assert "Unknown agent" in data["error"]
    # Migrating un-blocks it: ensure_account → the send goes through.
    account_store.ensure_account("mr-krabs", rate_limit_per_hour=100)
    result = _run_send_email(
        arguments, config, mock_rate_limiter, mock_redis_client,
        account=None, account_store=account_store,
    )
    data = json.loads(result[0].text)
    assert data["sent"] is True, data
