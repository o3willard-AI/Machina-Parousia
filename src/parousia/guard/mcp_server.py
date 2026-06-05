"""MCP outbound server for Parousia Guard.

Exposes a send_email tool to AI agents via MCP protocol (stdio transport).
Rate-limited via RateLimiter before SMTP send.
"""

import asyncio
import json
import logging
from typing import Any, Optional

import redis as redis_lib
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from parousia.config import load_config
from parousia.guard.email_sender import send_email as _smtp_send
from parousia.guard.rate_limiter import RateLimiter

logger = logging.getLogger("parousia.mcp")


def _build_server() -> Server:
    """Create and configure the MCP server with send_email tool."""
    config = load_config()

    # Initialize rate limiter
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

    server = Server("parousia-guard-mcp")

    # Register list_tools handler
    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return [
            Tool(
                name="send_email",
                description=(
                    "Send an email through the Parousia agent mail system. "
                    "Rate-limited: 100/hr per agent, 500/day domain-wide."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Recipient email address",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject line",
                        },
                        "body": {
                            "type": "string",
                            "description": "Plain-text email body",
                        },
                        "reply_to": {
                            "type": "string",
                            "description": "Optional Reply-To address",
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            )
        ]

    # Register call_tool handler
    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        if name != "send_email":
            raise ValueError(f"Unknown tool: {name}")

        to = arguments["to"]
        subject = arguments["subject"]
        body = arguments["body"]
        reply_to = arguments.get("reply_to")

        # Determine agent ID (first configured agent in Phase 1)
        agent_ids = list(config.agents.keys())
        agent_id = agent_ids[0] if agent_ids else "default"

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

        # Send via SMTP
        from_addr = f"{agent_id}@{config.domain}"
        try:
            message_id = _smtp_send(
                to=to,
                subject=subject,
                body=body,
                from_addr=from_addr,
                reply_to=reply_to,
            )
        except Exception as e:
            logger.error("SMTP send failed", extra={"error": str(e)})
            return [TextContent(
                type="text",
                text=json.dumps({
                    "sent": False,
                    "error": str(e),
                }),
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

    return server


async def run_mcp_server():
    """Start the MCP server with stdio transport."""
    server = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        logger.info("MCP server running on stdio transport")
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main():
    """Entry point for CLI."""
    asyncio.run(run_mcp_server())


if __name__ == "__main__":
    main()
