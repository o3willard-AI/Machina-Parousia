"""MCP outbound server for Parousia Guard.

Exposes a send_email tool (Phase 1), temporal tools (Phase 2),
and spatial tools (Phase 3) to AI agents via MCP protocol.

Supports two transports:
  - stdio: traditional MCP stdio transport (for local/CLI use)
  - SSE: Server-Sent Events over HTTP (for remote agent access)
"""

import asyncio
import contextvars
import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

import redis as redis_lib
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from parousia.memory.recorder import MemoryRecorder
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from parousia.auth.accounts import Account, AccountStore
from parousia.auth.mcp_auth import authenticate_mcp, get_auth_context, set_auth_context
from parousia.config import load_config
from parousia.guard.email_sender import send_email as _smtp_send
from parousia.guard.rate_limiter import RateLimiter
from parousia.inbox.wait_for import wait_for_email as _wait_for_email
from parousia.spatial.browser_pool import BrowserPoolManager
from parousia.spatial.serializer import SpatialSerializer
from parousia.spatial.tools import ALL_SPATIAL_SCHEMAS, SpatialToolHandlers
from parousia.temporal.db import TemporalDB
from parousia.temporal.tools import ALL_TEMPORAL_SCHEMAS, TemporalToolHandlers
from parousia.totp.store import TOTPStore
from parousia.totp.tools import ALL_TOTP_SCHEMAS, TotpHandlers
from parousia.vault.credential_store import CredentialStore

logger = logging.getLogger("parousia.mcp")

# ── MP-1: SSE session→account map + session-id capture ─────────────
#
# _SESSION_ACCOUNTS maps the SSE session id (UUID assigned by the MCP
# transport at GET /sse) → the Account authenticated there via Bearer
# token.  Module-level because _build_server (which owns the tool
# dispatch) runs before run_mcp_server_sse; one process serves one
# transport, so a module-level map is also the simplest test surface.
_SESSION_ACCOUNTS: dict[UUID, Account | None] = {}

# Recorder for the session-id capture.  handle_sse sets a fresh list via
# .set() around the transport's connect_sse(); the transport assigns
# `session_id = uuid4()` and inserts it into `_read_stream_writers`
# synchronously inside connect_sse — BEFORE yielding the streams — so the
# insertion is observed by the recorder in the CONNECTING task's context.
# Concurrent connections cannot cross-talk: contextvars are per-task, so
# each handle_sse reads its own recorder.
_session_capture: contextvars.ContextVar = contextvars.ContextVar(
    "parousia_session_capture", default=None
)


class _ObservableWriterMap(dict):
    """dict that reports insertions to the active session-capture recorder.

    Replaces the SDK transport's plain `_read_stream_writers` dict so
    handle_sse can capture the exact session id assigned to THIS
    connection.  This removes the previous `list(...)[-1]`
    grab-the-last-key hack, which mis-keys under concurrent connections
    (dict insertion order is connection-arrival order, not THIS session).
    """

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        recorder = _session_capture.get()
        if recorder is not None:
            recorder.append(key)


def _request_from_mcp_context(server: Server) -> Any:
    """The ASGI Request attached to the current MCP message, if any.

    The SSE transport attaches the POST Request to every incoming message
    (ServerMessageMetadata.request_context); the low-level Server exposes
    it as ``server.request_context.request`` (mcp>=1.9; some versions name
    the accessor get_request_context()).  Returns None on the stdio
    transport (no request) and outside a message context.
    """
    try:
        ctx = server.request_context
    except LookupError:
        return None
    except AttributeError:
        getter = getattr(server, "get_request_context", None)
        if getter is None:
            return None
        try:
            ctx = getter()
        except LookupError:
            return None
    return getattr(ctx, "request", None)


def _resolve_sse_account(server: Server) -> tuple[Account | None, bool]:
    """Resolve the authenticated account for a tool call that arrived via SSE.

    Returns ``(account, via_sse)``: ``via_sse`` is True when the call came
    through the SSE POST endpoint (its Request carries a ``session_id``
    query param).

    Why not the auth contextvar alone: the contextvar set in the POST
    wrapper does NOT cross the task boundary — the SDK runs tool handlers
    from the SSE read stream in the session task's context (a copy of the
    GET /sse task's context), not the POST task's.  So the account is
    resolved through the message's own Request: its session_id keys
    _SESSION_ACCOUNTS, populated at GET /sse after Bearer auth.
    """
    request = _request_from_mcp_context(server)
    if request is None:
        return None, False
    try:
        session_id_param = request.query_params.get("session_id")
    except Exception:
        return None, False
    if not session_id_param:
        # A Request without a session_id param is not an SSE POST.
        return None, False
    try:
        sid = UUID(hex=session_id_param)
    except ValueError:
        return None, True
    return _SESSION_ACCOUNTS.get(sid), True


def _resolve_agent_id(arguments: dict) -> str:
    """Resolve agent_id from an explicit argument; "default" when absent.

    MP-7: the legacy YAML agents block is GONE — AccountStore is the single
    registry, so there is no first-configured-agent fallback here. The
    account-scoped tools resolve through the authenticated account in the
    dispatch (contextvar → SSE session → "default"); this helper only
    honors an explicit ``agent_id`` argument.
    """
    if arguments.get("agent_id"):
        return arguments["agent_id"]
    return "default"


def _triage_bulk(name: str, message_ids: list, agent_id: str, inbox_store) -> dict:
    """Bulk mark_read/archive with fail-closed agent isolation.

    Every id must resolve to a message owned by ``agent_id``; any missing
    or foreign id refuses the WHOLE batch (nothing is changed) — the same
    isolation as the single-message path, never a partial success that
    would silently drop a cross-agent refusal. Returns
    ``{"marked": n, "count": n}`` / ``{"archived": n, "count": n}``, or an
    ``{"error": ...}`` dict on refusal.
    """
    owned = []
    for mid in message_ids:
        msg = inbox_store.get_message(mid)
        if msg is None:
            return {"error": "message not found"}
        if msg.agent_id != agent_id:
            return {"error": "not your message"}
        owned.append(mid)
    if name == "mark_read":
        n = inbox_store.mark_read_bulk(agent_id, owned)
        return {"marked": n, "count": n}
    n = inbox_store.archive_bulk(agent_id, owned)
    return {"archived": n, "count": n}


def _build_server() -> tuple[Server, AccountStore]:
    """Create and configure the MCP server with email + temporal tools.

    Returns a tuple of (server, account_store) so that run_mcp_server_sse
    can use the real AccountStore for SSE auth.
    """
    config = load_config()

    # Initialize rate limiter (Phase 1)
    redis_client = redis_lib.Redis(
        host=config.redis.host,
        port=config.redis.port,
        db=config.redis.db,
        socket_connect_timeout=2,
    )
    rate_limiter = RateLimiter(
        redis_client,
        per_agent_per_hour=config.rate_limits.per_agent_per_hour,
        domain_per_day=config.rate_limits.domain_per_day,
    )

    # Initialize temporal DB and tool handlers (Phase 2)
    temporal_db = TemporalDB()
    temporal_db.connect()
    temporal_db.create_tables()
    temporal_handlers = TemporalToolHandlers(config, temporal_db)

    # Initialize spatial browser pool and tool handlers (Phase 3)
    browser_pool = BrowserPoolManager(config.spatial)
    spatial_serializer = SpatialSerializer()
    # MP-3 (part B): server-side credential store for fill_credentials.
    # Falls back to an in-memory store when the deployed path is unwritable
    # (tests, non-root) — the tool then answers "no credential stored"
    # rather than failing server startup.
    try:
        credential_store = CredentialStore(
            config.credential_store.db_path,
            config.credential_store.key_path,
        )
        credential_store.connect()
    except Exception:
        credential_store = CredentialStore(":memory:", str(Path(tempfile.gettempdir()) / "parousia-cred.key"))
        credential_store.connect()
    spatial_handlers = SpatialToolHandlers(
        config, browser_pool, spatial_serializer, credential_store=credential_store
    )

    # MP-8: server-side TOTP vault (RFC 6238). Same unwritable-path fallback
    # as the credential store — the tools then answer "store unavailable"
    # rather than failing server startup (tests, non-root).
    try:
        totp_store = TOTPStore(
            config.totp_store.db_path,
            config.totp_store.key_path,
        )
        totp_store.connect()
    except Exception:
        totp_store = TOTPStore(":memory:", str(Path(tempfile.gettempdir()) / "parousia-totp.key"))
        totp_store.connect()
    totp_handlers = TotpHandlers(totp_store)

    server = Server("parousia-guard-mcp")

    # Initialize account store for MCP auth (Story D)
    # Falls back to in-memory when filesystem path is unwritable (tests, non-root).
    try:
        account_store = AccountStore()
        account_store.connect()
    except PermissionError:
        account_store = AccountStore(":memory:")
        account_store.connect()

    # Initialize inbox store for check_inbox MCP tool (Story F)
    from parousia.inbox.inbox_store import InboxStore
    inbox_store = InboxStore()

    # Initialize memory recorder for tool call logging
    memory_recorder = MemoryRecorder()

    # ── Phase 1 + Phase 2 tool listing ───────────────

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        tools = [
            Tool(
                name="send_email",
                description=(
                    "Send an email through the Parousia agent mail system. "
                    "Rate-limited: 100/hr per agent, 500/day domain-wide."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address"},
                        "subject": {"type": "string", "description": "Email subject line"},
                        "body": {"type": "string", "description": "Plain-text email body"},
                        "reply_to": {"type": "string", "description": "Optional Reply-To address"},
                        "from_agent": {"type": "string", "description": "Optional agent ID to send from (defaults to first configured agent)"},
                    },
                    "required": ["to", "subject", "body"],
                },
            )
        ]
        # Add temporal tools (Phase 2)
        for schema in ALL_TEMPORAL_SCHEMAS:
            tools.append(Tool(**schema))
        # Add spatial tools (Phase 3)
        for schema in ALL_SPATIAL_SCHEMAS:
            tools.append(Tool(**schema))
        # MP-8: TOTP authenticator tools (issue #11 workstream 3)
        for schema in ALL_TOTP_SCHEMAS:
            tools.append(Tool(**schema))
        # Add inbox tool (Story F)
        tools.append(Tool(
            name="check_inbox",
            description="Check your Parousia inbox for new emails.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID (defaults to authenticated account)",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only return unread messages",
                        "default": True,
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived messages (default false — archived mail is excluded so archiving shrinks the listing)",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum messages to return",
                        "default": 10,
                    },
                },
            },
        ))
        # MP-1 (Fix 2): read_message — check_inbox only previews the body;
        # this returns the full stored message by id.
        tools.append(Tool(
            name="read_message",
            description="Read a full message by ID (returns the complete body).",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Message ID from check_inbox",
                    },
                },
                "required": ["message_id"],
            },
        ))
        # MP-6 (part A): wait_for_email — couple the browser to the inbox
        # (double opt-in). Polls the caller's OWN inbox for a matching
        # message and surfaces the body links so the agent can browse_to
        # the confirmation link directly.
        tools.append(Tool(
            name="wait_for_email",
            description=(
                "Wait for an email matching a pattern (case-insensitive "
                "substring on subject OR sender) to arrive in YOUR inbox, "
                "within a timeout. Returns the message plus its body links "
                "(so you can browse_to a confirmation link), or "
                '{"found": false} on timeout.'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Substring to match on subject or sender (case-insensitive)",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Overall bound in ms. Default 30000, cap 120000.",
                        "default": 30000,
                    },
                },
                "required": ["pattern"],
            },
        ))
        # MP-5: inbox triage — mark_read / archive / delete, agent-isolated
        # exactly like read_message (the message must be the caller's own).
        for _triage_name, _triage_verb, _triage_key in (
            ("mark_read", "Mark", "marked"),
            ("archive", "Archive", "archived"),
            ("delete", "Delete", "deleted"),
        ):
            # MP-10: mark_read / archive accept an optional bulk message_ids
            # array so a watcher can triage a batch in ONE round trip.
            # delete stays single-message only (batching a destructive op
            # is deliberately out of scope).
            _props: dict[str, Any] = {
                "message_id": {
                    "type": "string",
                    "description": "Message ID from check_inbox",
                },
            }
            _required = ["message_id"]
            if _triage_name in ("mark_read", "archive"):
                _props["message_ids"] = {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Bulk: message IDs to triage in one call (alternative to message_id)",
                }
                _required = []
            tools.append(Tool(
                name=_triage_name,
                description=(
                    f"{_triage_verb} one of YOUR inbox messages as "
                    f"{_triage_key} by message ID. Only operates on messages "
                    "stored for the authenticated agent; another agent's "
                    "message id is refused."
                ),
                inputSchema={
                    "type": "object",
                    "properties": _props,
                    "required": _required,
                },
            ))
        return tools

    # ── Tool dispatch ────────────────────────────────

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        # ── Story D + MP-1 (Fix 1): account scoping ──
        # SSE transport: the account is authenticated at GET /sse (Bearer)
        # and reaches dispatch through the message's own Request (see
        # _resolve_sse_account — the POST wrapper's contextvar does not
        # cross the task boundary). Stdio transport: auth optional —
        # contextvar, then the "default" sentinel.
        account = get_auth_context()
        sse_account, via_sse = _resolve_sse_account(server)
        if account is None:
            account = sse_account
        if via_sse and account is None:
            # MP-1: fail closed. An SSE call with no authenticated account
            # must NEVER fall back to any first-agent default — that
            # fallback is what scoped a failed auth to the first agent's
            # inbox in the live incident. (The "default" sentinel survives
            # only on the stdio/no-auth path below.)
            logger.error(
                "SSE tool call rejected: no authenticated account (tool=%s)", name
            )
            return [TextContent(
                type="text",
                text=json.dumps({"error": "no authenticated account"}),
            )]
        agent_id_override = account.account_id if account else None

        # RAE L0: attribute every recorded action to the authenticated account's
        # human sponsor (empty string when there is no account or sponsor).
        sponsor = account.sponsor_id if account and account.sponsor_id else ""
        agent_id = agent_id_override or _resolve_agent_id(arguments)

        # ── Phase 1: send_email ────────────────────
        if name == "send_email":
            # account is the authenticated Account (None on stdio w/o auth).
            # account_store is the module-scoped singleton (passed via closure).
            # MP-7: AccountStore is the single registry — send_email validates
            # from_agent against it and has no other place to look.
            result_content = await _handle_send_email(arguments, config, rate_limiter, redis_client, account, account_store)
            try:
                memory_recorder.record_tool_call(
                    "send_email", arguments,
                    json.loads(result_content[0].text), agent_id,
                    sponsor=sponsor,
                )
            except Exception:
                pass
            return result_content

        # ── Story F: check_inbox ────────────────
        if name == "check_inbox":
            unread_only = arguments.get("unread_only", True)
            include_archived = arguments.get("include_archived", False)
            limit_val = arguments.get("limit", 10)
            messages = inbox_store.list_messages(
                agent_id, limit=limit_val, unread_only=unread_only,
                include_archived=include_archived,
            )
            result_data = []
            for msg in messages:
                result_data.append({
                    "id": msg.id,
                    "sender": msg.sender,
                    "subject": msg.subject,
                    "received_at": msg.received_at,
                    "read": msg.read,
                    "archived": msg.archived,
                    "body_preview": msg.body_text[:200],
                })
            result_dict = {"messages": result_data, "count": len(result_data), "unread_only": unread_only, "include_archived": include_archived}
            result = [TextContent(
                type="text",
                text=json.dumps(result_dict),
            )]
            try:
                memory_recorder.record_tool_call("check_inbox", arguments, result_dict, agent_id, sponsor=sponsor)
            except Exception:
                pass
            return result

        # ── MP-1 (Fix 2): read_message ────────
        if name == "read_message":
            message_id = arguments.get("message_id")
            if not message_id:
                result_dict = {"error": "message_id is required"}
            else:
                msg = inbox_store.get_message(message_id)
                if msg is None:
                    result_dict = {"error": "message not found"}
                elif msg.agent_id != agent_id:
                    # Agent isolation: never leak another agent's mail —
                    # a message stored for a different agent_id reads as
                    # "not your message", indistinguishable from absent.
                    result_dict = {"error": "not your message"}
                else:
                    result_dict = {
                        "id": msg.id,
                        "sender": msg.sender,
                        "recipient": msg.recipient,
                        "subject": msg.subject,
                        "body_text": msg.body_text,
                        "body_html": msg.body_html,
                        "received_at": msg.received_at,
                        "read": msg.read,
                        "archived": msg.archived,
                    }
            result = [TextContent(
                type="text",
                text=json.dumps(result_dict),
            )]
            try:
                memory_recorder.record_tool_call("read_message", arguments, result_dict, agent_id, sponsor=sponsor)
            except Exception:
                pass
            return result

        # ── MP-5: inbox triage — mark_read / archive / delete ─────
        # Mirrors read_message's isolation exactly: resolve the message,
        # refuse anything not stored for THIS agent (indistinguishable
        # from absent), then act. The agent_id resolution above is the
        # same SSE-account scoping check_inbox/read_message use.
        if name in ("mark_read", "archive", "delete"):
            message_id = arguments.get("message_id")
            message_ids = arguments.get("message_ids")

            # MP-10: bulk path — mark_read/archive accept a message_ids
            # array. Every id must resolve to a message owned by THIS agent
            # (any foreign or missing id refuses the whole batch, nothing
            # changed). delete stays single-message only.
            if message_ids is not None:
                if name == "delete":
                    result_dict = {"error": "message_ids is not supported for delete"}
                else:
                    result_dict = _triage_bulk(name, message_ids, agent_id, inbox_store)
            elif not message_id:
                result_dict = {"error": "message_id or message_ids is required"}
            else:
                msg = inbox_store.get_message(message_id)
                if msg is None:
                    # Clean not-found, never a 500.
                    result_dict = {"error": "message not found"}
                elif msg.agent_id != agent_id:
                    # Same isolation as read_message: a message stored for
                    # a different agent_id is NEVER touched — reported
                    # "not your message", indistinguishable from absent.
                    result_dict = {"error": "not your message"}
                elif name == "mark_read":
                    result_dict = {"marked": inbox_store.mark_read(message_id)}
                elif name == "archive":
                    result_dict = {"archived": inbox_store.archive(message_id)}
                else:  # delete
                    result_dict = {"deleted": inbox_store.delete(message_id)}
            result = [TextContent(
                type="text",
                text=json.dumps(result_dict),
            )]
            try:
                memory_recorder.record_tool_call(name, arguments, result_dict, agent_id, sponsor=sponsor)
            except Exception:
                pass
            return result

        # ── MP-6 (part A): wait_for_email ─────────────
        if name == "wait_for_email":
            pattern = arguments.get("pattern", "")
            timeout_ms = arguments.get("timeout_ms", 30000)
            # Account-scoped by construction: the helper polls
            # inbox_store.list_messages(agent_id) — only this agent's mail.
            result_dict = await _wait_for_email(
                inbox_store, agent_id, pattern, timeout_ms
            )
            result = [TextContent(
                type="text",
                text=json.dumps(result_dict),
            )]
            # No fact recorded: the result carries message bodies (a
            # confirmation mail is not a presence fact; a found/timeout
            # summary alone would be noise — and the helper's message
            # content must never flow into the memory layer unfiltered).
            return result

        # ── MP-8: TOTP authenticator tools ─────────────
        # Account-scoped like the email tools: agent_id above is the
        # authenticated account (SSE session) or the stdio sentinel;
        # the store keys everything by agent_id, so an agent can only
        # enroll/read/list/remove its OWN enrollments.
        if name in ("enroll_totp", "get_totp_code", "list_totp", "remove_totp"):
            result_str = await totp_handlers.dispatch(name, arguments, agent_id)
            result = [TextContent(type="text", text=result_str)]
            # No fact recorded: the results describe 2FA secrets/codes —
            # never recorded into any memory layer (and the payloads never
            # carry the secret anyway, by construction).
            return result

        # ── Phase 2: temporal tools ────────────────
        temporal_names = {s["name"] for s in ALL_TEMPORAL_SCHEMAS}
        if name in temporal_names:
            result_str = temporal_handlers.dispatch(name, arguments, agent_id)
            result = [TextContent(type="text", text=result_str)]
            try:
                memory_recorder.record_tool_call(name, arguments, json.loads(result_str), agent_id, sponsor=sponsor)
            except Exception:
                pass
            return result

        # ── Phase 3: spatial tools ────────────────
        spatial_names = {s["name"] for s in ALL_SPATIAL_SCHEMAS}
        if name in spatial_names:
            result_str = await spatial_handlers.dispatch(name, arguments, agent_id)
            result = [TextContent(type="text", text=result_str)]
            try:
                memory_recorder.record_tool_call(name, arguments, json.loads(result_str), agent_id, sponsor=sponsor)
            except Exception:
                pass
            return result

        # ── Unknown tool ───────────────────────────
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Unknown tool: {name}"}),
        )]

    return server, account_store


async def _handle_send_email(
    arguments: dict, config, rate_limiter: RateLimiter, redis_client,
    account=None, account_store: AccountStore | None = None,
) -> list[TextContent]:
    """Handle send_email tool call (Phase 1).

    MP-7: AccountStore is the SINGLE registry. The authenticated
    ``account`` (None on the stdio/no-auth path) is the strongest signal;
    an authenticated caller may only send ``from_agent`` as itself. On
    stdio without auth, ``from_agent`` must name a real AccountStore
    account (or be omitted for the "default" sentinel). There is no legacy
    YAML fallback: an agent unknown to AccountStore is refused with the
    active AccountStore account ids as ``available_agents``.
    """
    to = arguments["to"]
    subject = arguments["subject"]
    body = arguments["body"]
    reply_to = arguments.get("reply_to")

    # ── Resolve agent_id ────────────────────────
    # Authenticated caller's account is the strongest signal. On stdio
    # without auth the caller may pass from_agent explicitly; with no
    # from_agent the "default" sentinel applies (MP-7: no first-agent
    # fallback — AccountStore is the single registry).
    if account is not None:
        requested_agent = arguments.get("from_agent")
        if requested_agent and requested_agent != account.account_id:
            # Caller is authenticated as ``account`` but is trying to send
            # *as* a different agent. MP-7: with the legacy registry gone,
            # cross-account sends are refused outright when the target
            # exists as an active AccountStore account — and also when no
            # store is available to validate it (fail closed).
            other_acct = (
                account_store.get_account(requested_agent)
                if account_store is not None
                else None
            )
            if other_acct is None or other_acct.status == "active":
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "sent": False,
                        "error": f"You are authenticated as '{account.account_id}', not '{requested_agent}'.",
                    }),
                )]
        agent_id = requested_agent or account.account_id
    else:
        # Stdio (no auth): explicit from_agent or the "default" sentinel.
        agent_id = arguments.get("from_agent") or "default"

    # ── Validate agent exists (AccountStore: the single registry) ──
    if agent_id != "default":
        acct = (
            account_store.get_account(agent_id)
            if account_store is not None
            else None
        )
        if acct is None:
            available = (
                account_store.list_account_ids(active_only=True)
                if account_store is not None
                else []
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "sent": False,
                    "error": f"Unknown agent: {agent_id}",
                    "available_agents": available,
                }),
            )]
        if acct.status != "active":
            return [TextContent(
                type="text",
                text=json.dumps({
                    "sent": False,
                    "error": f"Account '{agent_id}' is {acct.status}",
                }),
            )]

    # Rate limit check
    allowed, remaining, reset_seconds = rate_limiter.check(agent_id)
    if not allowed:
        return [TextContent(
            type="text",
            text=json.dumps({
                "sent": False,
                "error": "rate_limit_exceeded",
                "rate_limit_remaining": remaining,
                "rate_limit_reset_seconds": reset_seconds,
            }),
        )]

    # Human-in-the-loop approval check
    if config.approval.enabled and agent_id in config.approval.require_approval_for:
        from parousia.guard.approval_queue import ApprovalQueue
        from_addr = f"{agent_id}@{config.domain}"
        aq = ApprovalQueue(redis_client)
        approval_id = aq.enqueue(
            agent_id=agent_id,
            to=to,
            subject=subject,
            body=body,
            from_addr=from_addr,
            reply_to=reply_to,
            ttl_hours=config.approval.queue_ttl_hours,
        )
        return [TextContent(
            type="text",
            text=json.dumps({
                "sent": False,
                "queued_for_approval": True,
                "approval_id": approval_id,
                "message": "Email held for human review. It will be sent upon approval.",
            }),
        )]

    from_addr = f"{agent_id}@{config.domain}"
    try:
        message_id = _smtp_send(
            to=to, subject=subject, body=body,
            from_addr=from_addr, reply_to=reply_to,
        )
    except Exception as e:
        logger.error("SMTP send failed", extra={"error": str(e)})
        return [TextContent(
            type="text",
            text=json.dumps({"sent": False, "error": str(e)}),
        )]

    logger.info(
        "email sent",
        extra={"agent_id": agent_id, "to": to, "message_id": message_id},
    )

    return [TextContent(
        type="text",
        text=json.dumps({
            "sent": True,
            "message_id": message_id,
            "rate_limit_remaining": remaining,
            "rate_limit_reset_seconds": reset_seconds,
        }),
    )]


async def run_mcp_server():
    """Start the MCP server with stdio transport."""
    server, _account_store = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        logger.info("MCP server running on stdio transport")
        await server.run(
            read_stream, write_stream, server.create_initialization_options(),
        )


async def run_mcp_server_sse(host: str = "0.0.0.0", port: int = 8081):
    """Start the MCP server with SSE transport for remote agent access.

    Args:
        host: Bind address (default: 0.0.0.0 for public access).
        port: TCP port (default: 8081).
    """
    import uvicorn

    server, account_store = _build_server()
    sse = SseServerTransport("/messages/")

    # MP-1 (Fix 1): observe the session ids the transport assigns. The SDK
    # inserts its `session_id = uuid4()` into `_read_stream_writers`
    # synchronously inside connect_sse(), before yielding the streams, so
    # handle_sse can capture the exact id assigned to THIS connection (see
    # _ObservableWriterMap) instead of guessing `list(...)[-1]`.
    sse._read_stream_writers = _ObservableWriterMap(sse._read_stream_writers)

    async def handle_sse(request):
        """GET /sse — authenticate via Bearer token, then run the SSE transport."""
        from starlette.responses import JSONResponse

        # Validate Authorization header against AccountStore.
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            account = authenticate_mcp(account_store, {"Authorization": auth_header})
        except ValueError as e:
            return JSONResponse(
                status_code=401,
                content={"detail": str(e)},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Open the SSE connection; the transport assigns a session_id.
        # MP-1: capture the id THIS connection was assigned via the
        # observable writer map (per-task recorder — safe under concurrent
        # connections), never `list(...)[-1]`.
        capture: list[UUID] = []
        capture_token = _session_capture.set(capture)
        try:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                _session_capture.reset(capture_token)
                capture_token = None
                if not capture:
                    # Cannot happen with the SDK's connect_sse (it always
                    # inserts exactly one session id before yielding), but
                    # fail loudly rather than mis-scope if that ever changes.
                    logger.error("failed to capture the MCP session id at GET /sse")
                    return JSONResponse(
                        status_code=500,
                        content={"detail": "failed to capture MCP session id"},
                    )
                session_id = capture[-1]
                _SESSION_ACCOUNTS[session_id] = account
                try:
                    await server.run(
                        streams[0], streams[1],
                        server.create_initialization_options(),
                    )
                finally:
                    _SESSION_ACCOUNTS.pop(session_id, None)
        finally:
            if capture_token is not None:
                _session_capture.reset(capture_token)
        return Response()

    # ── Wrapped POST handler: inject account into contextvar ─────
    # NOTE (MP-1): this contextvar injection is kept for in-process
    # harnesses and any SDK version that runs tool handlers in the POST
    # request's context. It does NOT reach the dispatch task on the
    # current SDK (handlers run from the SSE read stream in the session
    # task's context) — the authoritative SSE path is the message's own
    # Request → session_id → _SESSION_ACCOUNTS (see _resolve_sse_account).
    _original_post = sse.handle_post_message

    async def handle_post_message(scope, receive, send):
        """POST /messages — look up session account, set contextvar, forward."""
        from starlette.requests import Request as _Request
        request = _Request(scope, receive)
        session_id_param = request.query_params.get("session_id")
        if session_id_param:
            try:
                sid = UUID(hex=session_id_param)
                account = _SESSION_ACCOUNTS.get(sid)
                if account is not None:
                    set_auth_context(account)
            except ValueError:
                pass
        await _original_post(scope, receive, send)

    routes = [
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=handle_post_message),
    ]

    starlette_app = Starlette(routes=routes)

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="info",
    )
    server_instance = uvicorn.Server(config)
    logger.info("MCP server running on SSE transport (http://%s:%d/sse)", host, port)
    await server_instance.serve()


def main():
    """Entry point for CLI."""
    asyncio.run(run_mcp_server())


if __name__ == "__main__":
    main()
