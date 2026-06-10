"""Tests for MCP outbound server and email sender."""

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from parousia.config import ParousiaConfig, AgentConfig
from parousia.guard.email_sender import send_email
from parousia.guard.mcp_server import _build_server
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
    return ParousiaConfig(
        domain="agents.test.com",
        agents={"hermes": AgentConfig()},
    )


@pytest.fixture
def mock_redis():
    import fakeredis
    return fakeredis.FakeRedis()


def test_mcp_server_builds_without_error(mock_config, mock_redis, monkeypatch):
    """Server constructs without exceptions."""
    monkeypatch.setattr("parousia.guard.mcp_server.load_config", lambda: mock_config)
    monkeypatch.setattr("parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: mock_redis)
    monkeypatch.setattr("parousia.guard.mcp_server.TemporalDB", lambda **kw: _fake_temporal_db())

    server = _build_server()
    assert server.name == "parousia-guard-mcp"


@pytest.mark.xfail(reason="MCP SDK Server.tools is not a public attribute in this version")
def test_send_email_tool_schema_has_required_fields(mock_config, mock_redis, monkeypatch):
    """Tool inputSchema specifies required fields."""
    monkeypatch.setattr("parousia.guard.mcp_server.load_config", lambda: mock_config)
    monkeypatch.setattr("parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: mock_redis)
    monkeypatch.setattr("parousia.guard.mcp_server.TemporalDB", lambda **kw: _fake_temporal_db())

    server = _build_server()
    tool = next((t for t in server.tools if t.name == "send_email"), None)
    assert tool is not None
    schema = tool.input_schema
    assert "required" in schema
    assert "to" in schema["required"]
    assert "subject" in schema["required"]
    assert "body" in schema["required"]
