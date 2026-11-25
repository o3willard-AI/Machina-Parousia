"""MP-1 tests: check_inbox account scoping (Fix 1) + read_message (Fix 2).

Fix 1 regression (live incident): a newly-onboarded agent ("ricky") called
check_inbox over SSE and got config.agents[0]'s ("hermes") inbox — the
session→account map was mis-keyed (`list(...)[-1]`) and the silent fallback
scoped a failed auth to the first agent. These tests drive the REAL dispatch
(the handler registered by _build_server) with an authenticated non-first
account and assert the inbox is scoped to that account only, plus the
fail-closed behavior when no account resolves.

Fix 2: read_message returns the full body (check_inbox previews 200 chars),
refuses bogus ids, and never leaks another agent's mail.
"""
import asyncio
import contextvars
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from parousia.auth.accounts import AccountStore
from parousia.auth.mcp_auth import set_auth_context
from parousia.config import ParousiaConfig
from parousia.guard import mcp_server
from parousia.guard.mcp_server import _build_server
from parousia.inbox.inbox_store import InboxMessage, InboxStore


# ── Fixtures: a buildable server over a temp inbox ─────────────────


def _make_inbox():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    return InboxStore(db_path)


def _msg(agent_id: str, message_id: str, body: str = "short body", **overrides) -> InboxMessage:
    defaults = {
        "id": message_id,
        "agent_id": agent_id,
        "sender": f"someone@{agent_id}.example",
        "recipient": f"{agent_id}@machinaparousia.ai",
        "subject": f"mail for {agent_id}",
        "body_text": body,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "read": False,
        "archived": False,
    }
    defaults.update(overrides)
    return InboxMessage(**defaults)


def _fake_temporal_db():
    from parousia.temporal.db import TemporalDB

    db = TemporalDB(db_path=":memory:")
    db.connect()
    db.create_tables()
    return db


@pytest.fixture
def server_and_inbox(monkeypatch, tmp_path):
    """The real _build_server() over a temp inbox and a two-agent config.

    config.agents[0] is "hermes" — the agent a FAILED auth used to fall back
    to. The inbox is seeded with one message for hermes and one for ricky so
    any scoping mistake is observable.
    """
    inbox = _make_inbox()
    inbox.store_message(_msg("hermes", "msg-hermes-1", body="hermes private body"))
    inbox.store_message(_msg("ricky", "msg-ricky-1", body="ricky private body"))

    # MP-7: no legacy agents dict — the harness accounts live in the
    # AccountStore (ricky_account fixture); config carries no registry.
    config = ParousiaConfig(domain="agents.test.com")

    import fakeredis

    monkeypatch.setattr(mcp_server, "load_config", lambda: config)
    monkeypatch.setattr("parousia.inbox.inbox_store.InboxStore", lambda *a, **kw: inbox)
    monkeypatch.setattr(
        "parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: fakeredis.FakeRedis()
    )
    monkeypatch.setattr(mcp_server, "TemporalDB", lambda *a, **kw: _fake_temporal_db())
    monkeypatch.setattr(mcp_server, "MemoryRecorder", MagicMock)

    server, account_store = _build_server()

    # Module-level SSE session map: isolate + clear around each test.
    saved = dict(mcp_server._SESSION_ACCOUNTS)
    monkeypatch.setattr(mcp_server, "_SESSION_ACCOUNTS", {})
    yield server, inbox, config
    mcp_server._SESSION_ACCOUNTS.clear()
    mcp_server._SESSION_ACCOUNTS.update(saved)
    os.unlink(inbox.db_path)


@pytest.fixture
def ricky_account(tmp_path):
    """A real authenticated Account for a NON-first agent (ricky)."""
    store = AccountStore(str(tmp_path / "accounts.db"))
    store.connect()
    account, _raw_key = store.create_account("ricky", tier="free")
    yield account
    store.close()


def _call_tool(server):
    """The CallToolRequest handler registered by _build_server."""
    import mcp.types as types

    return server.request_handlers[types.CallToolRequest]


def _request(name: str, arguments: dict) -> CallToolRequest:
    return CallToolRequest(
        params=CallToolRequestParams(name=name, arguments=arguments)
    )


def _result_text(result) -> str:
    """First TextContent text from a CallTool handler result."""
    payload = getattr(result, "root", result)  # ServerResult → CallToolResult
    content = getattr(payload, "content", None)
    assert content, f"no content in result: {result!r}"
    return content[0].text


def _as_dict(text: str) -> dict:
    return json.loads(text)


async def _dispatch(server, name: str, arguments: dict) -> dict:
    handler = _call_tool(server)
    result = await handler(_request(name, arguments))
    return _as_dict(_result_text(result))


def _sse_request_ctx(server, session_id: UUID):
    """Set the low-level request_ctx to a POST Request carrying session_id.

    Mirrors what the SSE transport does for every POST /messages: the SDK
    attaches the ASGI Request to the incoming message
    (ServerMessageMetadata.request_context) and the low-level Server exposes
    it via server.request_context — this is how the authenticated account
    reaches the dispatch task (the POST wrapper's contextvar does not cross
    the task boundary).
    """
    from starlette.requests import Request

    from mcp.server.lowlevel import server as lowlevel_server
    from mcp.shared.context import RequestContext

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/messages/",
        "query_string": f"session_id={session_id.hex}".encode(),
        "headers": [],
        "root_path": "",
        "http_version": "1.1",
        "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    ctx = RequestContext(
        "test", None, None, None, request=Request(scope, receive)
    )
    return lowlevel_server.request_ctx.set(ctx)


# ── Fix 1: check_inbox account scoping ────────────────────────────


@pytest.mark.asyncio
async def test_check_inbox_scoped_to_non_first_account(server_and_inbox, ricky_account):
    """MP-1 regression: an authenticated NON-first agent gets ONLY its mail.

    ricky authenticates (Bearer → Account) and calls check_inbox with no
    agent_id argument: the result must be ricky's messages only — never
    config.agents[0]'s ("hermes"), which is where a failed auth used to
    land. Exercises the contextvar auth path (in-process auth).
    """
    server, inbox, config = server_and_inbox
    set_auth_context(ricky_account)
    try:
        result = await _dispatch(server, "check_inbox", {})
        assert result["count"] == 1, result
        assert result["messages"][0]["id"] == "msg-ricky-1", result
        returned_ids = {m["id"] for m in result["messages"]}
        assert "msg-hermes-1" not in returned_ids, (
            "hermes' mail leaked to an authenticated ricky"
        )
    finally:
        # reset the contextvar for the next test
        from parousia.auth.mcp_auth import _auth_context

        _auth_context.set(None)


@pytest.mark.asyncio
async def test_check_inbox_scoped_via_sse_request_context(server_and_inbox, ricky_account):
    """The SSE path: the account reaches dispatch through the message's own
    Request (session_id → _SESSION_ACCOUNTS), not the POST task's contextvar.

    Simulates a full SSE round-trip at the dispatch level: GET /sse stored
    the authenticated account under the session id; the POST message carries
    that session_id. No contextvar is set anywhere.
    """
    server, inbox, config = server_and_inbox
    session_id = uuid4()
    mcp_server._SESSION_ACCOUNTS[session_id] = ricky_account
    token = _sse_request_ctx(server, session_id)
    try:
        result = await _dispatch(server, "check_inbox", {})
        assert result["count"] == 1, result
        assert result["messages"][0]["id"] == "msg-ricky-1", result
        assert "msg-hermes-1" not in {m["id"] for m in result["messages"]}
    finally:
        from mcp.server.lowlevel import server as lowlevel_server

        lowlevel_server.request_ctx.reset(token)


@pytest.mark.asyncio
async def test_sse_call_without_account_fails_closed(server_and_inbox):
    """MP-1 fail-closed: an SSE call whose session resolves NO account must
    return an explicit error — never fall back to config.agents[0].

    This is the exact incident shape: the session→account map misses (here:
    a session_id the map never saw), and the old code silently served the
    FIRST agent's inbox.
    """
    server, inbox, config = server_and_inbox
    unknown_session = uuid4()  # authenticated at no GET /sse
    token = _sse_request_ctx(server, unknown_session)
    try:
        result = await _dispatch(server, "check_inbox", {})
        assert result == {"error": "no authenticated account"}, result
        # And specifically: no hermes mail in the error path.
        assert "msg-hermes-1" not in json.dumps(result)
    finally:
        from mcp.server.lowlevel import server as lowlevel_server

        lowlevel_server.request_ctx.reset(token)


@pytest.mark.asyncio
async def test_stdio_without_auth_resolves_to_default_sentinel(server_and_inbox):
    """MP-7: the stdio/no-auth path resolves to the "default" sentinel.

    The legacy first-configured-agent fallback is GONE (AccountStore is
    the single registry) — an unauthenticated check_inbox with no
    agent_id NEVER lands in a real agent's inbox (here: hermes' mail is
    invisible; "default" has an empty inbox).
    """
    server, inbox, config = server_and_inbox
    result = await _dispatch(server, "check_inbox", {})
    assert result["count"] == 0, result
    assert result["messages"] == [], result
    # And hermes' mail is still there — untouched by the sentinel path.
    assert inbox.get_message("msg-hermes-1") is not None


# ── Fix 1: session-id capture (replaces list(...)[-1]) ──────────────


@pytest.mark.asyncio
async def test_connect_sse_capture_gets_this_sessions_id():
    """handle_sse's capture mechanism records the id the transport assigned
    THIS connection — proven against the real SseServerTransport.connect_sse
    (the SDK inserts `session_id = uuid4()` into `_read_stream_writers`
    synchronously before yielding the streams)."""
    from mcp.server.sse import SseServerTransport

    sse = SseServerTransport("/messages/")
    sse._read_stream_writers = mcp_server._ObservableWriterMap(sse._read_stream_writers)

    # A pre-existing session from an EARLIER connection: under the old
    # `list(...)[-1]` grab-the-last-key this is exactly the hazard.
    earlier_session = uuid4()
    sse._read_stream_writers[earlier_session] = object()

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/sse",
        "headers": [(b"host", b"testserver")],
        "query_string": b"",
        "root_path": "",
        "http_version": "1.1",
        "scheme": "http",
    }

    async def receive():
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    class _Unwind(Exception):
        def __init__(self, captured):
            self.captured = captured

    capture = []
    keys_while_connected = None
    token = mcp_server._session_capture.set(capture)
    try:
        async with sse.connect_sse(scope, receive, send) as _streams:
            mcp_server._session_capture.reset(token)
            token = None
            keys_while_connected = set(sse._read_stream_writers.keys())
            # Unwind out of the (long-lived) SSE context manager. The
            # transport's task group wraps the raise in an ExceptionGroup.
            raise _Unwind(list(capture))
    except* _Unwind as done:
        capture = done.exceptions[0].captured
    finally:
        if token is not None:
            mcp_server._session_capture.reset(token)

    assert len(capture) == 1, capture
    captured_id = capture[0]
    assert isinstance(captured_id, UUID)
    # The captured id is the one the transport assigned THIS connection:
    # present while connected, gone after disconnect (the transport pops
    # its own entry on exit — and must pop ONLY its own).
    assert captured_id in keys_while_connected
    assert captured_id not in sse._read_stream_writers
    assert earlier_session in sse._read_stream_writers, (
        "this connection's cleanup must not remove another session's entry"
    )
    assert captured_id != earlier_session, (
        "capture must get THIS connection's id, not the pre-existing session"
    )


def test_observable_writer_map_ignores_other_contexts():
    """Concurrent connections cannot cross-talk: an insert from a context
    that has no active recorder (another task's) is not captured; the
    connecting task's own insert is."""
    from mcp.server.sse import SseServerTransport  # noqa: F401  (doc anchor)

    writers = mcp_server._ObservableWriterMap()
    capture = []
    token = mcp_server._session_capture.set(capture)
    try:
        # Another task's insert: fresh Context, no recorder → not captured.
        foreign_uuid, own_uuid = uuid4(), uuid4()
        contextvars.Context().run(lambda: writers.__setitem__(foreign_uuid, object()))
        assert capture == [], capture
        # Our insert: this task's recorder → captured.
        writers[own_uuid] = object()
        assert capture == [own_uuid], capture
        assert foreign_uuid in writers and own_uuid in writers
    finally:
        mcp_server._session_capture.reset(token)


# ── Fix 2: read_message ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_message_returns_full_body(server_and_inbox, ricky_account):
    """read_message returns the COMPLETE body — not the 200-char preview."""
    server, inbox, config = server_and_inbox
    long_body = "R" * 5000 + " end of the real body"
    inbox.store_message(_msg("ricky", "msg-long", body=long_body))

    set_auth_context(ricky_account)
    try:
        result = await _dispatch(
            server, "read_message", {"message_id": "msg-long"}
        )
        assert result["id"] == "msg-long", result
        assert result["body_text"] == long_body, "full body must be returned"
        assert result["sender"] == "someone@ricky.example"
        assert result["subject"] == "mail for ricky"
        assert result["read"] is False
        assert result["archived"] is False
        assert result["received_at"]
    finally:
        from parousia.auth.mcp_auth import _auth_context

        _auth_context.set(None)


@pytest.mark.asyncio
async def test_read_message_not_found(server_and_inbox, ricky_account):
    """A bogus id returns an explicit error."""
    server, inbox, config = server_and_inbox
    set_auth_context(ricky_account)
    try:
        result = await _dispatch(
            server, "read_message", {"message_id": "no-such-id"}
        )
        assert result == {"error": "message not found"}, result
    finally:
        from parousia.auth.mcp_auth import _auth_context

        _auth_context.set(None)


@pytest.mark.asyncio
async def test_read_message_refuses_other_agents_mail(server_and_inbox, ricky_account):
    """Agent isolation: ricky cannot read hermes' message — "not your
    message", never the body."""
    server, inbox, config = server_and_inbox
    set_auth_context(ricky_account)
    try:
        result = await _dispatch(
            server, "read_message", {"message_id": "msg-hermes-1"}
        )
        assert result == {"error": "not your message"}, result
        assert "hermes private body" not in json.dumps(result)
    finally:
        from parousia.auth.mcp_auth import _auth_context

        _auth_context.set(None)


@pytest.mark.asyncio
async def test_read_message_fails_closed_on_sse_without_account(server_and_inbox):
    """Fix 1 + Fix 2 together: an SSE read_message with no authenticated
    account errors out rather than reading config.agents[0]'s mail."""
    server, inbox, config = server_and_inbox
    token = _sse_request_ctx(server, uuid4())
    try:
        result = await _dispatch(
            server, "read_message", {"message_id": "msg-hermes-1"}
        )
        assert result == {"error": "no authenticated account"}, result
        assert "hermes private body" not in json.dumps(result)
    finally:
        from mcp.server.lowlevel import server as lowlevel_server

        lowlevel_server.request_ctx.reset(token)


@pytest.mark.asyncio
async def test_read_message_requires_message_id(server_and_inbox, ricky_account):
    """Missing message_id is an explicit error, not a crash."""
    server, inbox, config = server_and_inbox
    set_auth_context(ricky_account)
    try:
        handler = _call_tool(server)
        result = await handler(_request("read_message", {}))
        # The tool schema requires message_id, so the SDK's input validation
        # rejects the call before dispatch — an explicit, parseable error.
        assert "message_id" in _result_text(result), result
    finally:
        from parousia.auth.mcp_auth import _auth_context

        _auth_context.set(None)


@pytest.mark.asyncio
async def test_read_message_is_listed(server_and_inbox):
    """The tool listing exposes read_message next to check_inbox."""
    import mcp.types as types

    server, inbox, config = server_and_inbox
    handler = server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest(params=None))
    tools = {t.name for t in result.root.tools}
    assert "read_message" in tools, tools
    assert "check_inbox" in tools, tools