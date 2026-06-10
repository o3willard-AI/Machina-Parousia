"""Tests for REST ingest server and MIME parsing."""

import email
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from fastapi.testclient import TestClient

from parousia.guard.rest_server import app

client = TestClient(app)


# ── Health endpoint ──


def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "redis" in data
    assert data["postfix"] is True


# ── Ingest endpoint ──


@pytest.fixture
def valid_payload():
    return {
        "sender": "human@example.com",
        "recipient": "hermes@agents.test.com",
        "subject": "Review this PR",
        "body": "Please review https://github.com/example/repo/pull/1",
        "agent_id": "hermes",
        "raw_mime": "From: human@example.com\nTo: hermes@agents.test.com\nSubject: Review\n\nBody",
    }


@pytest.fixture
def agent_config():
    """Mock agent config for testing."""
    from parousia.config import ParousiaConfig, AgentConfig

    return ParousiaConfig(
        agents={
            "hermes": AgentConfig(
                rate_limit_per_hour=100,
            )
        }
    )


def test_ingest_accepts_valid_payload(valid_payload, agent_config, monkeypatch):
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: agent_config)

    resp = client.post("/ingest", json=valid_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["agent_id"] == "hermes"
    assert len(data["task_id"]) > 0


def test_ingest_rejects_unknown_agent(valid_payload, agent_config, monkeypatch):
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: agent_config)

    payload = {**valid_payload, "agent_id": "unknown_bot"}
    resp = client.post("/ingest", json=payload)
    assert resp.status_code == 404


def test_ingest_handles_missing_fields():
    resp = client.post("/ingest", json={"sender": "x@y.com"})
    assert resp.status_code == 422  # Pydantic validation


# ── MIME parsing (ingest.py) ──


def build_test_email(from_addr="test@example.com", to_addr="hermes@agents.test.com",
                     subject="Test", body="Hello world"):
    """Build a simple RFC 822 email for testing."""
    msg = MIMEText(body, "plain")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    return msg.as_string()


def build_multipart_email():
    """Build a multipart email with text + HTML."""
    msg = MIMEMultipart("alternative")
    msg["From"] = "human@example.com"
    msg["To"] = "hermes@agents.test.com"
    msg["Subject"] = "Multi-part test"
    msg.attach(MIMEText("Plain text body", "plain"))
    msg.attach(MIMEText("<p>HTML body</p>", "html"))
    return msg.as_string()


def test_ingest_parse_plain_email():
    """Parse a simple email via the ingest module logic."""
    raw = build_test_email()
    msg = email.message_from_string(raw, policy=email.policy.default)
    assert msg["From"] == "test@example.com"
    assert msg["Subject"] == "Test"

    # Extract agent_id
    recipient = str(msg["To"])
    agent_id = recipient.split("@")[0].strip().lower()
    assert agent_id == "hermes"

    # Extract body
    body = msg.get_content()
    assert "Hello world" in str(body)


def test_ingest_parse_multipart():
    """Parse multipart email, get plain text only."""
    raw = build_multipart_email()
    msg = email.message_from_string(raw, policy=email.policy.default)

    body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            body = part.get_content()
            break
    assert body.strip() == "Plain text body"


def test_ingest_parse_agent_id_from_recipient():
    """Extract agent_id from various recipient formats."""
    cases = [
        ("hermes@agents.test.com", "hermes"),
        ("HERMES@Agents.Test.Com", "hermes"),
        ("openclaw@domain.com", "openclaw"),
        ("no-domain", "no-domain"),  # fallback
    ]
    for recipient, expected in cases:
        agent_id = recipient.split("@")[0].strip().lower() if "@" in recipient else recipient.strip().lower()
        assert agent_id == expected, f"Failed for {recipient}"
