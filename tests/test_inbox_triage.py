"""MP-5: inbox triage MCP tools — mark_read / archive / delete (issue #23).

An agent could check_inbox and read_message but not triage. Three tools,
mirroring read_message's account scoping EXACTLY (same dispatch-level
agent_id resolution; same per-message ownership refusal):

- mark_read(message_id) → {"marked": bool}
- archive(message_id) → {"archived": bool}
- delete(message_id) → {"deleted": bool} — over a NEW InboxStore.delete.

Invariants under test (the DONE gate): each tool works on the caller's OWN
message (and the store row actually changes/disappears); a cross-agent
message is refused and left untouched; a nonexistent id is a clean
not-found (never a 500); InboxStore.delete really removes the row and
returns False for a missing id.

Store-level tests run against a real SQLite tmp db. MCP tests drive the
REAL dispatch (the CallToolRequest handler registered by _build_server)
over a temp inbox with an authenticated non-first account (ricky) — the
same harness as tests/test_read_message.py.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from parousia.auth.accounts import AccountStore
from parousia.auth.mcp_auth import _auth_context, set_auth_context
from parousia.config import AgentConfig, ParousiaConfig
from parousia.guard import mcp_server
from parousia.guard.mcp_server import _build_server
from parousia.inbox.inbox_store import InboxMessage, InboxStore


# ── Fixtures ─────────────────────────────────────────────────────


def _make_inbox():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    return InboxStore(db_path)


def _msg(agent_id: str, message_id: str, **overrides) -> InboxMessage:
    defaults = {
        "id": message_id,
        "agent_id": agent_id,
        "sender": f"someone@{agent_id}.example",
        "recipient": f"{agent_id}@machinaparousia.ai",
        "subject": f"mail for {agent_id}",
        "body_text": f"{agent_id} private body",
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
def inbox(tmp_path):
    """A bare temp inbox (no seeds) for store-level tests."""
    store = InboxStore(str(tmp_path / "inbox.db"))
    yield store
    os.unlink(store.db_path)


@pytest.fixture
def server_and_inbox(monkeypatch):
    """The real _build_server() over a temp inbox and a two-agent config.

    config.agents[0] is "hermes" — the first agent. The inbox is seeded with
    one unread message for hermes and one for ricky so cross-agent refusals
    (and their non-effects on the store) are observable.
    """
    inbox = _make_inbox()
    inbox.store_message(_msg("hermes", "msg-hermes-1"))
    inbox.store_message(_msg("ricky", "msg-ricky-1"))

    config = ParousiaConfig(
        domain="agents.test.com",
        agents={"hermes": AgentConfig(), "ricky": AgentConfig()},
    )

    import fakeredis

    monkeypatch.setattr(mcp_server, "load_config", lambda: config)
    monkeypatch.setattr("parousia.inbox.inbox_store.InboxStore", lambda *a, **kw: inbox)
    monkeypatch.setattr(
        "parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: fakeredis.FakeRedis()
    )
    monkeypatch.setattr(mcp_server, "TemporalDB", lambda *a, **kw: _fake_temporal_db())
    monkeypatch.setattr(mcp_server, "MemoryRecorder", MagicMock)

    server, account_store = _build_server()

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


# ── Dispatch helpers (drive the REAL registered handler) ─────────


def _dispatch_raw(server, name, arguments):
    """One real CallToolRequest through the registered handler → raw text.

    The SDK's schema validation rejects malformed calls before dispatch —
    that returns prose, not JSON, hence the raw-text return for the
    missing-argument cases.
    """
    import mcp.types as types

    async def run():
        handler = server.request_handlers[types.CallToolRequest]
        result = await handler(
            types.CallToolRequest(
                params=CallToolRequestParams(name=name, arguments=arguments)
            )
        )
        return result.root.content[0].text

    return asyncio.run(run())


def _dispatch(server, name, arguments):
    return json.loads(_dispatch_raw(server, name, arguments))


@pytest.fixture
def as_ricky(server_and_inbox, ricky_account):
    """Authenticate as ricky for the dispatch, reset the contextvar after."""
    set_auth_context(ricky_account)
    yield server_and_inbox
    _auth_context.set(None)


# ── Store level: InboxStore.delete ──────────────────────────────


class TestStoreDelete:
    def test_delete_removes_row_and_returns_true(self, inbox):
        inbox.store_message(_msg("agent-x", "m-del-1"))
        assert inbox.delete("m-del-1") is True
        assert inbox.get_message("m-del-1") is None

    def test_delete_missing_returns_false(self, inbox):
        assert inbox.delete("never-existed") is False

    def test_delete_is_idempotent_false_on_second_call(self, inbox):
        inbox.store_message(_msg("agent-x", "m-del-2"))
        assert inbox.delete("m-del-2") is True
        assert inbox.delete("m-del-2") is False  # already gone

    def test_delete_only_the_named_row(self, inbox):
        inbox.store_message(_msg("agent-x", "m-keep"))
        inbox.store_message(_msg("agent-x", "m-drop"))
        assert inbox.delete("m-drop") is True
        assert inbox.get_message("m-drop") is None
        assert inbox.get_message("m-keep") is not None  # sibling untouched

    def test_delete_does_not_touch_other_agents_rows(self, inbox):
        inbox.store_message(_msg("agent-y", "m-y-1"))
        assert inbox.delete("m-y-1") is True
        # Store-level delete is by row id; the MCP layer adds the
        # agent-isolation (see the dispatch tests below).
        assert inbox.get_message("m-y-1") is None


# ── MCP: the three tools on the caller's OWN message ─────────────


class TestTriageOwnMessage:
    def test_mark_read_marks_the_store_row(self, as_ricky):
        server, inbox, _config = as_ricky
        assert inbox.get_message("msg-ricky-1").read is False  # pre-condition
        result = _dispatch(server, "mark_read", {"message_id": "msg-ricky-1"})
        assert result == {"marked": True}, result
        assert inbox.get_message("msg-ricky-1").read is True  # row changed

    def test_archive_archives_the_store_row(self, as_ricky):
        server, inbox, _config = as_ricky
        assert inbox.get_message("msg-ricky-1").archived is False
        result = _dispatch(server, "archive", {"message_id": "msg-ricky-1"})
        assert result == {"archived": True}, result
        assert inbox.get_message("msg-ricky-1").archived is True

    def test_delete_deletes_the_store_row(self, as_ricky):
        server, inbox, _config = as_ricky
        result = _dispatch(server, "delete", {"message_id": "msg-ricky-1"})
        assert result == {"deleted": True}, result
        assert inbox.get_message("msg-ricky-1") is None  # row disappeared

    def test_mark_read_already_read_still_true(self, as_ricky):
        """mark_read on an already-read message: idempotent True (the store
        returns rows_affected > 0 → but a second mark on a read row has
        nothing to change... the store still reports True: the row EXISTS
        and the UPDATE matches it). Verified against the real store."""
        server, inbox, _config = as_ricky
        _dispatch(server, "mark_read", {"message_id": "msg-ricky-1"})
        second = _dispatch(server, "mark_read", {"message_id": "msg-ricky-1"})
        assert second == {"marked": True}
        assert inbox.get_message("msg-ricky-1").read is True


# ── MCP: cross-agent refusal (the critical isolation) ─────────────


class TestTriageCrossAgentRefused:
    def test_mark_read_refuses_other_agents_message(self, as_ricky):
        server, inbox, _config = as_ricky
        result = _dispatch(server, "mark_read", {"message_id": "msg-hermes-1"})
        assert result == {"error": "not your message"}, result
        # hermes' row is untouched.
        assert inbox.get_message("msg-hermes-1").read is False

    def test_archive_refuses_other_agents_message(self, as_ricky):
        server, inbox, _config = as_ricky
        result = _dispatch(server, "archive", {"message_id": "msg-hermes-1"})
        assert result == {"error": "not your message"}, result
        assert inbox.get_message("msg-hermes-1").archived is False

    def test_delete_refuses_other_agents_message(self, as_ricky):
        """The dangerous one: delete must never remove another agent's row."""
        server, inbox, _config = as_ricky
        result = _dispatch(server, "delete", {"message_id": "msg-hermes-1"})
        assert result == {"error": "not your message"}, result
        assert inbox.get_message("msg-hermes-1") is not None  # row still there


# ── MCP: nonexistent id → clean not-found ─────────────────────────


class TestTriageNotFound:
    @pytest.mark.parametrize("tool,key", [
        ("mark_read", "marked"),
        ("archive", "archived"),
        ("delete", "deleted"),
    ])
    def test_missing_id_is_clean_not_found(self, as_ricky, tool, key):
        server, _inbox, _config = as_ricky
        result = _dispatch(server, tool, {"message_id": "no-such-id"})
        assert result == {"error": "message not found"}, result
        assert key not in result  # never a spoofed success

    @pytest.mark.parametrize("tool", ["mark_read", "archive", "delete"])
    def test_missing_argument_rejected_by_schema(self, as_ricky, tool):
        """No message_id at all → the SDK's schema validation rejects the
        call BEFORE dispatch (prose error naming the field — never a 500,
        never a silent success)."""
        server, _inbox, _config = as_ricky
        raw = _dispatch_raw(server, tool, {})
        assert "message_id" in raw, raw


# ── Registry: the three tools are listed ─────────────────────────


class TestRegistry:
    def test_triage_tools_listed(self, as_ricky):
        import mcp.types as types

        server, _inbox, _config = as_ricky

        async def run():
            handler = server.request_handlers[types.ListToolsRequest]
            listed = await handler(types.ListToolsRequest(params=None))
            return {t.name for t in listed.root.tools}

        names = asyncio.run(run())
        assert {"mark_read", "archive", "delete"} <= names

    def test_triage_schemas_require_message_id(self, as_ricky):
        import mcp.types as types

        server, _inbox, _config = as_ricky

        async def run():
            handler = server.request_handlers[types.ListToolsRequest]
            listed = await handler(types.ListToolsRequest(params=None))
            return {t.name: t.inputSchema for t in listed.root.tools}

        schemas = asyncio.run(run())
        for tool in ("mark_read", "archive", "delete"):
            assert schemas[tool]["required"] == ["message_id"], tool