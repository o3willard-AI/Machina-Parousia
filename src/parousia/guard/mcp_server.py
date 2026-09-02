"""MCP outbound server for Parousia Guard.

Exposes a send_email tool (Phase 1), temporal tools (Phase 2),
and spatial tools (Phase 3) to AI agents via MCP protocol.

Supports two transports:
  - stdio: traditional MCP stdio transport (for local/CLI use)
  - SSE: Server-Sent Events over HTTP (for remote agent access)
"""

import asyncio
import json
import logging
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
from parousia.spatial.browser_pool import BrowserPoolManager
from parousia.spatial.serializer import SpatialSerializer
from parousia.spatial.tools import ALL_SPATIAL_SCHEMAS, SpatialToolHandlers
from parousia.temporal.db import TemporalDB
from parousia.temporal.tools import ALL_TEMPORAL_SCHEMAS, TemporalToolHandlers

logger = logging.getLogger("parousia.mcp")


def _resolve_agent_id(config, arguments: dict) -> str:
    """Resolve agent_id from arguments, then config fallback."""
    if arguments.get("agent_id"):
        return arguments["agent_id"]
    agent_ids = list(config.agents.keys())
    return agent_ids[0] if agent_ids else "default"


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
    spatial_handlers = SpatialToolHandlers(config, browser_pool, spatial_serializer)

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
                    "limit": {
                        "type": "integer",
                        "description": "Maximum messages to return",
                        "default": 10,
                    },
                },
            },
        ))
        return tools

    # ── Tool dispatch ────────────────────────────────

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        # ── Story D: MCP auth ──────────────────────
        # SSE transport: account injected via context var.
        # Stdio transport: auth optional — falls back to config.
        account = get_auth_context()
        if account:
            agent_id_override = account.account_id
        else:
            agent_id_override = None

        # ── Phase 1: send_email ────────────────────
        if name == "send_email":
            # account is the authenticated Account (None on stdio w/o auth).
            # account_store is the module-scoped singleton (passed via closure).
            # Both are consulted so send_email can validate from_agent against
            # BOTH registries (AccountStore + legacy config.agents).
            result_content = await _handle_send_email(arguments, config, rate_limiter, redis_client, account, account_store)
            try:
                memory_recorder.record_tool_call(
                    "send_email", arguments,
                    json.loads(result_content[0].text), agent_id
                )
            except Exception:
                pass
            return result_content

        agent_id = agent_id_override or _resolve_agent_id(config, arguments)

        # ── Story F: check_inbox ────────────────
        if name == "check_inbox":
            unread_only = arguments.get("unread_only", True)
            limit_val = arguments.get("limit", 10)
            messages = inbox_store.list_messages(
                agent_id, limit=limit_val, unread_only=unread_only
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
            result_dict = {"messages": result_data, "count": len(result_data), "unread_only": unread_only}
            result = [TextContent(
                type="text",
                text=json.dumps(result_dict),
            )]
            try:
                memory_recorder.record_tool_call("check_inbox", arguments, result_dict, agent_id)
            except Exception:
                pass
            return result

        # ── Phase 2: temporal tools ────────────────
        temporal_names = {s["name"] for s in ALL_TEMPORAL_SCHEMAS}
        if name in temporal_names:
            result_str = temporal_handlers.dispatch(name, arguments, agent_id)
            result = [TextContent(type="text", text=result_str)]
            try:
                memory_recorder.record_tool_call(name, arguments, json.loads(result_str), agent_id)
            except Exception:
                pass
            return result

        # ── Phase 3: spatial tools ────────────────
        spatial_names = {s["name"] for s in ALL_SPATIAL_SCHEMAS}
        if name in spatial_names:
            result_str = await spatial_handlers.dispatch(name, arguments, agent_id)
            result = [TextContent(type="text", text=result_str)]
            try:
                memory_recorder.record_tool_call(name, arguments, json.loads(result_str), agent_id)
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

    The authenticated ``account`` (None when the caller used stdio transport
    without auth) is consulted first: if the caller supplies ``from_agent`` it
    must match the authenticated account, otherwise the agent_id falls back to
    ``from_agent`` or the first configured agent.  Agent validity is resolved
    against AccountStore (modern source of truth) with a legacy config.agents
    fallback so that config-only agents (mr-krabs, atlas) keep working.
    """
    to = arguments["to"]
    subject = arguments["subject"]
    body = arguments["body"]
    reply_to = arguments.get("reply_to")

    # ── Resolve agent_id ────────────────────────
    # Authenticated caller's account is the strongest signal.  On stdio
    # without auth the caller may pass from_agent explicitly; otherwise we
    # fall back to the first configured agent.
    if account is not None:
        requested_agent = arguments.get("from_agent")
        if requested_agent and requested_agent != account.account_id:
            # Caller is authenticated as ``account`` but is trying to send
            # *as* a different agent.  Only allow it if that agent doesn't
            # exist as an AccountStore account (legacy passthrough).
            if account_store is not None:
                other_acct = account_store.get_account(requested_agent)
                if other_acct is not None and other_acct.status == "active":
                    return [TextContent(
                        type="text",
                        text=json.dumps({
                            "sent": False,
                            "error": f"You are authenticated as '{account.account_id}', not '{requested_agent}'.",
                        }),
                    )]
        agent_id = requested_agent or account.account_id
    else:
        # Stdio (no auth): behave as before — from_agent or first config agent.
        agent_id = arguments.get("from_agent")
        if not agent_id:
            agent_ids = list(config.agents.keys())
            agent_id = agent_ids[0] if agent_ids else "default"

    # ── Validate agent exists (both registries) ──
    if agent_id != "default":
        acct = account_store.get_account(agent_id) if account_store is not None else None
        if acct is not None:
            if acct.status != "active":
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "sent": False,
                        "error": f"Account '{agent_id}' is {acct.status}",
                    }),
                )]
        elif agent_id not in config.agents:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "sent": False,
                    "error": f"Unknown agent: {agent_id}",
                    "available_agents": list(config.agents.keys()),
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

    # ── Per-session account mapping ───────────────────────────────
    # Maps session_id (UUID) → authenticated Account | None.
    # Populated at GET /sse (connection time); consumed at POST /messages
    # (tool-call time) to inject the contextvar into dispatch.
    _session_accounts: dict[UUID, Account | None] = {}

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
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            # SseServerTransport._read_stream_writers gets a new entry on connect.
            session_id = list(sse._read_stream_writers.keys())[-1]
            _session_accounts[session_id] = account
            try:
                await server.run(
                    streams[0], streams[1],
                    server.create_initialization_options(),
                )
            finally:
                _session_accounts.pop(session_id, None)
        return Response()

    # ── Wrapped POST handler: inject account into contextvar ─────
    # The MCP SDK's ContextSendStream captures contextvars.copy_context() at
    # send() time; _spawn() then runs handlers via sender_ctx.run(...).
    # So setting the contextvar HERE (in the POST handler, before the message
    # enters the read stream) propagates into handle_call_tool's
    # get_auth_context() call.
    _original_post = sse.handle_post_message

    async def handle_post_message(scope, receive, send):
        """POST /messages — look up session account, set contextvar, forward."""
        from starlette.requests import Request as _Request
        request = _Request(scope, receive)
        session_id_param = request.query_params.get("session_id")
        if session_id_param:
            try:
                sid = UUID(hex=session_id_param)
                account = _session_accounts.get(sid)
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
