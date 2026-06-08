"""MCP tool handler implementations for spatial tools.

browse_to, interact, extract_page_state — registered on the existing MCP server (port 8081).
"""

import json
import logging
from typing import Any, Optional

from parousia.config import ParousiaConfig
from parousia.spatial.browser_pool import BrowserPoolManager
from parousia.spatial.serializer import SpatialSerializer

logger = logging.getLogger("parousia.spatial.tools")


# ── Tool schemas ──────────────────────────────────────


def browse_to_schema() -> dict:
    return {
        "name": "browse_to",
        "description": (
            "Navigate to a URL and optionally extract content. "
            "Returns the DOM as SDOM (structured DOM) for further processing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
                "timeout_ms": {"type": "integer", "description": "Timeout in milliseconds. Default: 30000."},
                "extract_mode": {
                    "type": "string",
                    "enum": ["standard", "content_only", "interactive_only"],
                    "description": "Extraction mode. Default: 'standard'.",
                },
            },
            "required": ["url"],
        },
    }


def interact_schema() -> dict:
    return {
        "name": "interact",
        "description": (
            "Perform an interaction with a web element (click, type, scroll_into_view, "
            "select, check, uncheck, hover, press)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Element ID to interact with"},
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "scroll_into_view", "select", "check", "uncheck", "hover", "press"],
                    "description": "Interaction action to perform",
                },
                "text": {"type": "string", "description": "Text to type (for 'type' action)"},
                "timeout_ms": {"type": "integer", "description": "Timeout in milliseconds. Default: 30000."},
            },
            "required": ["id", "action"],
        },
    }


def extract_page_state_schema() -> dict:
    return {
        "name": "extract_page_state",
        "description": (
            "Extract the current page state as SDOM (structured DOM). "
            "Returns SDOM content for further processing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["full", "changes", "context_only"],
                    "description": "Extraction mode. Default: 'full'.",
                },
            },
        },
    }


ALL_SPATIAL_SCHEMAS = [
    browse_to_schema(),
    interact_schema(),
    extract_page_state_schema(),
]


# ── Tool handler registry ─────────────────────────────


class SpatialToolHandlers:
    """Handle MCP tool invocations for spatial tools."""

    def __init__(self, config: ParousiaConfig, browser_pool: BrowserPoolManager, serializer: SpatialSerializer):
        self.config = config
        self.browser_pool = browser_pool
        self.serializer = serializer

    def dispatch(self, name: str, arguments: dict[str, Any], agent_id: str) -> str:
        """Route tool call to the correct handler and return JSON result."""
        handlers = {
            "browse_to": self._handle_browse_to,
            "interact": self._handle_interact,
            "extract_page_state": self._handle_extract_page_state,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown spatial tool: {name}"})
        try:
            result = handler(arguments, agent_id)
        except Exception as e:
            logger.error("spatial tool error", extra={"tool": name, "error": str(e)})
            result = {"error": str(e)}
        return json.dumps(result)

    # ── browse_to ──────────────────────────────────────

    def _handle_browse_to(self, args: dict, agent_id: str) -> dict:
        url = args["url"]
        timeout_ms = args.get("timeout_ms", 30000)
        extract_mode = args.get("extract_mode", "standard")

        browser = self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            # Navigate to the URL
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms)
            
            # Extract content based on mode
            sdom_content = self.serializer.to_sdom(page.content(), extract_mode)
            
            return {
                "url": url,
                "extracted": True,
                "sdom": sdom_content,
            }
        except Exception as e:
            logger.error("browse_to failed", extra={"url": url, "error": str(e)})
            return {"error": f"Failed to browse to {url}: {str(e)}"}

    # ── interact ───────────────────────────────────────

    def _handle_interact(self, args: dict, agent_id: str) -> dict:
        element_id = args["id"]
        action = args["action"]
        text = args.get("text")
        timeout_ms = args.get("timeout_ms", 30000)

        browser = self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            page = browser.new_page()
            
            # Perform the interaction based on action type
            if action == "click":
                page.click(f"#{element_id}", timeout=timeout_ms)
            elif action == "type":
                if text is None:
                    return {"error": "Text required for 'type' action"}
                page.type(f"#{element_id}", text, timeout=timeout_ms)
            elif action == "scroll_into_view":
                page.scroll_into_view(f"#{element_id}")
            elif action == "select":
                value = args.get("value")
                if not value:
                    return {"error": "Value required for 'select' action"}
                page.select_option(f"#{element_id}", value)
            elif action == "check":
                page.check(f"#{element_id}")
            elif action == "uncheck":
                page.uncheck(f"#{element_id}")
            elif action == "hover":
                page.hover(f"#{element_id}")
            elif action == "press":
                key = args.get("key")
                if not key:
                    return {"error": "Key required for 'press' action"}
                page.press(f"#{element_id}", key)
            else:
                return {"error": f"Unknown interaction action: {action}"}
            
            return {
                "id": element_id,
                "action": action,
                "success": True,
            }
        except Exception as e:
            logger.error("interact failed", extra={"element_id": element_id, "action": action, "error": str(e)})
            return {"error": f"Failed to interact with {element_id} using {action}: {str(e)}"}

    # ── extract_page_state ─────────────────────────────

    def _handle_extract_page_state(self, args: dict, agent_id: str) -> dict:
        mode = args.get("mode", "full")

        browser = self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            page = browser.new_page()
            
            # Extract content based on mode
            sdom_content = self.serializer.to_sdom(page.content(), mode)
            
            return {
                "mode": mode,
                "extracted": True,
                "sdom": sdom_content,
            }
        except Exception as e:
            logger.error("extract_page_state failed", extra={"mode": mode, "error": str(e)})
            return {"error": f"Failed to extract page state in {mode} mode: {str(e)}"}