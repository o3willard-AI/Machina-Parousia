"""MP-17: send_email calendar invites — optional `ics` param + spoof-proofed MIME.

Security posture under test (the "trusted presence" bar):
- ORGANIZER is DERIVED from the authenticated From identity, never agent-supplied
  (an agent-supplied ORGANIZER line in the .ics is stripped, not honored).
- ATTENDEE is derived from the recipient.
- No CRLF header/body injection from any untrusted field (to/subject/ics).
- The plain-text path (no `ics`) is byte-for-byte unchanged.
"""

import asyncio
import json
from email import message_from_string
from unittest.mock import MagicMock, patch

import pytest

from parousia.config import ParousiaConfig
from parousia.guard.email_sender import send_email
from parousia.guard.rate_limiter import RateLimiter


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_rate_limiter():
    rl = MagicMock(spec=RateLimiter)
    rl.check.return_value = (True, 99, 3600)
    return rl


@pytest.fixture
def mock_redis_client():
    return MagicMock()


def _capture_sendmail():
    """Patch smtplib.SMTP; return (mock_instance, call_capture list)."""
    captured = []

    class _Capture:
        def __init__(self):
            self.sendmail = MagicMock(side_effect=self._sendmail)

        def _sendmail(self, from_addr, to_addrs, msg_data, **kw):
            captured.append(
                {"from_addr": from_addr, "to_addrs": to_addrs, "data": msg_data}
            )
            return {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    instance = _Capture()
    patcher = patch("smtplib.SMTP", return_value=instance)
    return patcher, captured


def _send(**kw):
    """Call send_email under a captured SMTP; return the raw message string."""
    patcher, captured = _capture_sendmail()
    with patcher:
        send_email(**kw)
    assert captured, "sendmail was not called"
    return captured[0]["data"]


VALID_ICS = "\n".join(
    [
        "BEGIN:VCALENDAR",
        "PRODID:-//Parousia//Temporal Export//EN",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:evt-123@agents.test.com",
        "SUMMARY:Deploy window",
        "DTSTART:20260701T150000Z",
        "DTEND:20260701T160000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)


# ── 1. MCP tool schema ───────────────────────────────────────────────


def test_send_email_schema_declares_optional_ics(monkeypatch):
    """The send_email tool schema carries `ics` (optional), never `raw_mime`."""
    from parousia.guard import mcp_server as mcp_mod
    from parousia.temporal.db import TemporalDB

    def _fake_temporal_db(**kw):
        db = TemporalDB(db_path=":memory:")
        db.connect()
        db.create_tables()
        return db

    monkeypatch.setattr(mcp_mod, "load_config", lambda: ParousiaConfig())
    monkeypatch.setattr(mcp_mod, "MemoryRecorder", MagicMock())
    monkeypatch.setattr(mcp_mod, "TemporalDB", _fake_temporal_db)
    import mcp.types as types

    async def run():
        server, _store = mcp_mod._build_server()
        handler = server.request_handlers[types.ListToolsRequest]
        listed = await handler(types.ListToolsRequest(params=None))
        return listed.root.tools

    tools = asyncio.run(run())
    tool = next(t for t in tools if t.name == "send_email")
    schema = tool.inputSchema

    assert "ics" in schema["properties"], "send_email must declare `ics`"
    assert "ics" not in schema.get("required", []), "`ics` stays optional"
    assert "text/calendar" in schema["properties"]["ics"]["description"]
    # MP-17: a verbatim MIME blob would let a caller spoof From/ORGANIZER.
    assert "raw_mime" not in schema["properties"], "raw_mime must NOT exist"
    # The plain required set is unchanged.
    assert schema["required"] == ["to", "subject", "body"]

# ── 2. MIME structure with `ics` ──────────────────────────────────────


def test_ics_send_is_multipart_alternative_with_calendar_parts():
    """send_email(ics=...) → multipart/alternative: text/plain +
    text/calendar;method=REQUEST + application/ics attachment."""
    raw = _send(
        to="human@example.com",
        subject="Deploy window",
        body="Please review.",
        from_addr="hermes@agents.test.com",
        ics=VALID_ICS,
    )
    msg = message_from_string(raw)

    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.walk())
    # walk() yields the container first; then the three parts in order.
    payload_parts = [p for p in parts if p.get_content_type() != "multipart/alternative"]

    types_in_order = [p.get_content_type() for p in payload_parts]
    assert types_in_order == ["text/plain", "text/calendar", "application/ics"]

    cal = payload_parts[1]
    assert cal.get_param("method") == "REQUEST"
    assert "REQUEST" in (cal.get_param("method") or "")

    # application/ics attachment part carries the same VEVENT payload.
    att = payload_parts[2]
    assert att.get_param("method") != "REQUEST"
    assert att.get_payload()  # base64 body present


def test_ics_body_contains_method_request_in_calendar_part():
    """METHOD:REQUEST must appear in BOTH the part Content-Type and the
    .ics body (the #1 Outlook failure mode is a mismatch)."""
    raw = _send(
        to="human@example.com",
        subject="Deploy window",
        body="Please review.",
        from_addr="hermes@agents.test.com",
        ics=VALID_ICS,
    )
    msg = message_from_string(raw)
    cal = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    body = cal.get_payload(decode=True).decode("utf-8")
    assert "METHOD:REQUEST" in body

    # The attachment carries the enriched payload too.
    att = next(p for p in msg.walk() if p.get_content_type() == "application/ics")
    att_body = att.get_payload(decode=True).decode("utf-8")
    assert "METHOD:REQUEST" in att_body


def test_send_email_without_ics_stays_plain_text():
    """No `ics` → byte-for-byte the old behavior: a plain MIMEText."""
    raw = _send(
        to="human@example.com",
        subject="Test",
        body="Hello",
        from_addr="hermes@agents.test.com",
    )
    msg = message_from_string(raw)
    assert msg.get_content_type() == "text/plain"
    assert msg["From"] == "hermes@agents.test.com"
    assert msg["To"] == "human@example.com"
    assert msg["Subject"] == "Test"


# ── 3. Security: derived ORGANIZER/ATTENDEE, spoof + injection resistance ──


FORGED_ICS = "\n".join(
    [
        "BEGIN:VCALENDAR",
        "PRODID:-//Evil//Forge//EN",
        "VERSION:2.0",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        "ORGANIZER;CN=Fake Boss:mailto:boss@attacker.example",
        "ATTENDEE;CN=Pwned Victim:mailto:victim@corp.example",
        "UID:evt-123@agents.test.com",
        "SUMMARY:Deploy window",
        "DTSTART:20260701T150000Z",
        "DTEND:20260701T160000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)


def _calendar_body(raw: str) -> str:
    msg = message_from_string(raw)
    cal = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    return cal.get_payload(decode=True).decode("utf-8")


def test_organizer_is_derived_not_agent_supplied():
    """ORGANIZER comes from the authenticated From identity; a forged
    ORGANIZER (and any METHOD/ATTENDEE) in the agent-supplied .ics is
    stripped, never honored."""
    raw = _send(
        to="human@example.com",
        subject="Deploy window",
        body="Please review.",
        from_addr="hermes@agents.test.com",
        ics=FORGED_ICS,
    )
    body = _calendar_body(raw)

    # Derived organizer: the account's own address, CN = local part.
    assert "ORGANIZER;CN=hermes:mailto:hermes@agents.test.com" in body
    # The forged organizer is gone.
    assert "boss@attacker.example" not in body
    # A forged METHOD:PUBLISH is never silently honored.
    assert "METHOD:PUBLISH" not in body
    assert "METHOD:REQUEST" in body


def test_attendee_is_derived_from_recipient():
    """ATTENDEE comes from `to` (ROLE=REQ-PARTICIPANT); forged ATTENDEE
    lines from the .ics payload are stripped."""
    raw = _send(
        to="human@example.com",
        subject="Deploy window",
        body="Please review.",
        from_addr="hermes@agents.test.com",
        ics=FORGED_ICS,
    )
    body = _calendar_body(raw)

    assert "ATTENDEE;CN=human;ROLE=REQ-PARTICIPANT:mailto:human@example.com" in body
    # The forged attendee is gone.
    assert "victim@corp.example" not in body
    assert "ATTENDEE;CN=Pwned" not in body


def test_crlf_injection_via_to_and_subject_cannot_add_headers():
    """`to`/`subject` carrying CRLF header-breakout payloads must not
    produce new headers in the sent message."""
    inject_to = "human@example.com\r\nBcc: attacker@evil.example"
    inject_subject = "Deploy\r\nBcc: pwned@evil.example"
    raw = _send(
        to=inject_to,
        subject=inject_subject,
        body="Hello",
        from_addr="hermes@agents.test.com",
        ics=VALID_ICS,
    )
    msg = message_from_string(raw)
    header_names = [k.lower() for k in msg.keys()]
    assert "bcc" not in header_names, "CRLF broke out into a Bcc header"
    # The injected text survives only flattened inside the header value.
    assert "attacker@evil.example" in msg["To"]
    assert "\r" not in msg["To"] and "\n" not in msg["To"]


def test_crlf_in_to_cannot_inject_ics_property_lines():
    """A CRLF payload inside `to` is flattened before it reaches the
    ATTENDEE derivation, so it cannot forge new .ics property lines."""
    inject_to = "human@example.com\r\nORGANIZER:mailto:evil@evil.example"
    raw = _send(
        to=inject_to,
        subject="Deploy window",
        body="Hello",
        from_addr="hermes@agents.test.com",
        ics=VALID_ICS,
    )
    body = _calendar_body(raw)
    # Exactly one ORGANIZER line — the derived one; the forged text was
    # flattened into the ATTENDEE *value* (inert), never its own property line.
    organizer_lines = [l for l in body.split("\r\n") if l.upper().startswith("ORGANIZER")]
    assert organizer_lines == ["ORGANIZER;CN=hermes:mailto:hermes@agents.test.com"]
    ics_lines = body.split("\r\n")
    assert "ORGANIZER:mailto:evil@evil.example" not in ics_lines


def test_uid_preserved_from_payload_when_present():
    raw = _send(
        to="human@example.com",
        subject="Deploy window",
        body="Hello",
        from_addr="hermes@agents.test.com",
        ics=VALID_ICS,
    )
    body = _calendar_body(raw)
    assert "UID:evt-123@agents.test.com" in body


def test_uid_generated_when_missing_from_payload():
    ics_no_uid = "\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "SUMMARY:Ad hoc sync",
            "DTSTART:20260701T150000Z",
            "DTEND:20260701T160000Z",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    raw = _send(
        to="human@example.com",
        subject="Sync",
        body="Hello",
        from_addr="hermes@agents.test.com",
        ics=ics_no_uid,
    )
    body = _calendar_body(raw)
    uid_lines = [l for l in body.split("\r\n") if l.upper().startswith("UID:")]
    assert len(uid_lines) == 1
    # Stable, derived from the sending domain (make_msgid), no angle brackets.
    assert "agents.test.com" in uid_lines[0]
    assert "<" not in uid_lines[0]


def test_to_ics_output_composes_into_a_valid_invite():
    """MP-17 section 4: schedule_event -> TemporalExport.to_ics ->
    send_email(ics=...) must produce a valid, spoof-proofed invitation."""
    from parousia.temporal.export import TemporalExport

    event_row = {
        "event_id": "hermes:e6",
        "title": "Deploy Parousia v0.6.0",
        "start_time": "2026-07-01T15:00:00+00:00",
        "end_time": "2026-07-01T16:00:00+00:00",
    }
    ics_payload = TemporalExport.to_ics(event_row)
    # to_ics carries UID and DTSTART/DTEND but no METHOD — the sender injects it.
    assert "UID:" in ics_payload
    assert "DTSTART" in ics_payload
    assert "DTEND" in ics_payload
    assert "METHOD:" not in ics_payload

    raw = _send(
        to="human@example.com",
        subject="Deploy Parousia v0.6.0",
        body="See invite.",
        from_addr="hermes@agents.test.com",
        ics=ics_payload,
    )
    body = _calendar_body(raw)
    assert "METHOD:REQUEST" in body
    assert "DTSTART" in body
    assert "ORGANIZER;CN=hermes:mailto:hermes@agents.test.com" in body


# ── 4. Handler wiring: `ics` flows through _handle_send_email ─────────


def _run_send_email(arguments, config, rate_limiter, redis_client, account=None, account_store=None):
    """Helper: synchronously invoke the async _handle_send_email."""
    from parousia.guard.mcp_server import _handle_send_email

    return asyncio.run(
        _handle_send_email(arguments, config, rate_limiter, redis_client, account, account_store)
    )


def test_handle_send_email_passes_ics_to_sender(mock_rate_limiter, mock_redis_client, monkeypatch):
    """The MCP handler threads `ics` into _smtp_send (the enrichment and
    MIME construction live in the sender, mcp_server only passes it)."""
    from parousia.guard import mcp_server as mcp_mod

    captured = {}

    def fake_send(**kw):
        captured.update(kw)
        return "msg-id"

    monkeypatch.setattr(mcp_mod, "_smtp_send", fake_send)

    config = ParousiaConfig(domain="agents.test.com")
    arguments = {
        "to": "human@example.com",
        "subject": "Deploy window",
        "body": "Please review.",
        "ics": VALID_ICS,
    }
    result = _run_send_email(arguments, config, mock_rate_limiter, mock_redis_client)
    data = json.loads(result[0].text)
    assert data["sent"] is True, data
    assert captured["ics"] == VALID_ICS
    assert captured["from_addr"] == "default@agents.test.com"
    assert captured["to"] == "human@example.com"


def test_handle_send_email_without_ics_omits_ics_kwarg(
    mock_rate_limiter, mock_redis_client, monkeypatch
):
    """No `ics` → the sender is called without an ics payload (the
    plain-text path is not a calendar send)."""
    from parousia.guard import mcp_server as mcp_mod

    captured = {}

    def fake_send(**kw):
        captured.update(kw)
        return "msg-id"

    monkeypatch.setattr(mcp_mod, "_smtp_send", fake_send)

    config = ParousiaConfig(domain="agents.test.com")
    arguments = {
        "to": "human@example.com",
        "subject": "Hi",
        "body": "Hello",
    }
    result = _run_send_email(arguments, config, mock_rate_limiter, mock_redis_client)
    data = json.loads(result[0].text)
    assert data["sent"] is True, data
    assert "ics" not in captured or captured.get("ics") is None


def test_rate_limiter_governs_ics_sends(mock_redis_client, tmp_path, monkeypatch):
    """The `ics` path goes through the same rate_limiter.check gate: a
    denied quota refuses the calendar send exactly like a plain send."""
    from parousia.auth.accounts import AccountStore
    from parousia.guard import mcp_server as mcp_mod
    from parousia.guard.mcp_server import _handle_send_email

    monkeypatch.setattr(mcp_mod, "_smtp_send", lambda **kw: "msg-id")
    config = ParousiaConfig(domain="agents.test.com")
    store = AccountStore(str(tmp_path / "rl.db"))
    store.connect()
    try:
        store.ensure_account("agent-a", rate_limit_per_hour=100)

        sent = []
        calls = []

        def deny_then_allow(agent_id, per_agent_limit=None):
            calls.append(1)
            if len(calls) == 1:
                return (False, 0, 60)  # first check denies
            sent.append("sent")
            return (True, 99, 3600)  # subsequent checks allow

        rl = MagicMock(spec=RateLimiter)
        rl.check.side_effect = deny_then_allow

        async def run():
            return await _handle_send_email(
                {
                    "to": "human@example.com",
                    "subject": "Deploy window",
                    "body": "Please review.",
                    "from_agent": "agent-a",
                    "ics": VALID_ICS,
                },
                config, rl, mock_redis_client,
                account=None, account_store=store,
            )

        # First call: limiter denies — refused, nothing "sent".
        data = json.loads(asyncio.run(run())[0].text)
        assert data["sent"] is False
        assert data["error"] == "rate_limit_exceeded"
        assert sent == []

        # Second call: limiter allows — the invite sends.
        data2 = json.loads(asyncio.run(run())[0].text)
        assert data2["sent"] is True, data2
    finally:
        store.close()
