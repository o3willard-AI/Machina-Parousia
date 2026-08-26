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


@pytest.fixture
def account_store(tmp_path):
    """Real AccountStore on a temp SQLite DB, connected for the test."""
    from parousia.auth.accounts import AccountStore

    db = tmp_path / "test_accounts.db"
    store = AccountStore(str(db))
    store.connect()
    yield store
    store.close()


@pytest.fixture
def empty_agent_config():
    """Config with NO legacy agents — forces AccountStore lookup."""
    from parousia.config import ParousiaConfig

    return ParousiaConfig(agents={})


def test_ingest_accepts_valid_payload(valid_payload, agent_config, account_store, monkeypatch):
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: agent_config)
    monkeypatch.setattr("parousia.guard.rest_server._account_store", account_store)
    resp = client.post("/ingest", json=valid_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["agent_id"] == "hermes"
    assert len(data["task_id"]) > 0


def test_ingest_rejects_unknown_agent(valid_payload, agent_config, account_store, monkeypatch):
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: agent_config)
    monkeypatch.setattr("parousia.guard.rest_server._account_store", account_store)

    payload = {**valid_payload, "agent_id": "unknown_bot"}
    resp = client.post("/ingest", json=payload)
    assert resp.status_code == 404


def test_ingest_handles_missing_fields():
    resp = client.post("/ingest", json={"sender": "x@y.com"})
    assert resp.status_code == 422  # Pydantic validation


# ── AccountStore dual-registry regression tests ──
# Bug: /ingest validated recipients only against config.agents (legacy),
# so onboarded agents (in AccountStore but not config.agents) got 404 and
# their mail was silently dropped.  These tests verify the fix.


def test_ingest_accountstore_only_agent_accepted(
    valid_payload, empty_agent_config, account_store, monkeypatch
):
    """Account in AccountStore but NOT in config.agents → 200."""
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: empty_agent_config)
    monkeypatch.setattr("parousia.guard.rest_server._account_store", account_store)
    account_store.create_account("tina", tier="free")

    payload = {**valid_payload, "agent_id": "tina"}
    resp = client.post("/ingest", json=payload)
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "tina"


def test_ingest_suspended_account_rejected(
    valid_payload, empty_agent_config, account_store, monkeypatch
):
    """Account with status != 'active' in AccountStore → 403."""
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: empty_agent_config)
    monkeypatch.setattr("parousia.guard.rest_server._account_store", account_store)
    account_store.create_account("tina", tier="free")
    account_store.set_status("tina", "suspended")

    payload = {**valid_payload, "agent_id": "tina"}
    resp = client.post("/ingest", json=payload)
    assert resp.status_code == 403


def test_ingest_truly_unknown_agent_404(
    valid_payload, empty_agent_config, account_store, monkeypatch
):
    """Agent in neither AccountStore nor config.agents → 404."""
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: empty_agent_config)
    monkeypatch.setattr("parousia.guard.rest_server._account_store", account_store)

    payload = {**valid_payload, "agent_id": "ghost"}
    resp = client.post("/ingest", json=payload)
    assert resp.status_code == 404


def test_ingest_legacy_config_only_agent_still_accepted(
    valid_payload, agent_config, account_store, monkeypatch
):
    """Agent in config.agents but NOT AccountStore → 200 (legacy fallback)."""
    monkeypatch.setattr("parousia.guard.rest_server.load_config", lambda: agent_config)
    monkeypatch.setattr("parousia.guard.rest_server._account_store", account_store)
    # agent_config has only "hermes"; account_store has nobody
    assert account_store.get_account("hermes") is None

    resp = client.post("/ingest", json=valid_payload)
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "hermes"


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
