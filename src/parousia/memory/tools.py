"""MCP tool schemas + handlers for the memory read side (MP-11, issue #13).

Two tools, account-scoped BY CONSTRUCTION:
- ``mem0_search(query, top_k=5)`` — semantic recall across the agent's own
  recorded facts → ``{results: [{memory, score}], count}``.
- ``mem0_profile()`` — the agent's full recorded fact list (the "what have
  I done" dump) → ``{facts: [{memory, created_at?}], count}``.

The schemas take NO user_id/agent_id argument: the mem0 user_id is derived
ONLY from the authenticated agent_id the MCP dispatch hands in (SSE
account or the stdio "default" sentinel — same as the TOTP tools). An agent
reads its own facts and nothing else; there is no path from tool input to
another agent's scope.

Memory reads are NEVER themselves recorded (no re-entrant
``record_tool_call`` from a read — the dispatch branch for these tools
skips the recorder; also keeps the loop from feeding its own output back
into itself).
"""

import json
import logging
from typing import Any, Optional

from parousia.memory.query import (
    MAX_TOP_K,
    DEFAULT_TOP_K,
    MemoryQuery,
    MemoryReadError,
)

logger = logging.getLogger("parousia.memory")


# ── Schemas ─────────────────────────────────────────────────────


def mem0_search_schema() -> dict:
    return {
        "name": "mem0_search",
        "description": (
            "MP-11: semantic recall across YOUR recorded facts. Every "
            "write-side tool call (email sent, event scheduled, page "
            "browsed...) was recorded as a natural-language fact; this "
            "searches them. ACCOUNT-SCOPED: results come only from the "
            "authenticated agent's own memory (user_id is derived from the "
            "account, never from arguments) — another agent's facts are "
            "never visible. Returns {results: [{memory, score}, ...], "
            "count}. Read-only: this call is never itself recorded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to recall — a natural-language question or topic.",
                },
                "top_k": {
                    "type": "integer",
                    "description": f"Maximum facts returned. Default {DEFAULT_TOP_K}, max {MAX_TOP_K}.",
                    "default": DEFAULT_TOP_K,
                },
            },
            "required": ["query"],
        },
    }


def mem0_profile_schema() -> dict:
    return {
        "name": "mem0_profile",
        "description": (
            "MP-11: the full recorded-fact list for the AUTHENTICATED agent "
            "(the 'what have I done' dump) → {facts: [{memory, created_at?}"
            ", ...], count}. ACCOUNT-SCOPED by construction: no user_id "
            "argument exists — the scope is the authenticated account. "
            "Read-only: this call is never itself recorded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    }


ALL_MEMORY_SCHEMAS = [
    mem0_search_schema(),
    mem0_profile_schema(),
]


# ── Handlers ─────────────────────────────────────────────────────


class MemoryQueryHandlers:
    """Handle MCP tool invocations for the memory read side."""

    def __init__(self, query: MemoryQuery):
        self.query = query

    async def dispatch(self, name: str, arguments: dict, agent_id: str) -> str:
        """Route a memory tool call and return its JSON result."""
        handlers = {
            "mem0_search": self._handle_search,
            "mem0_profile": self._handle_profile,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown memory tool: {name}"})
        try:
            result = await handler(arguments or {}, agent_id)
        except MemoryReadError as exc:
            # Fail soft: a Qdrant/mem0 read failure (or a tripped breaker)
            # is a clean tool error — never a 500 that leaks the stack.
            return json.dumps({"error": f"memory unavailable: {exc}"})
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("memory tool error", extra={"tool": name, "error": str(exc)})
            return json.dumps({"error": f"memory tool failed: {exc}"})
        return json.dumps(result)

    async def _handle_search(self, args: dict, agent_id: str) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        top_k = args.get("top_k", DEFAULT_TOP_K)
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = DEFAULT_TOP_K
        # ACCOUNT-SCOPED: the mem0 user_id comes ONLY from the
        # authenticated agent_id (never from arguments).
        user_id = self.query.user_id_for(agent_id)
        results = self.query.search(query, user_id, top_k=top_k)
        return {"results": results, "count": len(results)}

    async def _handle_profile(self, args: dict, agent_id: str) -> dict:
        user_id = self.query.user_id_for(agent_id)
        facts = self.query.profile(user_id)
        return {"facts": facts, "count": len(facts)}