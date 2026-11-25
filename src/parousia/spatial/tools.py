"""MCP tool handler implementations for spatial tools.

browse_to, interact, extract_page_state — registered on the existing MCP server (port 8081).

All handlers are async: they drive ``playwright.async_api`` from inside the
MCP server's asyncio event loop (the synchronous Playwright API cannot be used
there). ``dispatch`` and every ``_handle_*`` method are coroutines.
"""

import json
import logging
import re
import socket
from typing import Any, Optional

from parousia.config import ParousiaConfig
from parousia.spatial.browser_pool import BrowserPoolManager
from parousia.spatial.serializer import SpatialSerializer
from parousia.vault.credential_store import CredentialStore, normalize_origin

logger = logging.getLogger("parousia.spatial.tools")


# ── MP-3 helpers ─────────────────────────────────────────────────


def _client_hints_for(user_agent: str) -> dict:
    """Derive consistent Sec-CH-UA client-hint headers for a UA string.

    The brands/version mirror the UA's Chrome version so the high-entropy
    hints never contradict the UA header (a WAF tell in itself).
    """
    match = re.search(r"Chrome/(\d+)", user_agent or "")
    version = match.group(1) if match else "142"
    if "Windows" in (user_agent or ""):
        platform = "Windows"
    elif "Macintosh" in (user_agent or "") or "Mac OS" in (user_agent or ""):
        platform = "macOS"
    else:
        platform = "Linux"
    return {
        "Sec-CH-UA": (
            f'"Chromium";v="{version}", '
            f'"Google Chrome";v="{version}", '
            '"Not?A_Brand";v="99"'
        ),
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": f'"{platform}"',
    }


def _effective_egress_ip() -> str:
    """The host's source IP for internet egress — no packet leaves.

    UDP ``connect`` only selects the default-route source address; the
    destination is TEST-NET-1 (never routed). Offline-safe, no external
    service dependency. Returns "unknown" when there is no route.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(2.0)
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "unknown"


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
                "user_agent": {
                    "type": "string",
                    "description": "Optional per-navigation User-Agent override (consistent Sec-CH-UA client hints are applied). Default: the configured browser identity.",
                },
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


def fill_credentials_schema() -> dict:
    return {
        "name": "fill_credentials",
        "description": (
            "Fill a saved credential (username + password) into the current "
            "page's login form. Origin-bound: the current page origin must "
            "match the credential's stored origin. Returns a fill summary "
            "only — never the secret values."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Origin the credential is stored for (e.g. https://site.example)",
                },
                "handle": {
                    "type": "string",
                    "description": "Credential handle (as stored by the operator)",
                },
                "submit": {
                    "type": "boolean",
                    "description": "Also click a submit button after filling. Default: false.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Timeout in milliseconds for field lookups. Default: 30000.",
                },
            },
            "required": ["origin", "handle"],
        },
    }


ALL_SPATIAL_SCHEMAS = [
    browse_to_schema(),
    interact_schema(),
    extract_page_state_schema(),
    fill_credentials_schema(),
]


# ── Tool handler registry ─────────────────────────────


class SpatialToolHandlers:
    """Handle MCP tool invocations for spatial tools."""

    def __init__(
        self,
        config: ParousiaConfig,
        browser_pool: BrowserPoolManager,
        serializer: SpatialSerializer,
        credential_store: Optional[CredentialStore] = None,
    ):
        self.config = config
        self.browser_pool = browser_pool
        self.serializer = serializer
        # MP-3 (part B): server-side credential store for fill_credentials.
        # None → the tool answers "credential store unavailable" (and can
        # never fall back to anything less structured).
        self.credential_store = credential_store

    async def dispatch(self, name: str, arguments: dict[str, Any], agent_id: str) -> str:
        """Route tool call to the correct handler and return JSON result."""
        handlers = {
            "browse_to": self._handle_browse_to,
            "interact": self._handle_interact,
            "extract_page_state": self._handle_extract_page_state,
            "fill_credentials": self._handle_fill_credentials,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown spatial tool: {name}"})
        try:
            result = await handler(arguments, agent_id)
        except Exception as e:
            logger.error("spatial tool error", extra={"tool": name, "error": str(e)})
            result = {"error": str(e)}
        return json.dumps(result)

    async def _apply_identity(
        self, page: Any, user_agent: Optional[str], timeout_ms: int
    ) -> None:
        """MP-3 (part A): apply a per-navigation UA override + client hints.

        The persistent context already carries the configured identity at
        launch (browser_pool._launch_kwargs). A ``user_agent`` argument
        overrides it for THIS navigation via CDP ``Emulation.setUserAgentOverride``
        (with full ``userAgentMetadata`` so the client hints stay consistent)
        and mirrors the high-entropy headers via ``set_extra_http_headers``.
        CDP unavailability (non-chromium, detached) degrades to the context UA.
        """
        if not user_agent:
            return
        hints = _client_hints_for(user_agent)
        try:
            cdp = await page.context.new_cdp_session(page)
            version = re.search(r"Chrome/(\d+)", user_agent)
            version = version.group(1) if version else "142"
            platform = hints["Sec-CH-UA-Platform"].strip('"')
            brands = [
                {"brand": "Chromium", "version": version},
                {"brand": "Google Chrome", "version": version},
                {"brand": "Not?A_Brand", "version": "99"},
            ]
            await cdp.send(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": user_agent,
                    "acceptLanguage": "en-US,en;q=0.9",
                    "platform": platform,
                    "userAgentMetadata": {
                        "brands": brands,
                        "fullVersionList": brands,
                        "fullVersion": f"{version}.0.0.0",
                        "platform": platform,
                        "platformVersion": "15.0.0",
                        "architecture": "x86",
                        "model": "",
                        "mobile": False,
                        "bitness": "64",
                        "wow64": False,
                    },
                },
            )
        except Exception as e:
            logger.debug("CDP UA override unavailable; using context UA", extra={"error": str(e)})
        try:
            await page.set_extra_http_headers(hints)
        except Exception as e:
            logger.debug("extra client-hint headers unavailable", extra={"error": str(e)})

    async def _egress_identity(self, page: Any, agent_id: str) -> dict:
        """MP-3 (part A): the egress identity a browse result reports.

        Effective UA (read from the page — reflects any per-navigation
        override), the host's effective source IP (no packet sent), and the
        configured egress route (proxy server or "direct"), so an agent can
        tell "site refusing bots" from "wrong egress / site down".
        """
        ua = ""
        try:
            ua = await page.evaluate("() => navigator.userAgent")
        except Exception:
            # Page detached / evaluate unavailable → the configured identity.
            cfg = getattr(self.config, "spatial", None)
            ua = getattr(cfg, "user_agent", "") or ""
        proxy = ""
        try:
            raw = self.browser_pool.proxy_for(agent_id)
            # Only a real string counts as a route — anything else
            # (missing helper, unexpected type) reports "direct".
            if isinstance(raw, str):
                proxy = raw
        except Exception:
            proxy = ""
        return {
            "user_agent": ua,
            "ip": _effective_egress_ip(),
            "proxy": proxy or "direct",
        }

    # ── browse_to ──────────────────────────────────────

    async def _handle_browse_to(self, args: dict, agent_id: str) -> dict:
        url = args["url"]
        timeout_ms = args.get("timeout_ms", 30000)
        extract_mode = args.get("extract_mode", "standard")
        user_agent = args.get("user_agent")

        browser = await self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            # Navigate the agent's persistent page
            page = browser.page
            # MP-3: per-navigation UA override + consistent client hints
            await self._apply_identity(page, user_agent, timeout_ms)
            response = await page.goto(url, timeout=timeout_ms)
            status = response.status if response is not None else 200

            # Extract content as SDOM, then serialize to a JSON-safe dict
            sdom = self.serializer.to_sdom(await page.content(), url, status)

            return {
                "url": url,
                "extracted": True,
                "sdom": sdom.model_dump(),
                # MP-3: the egress identity this fetch presented — so the
                # agent can distinguish "refusing bots" from "site down".
                "egress": await self._egress_identity(page, agent_id),
            }
        except Exception as e:
            logger.error("browse_to failed", extra={"url": url, "error": str(e)})
            return {"error": f"Failed to browse to {url}: {str(e)}"}

    # ── interact ───────────────────────────────────────

    async def _handle_interact(self, args: dict, agent_id: str) -> dict:
        element_id = args["id"]
        action = args["action"]
        text = args.get("text")
        timeout_ms = args.get("timeout_ms", 30000)

        browser = await self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            page = browser.page

            # Perform the interaction based on action type
            if action == "click":
                await page.click(f"#{element_id}", timeout=timeout_ms)
            elif action == "type":
                if text is None:
                    return {"error": "Text required for 'type' action"}
                await page.type(f"#{element_id}", text, timeout=timeout_ms)
            elif action == "scroll_into_view":
                await page.scroll_into_view(f"#{element_id}")
            elif action == "select":
                value = args.get("value")
                if not value:
                    return {"error": "Value required for 'select' action"}
                await page.select_option(f"#{element_id}", value)
            elif action == "check":
                await page.check(f"#{element_id}")
            elif action == "uncheck":
                await page.uncheck(f"#{element_id}")
            elif action == "hover":
                await page.hover(f"#{element_id}")
            elif action == "press":
                key = args.get("key")
                if not key:
                    return {"error": "Key required for 'press' action"}
                await page.press(f"#{element_id}", key)
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

    async def _handle_extract_page_state(self, args: dict, agent_id: str) -> dict:
        mode = args.get("mode", "full")

        browser = await self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            page = browser.page

            # Extract content as SDOM, then serialize to a JSON-safe dict
            sdom = self.serializer.to_sdom(await page.content(), page.url, 200)

            return {
                "mode": mode,
                "extracted": True,
                "sdom": sdom.model_dump(),
            }
        except Exception as e:
            logger.error("extract_page_state failed", extra={"mode": mode, "error": str(e)})
            return {"error": f"Failed to extract page state in {mode} mode: {str(e)}"}

    # ── fill_credentials (MP-3 part B) ──────────────────

    # Username-field heuristic, most specific first. The first visible
    # matching element before/around the password field gets the username.
    _USERNAME_SELECTORS = (
        "input[type='email']",
        "input[name*='user' i]",
        "input[id*='user' i]",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[name*='login' i]",
        "input[id*='login' i]",
        "input[type='text']",
        "input:not([type])",
    )
    _PASSWORD_SELECTOR = "input[type='password']"
    _SUBMIT_SELECTORS = (
        "button[type='submit']",
        "input[type='submit']",
        "button:not([type])",
    )

    async def _handle_fill_credentials(self, args: dict, agent_id: str) -> dict:
        """Origin-bound credential fill — returns a summary, never the secret.

        The credential is resolved from the server-side store by
        (origin, handle); the fill is refused unless the browser's CURRENT
        page origin matches the stored origin, so a handle saved for one
        site can never be replayed into another. The response carries only
        which fields were filled — never username or password values.
        """
        origin = args.get("origin", "")
        handle = args.get("handle", "")
        submit = bool(args.get("submit", False))
        timeout_ms = args.get("timeout_ms", 30000)

        if self.credential_store is None:
            return {"error": "credential store unavailable"}

        browser = await self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}
        page = browser.page

        # ── Origin binding: the CURRENT page must match the stored origin.
        want_origin = normalize_origin(origin)
        if not want_origin:
            return {"error": "origin must be a URL or scheme://host"}
        try:
            current_origin = normalize_origin(page.url)
        except Exception:
            current_origin = ""
        if current_origin != want_origin:
            return {
                "error": (
                    f"refusing to fill: current page origin {current_origin or 'unknown'} "
                    f"does not match credential origin {want_origin}"
                ),
                "origin_bound": True,
            }

        cred = self.credential_store.get(want_origin, handle)
        if cred is None:
            return {"error": f"no credential stored for origin {want_origin} handle {handle}"}

        # ── Fill. Username via heuristic (first visible candidate), then
        # the first visible password field. fill() types into the field;
        # values never leave this scope into the response.
        try:
            user_locator = None
            user_selector = None
            for selector in self._USERNAME_SELECTORS:
                locator = page.locator(selector).first
                if await locator.is_visible():
                    user_locator = locator
                    user_selector = selector
                    break

            pwd_locator = page.locator(self._PASSWORD_SELECTOR).first
            pwd_visible = await pwd_locator.is_visible()
            if not pwd_visible:
                return {
                    "error": "no visible password field on this page — navigate to the login form first"
                }
            if user_locator is None:
                # No recognizable username field — some login forms are
                # password-only (e.g. passphrase gates). Fill just the password.
                await pwd_locator.fill(cred.password)
                filled_fields = ["password"]
            else:
                await user_locator.fill(cred.username)
                await pwd_locator.fill(cred.password)
                filled_fields = ["username", "password"]

            submitted = False
            if submit:
                for selector in self._SUBMIT_SELECTORS:
                    locator = page.locator(selector).first
                    if await locator.is_visible():
                        await locator.click()
                        submitted = True
                        break

            # Summary only — the secret values are NEVER echoed.
            return {
                "filled": True,
                "origin": want_origin,
                "handle": handle,
                "fields_filled": filled_fields,
                "submitted": submitted,
            }
        except Exception as e:
            # Never include cred values in the error path.
            logger.error(
                "fill_credentials failed",
                extra={"origin": want_origin, "handle": handle, "error": str(e)},
            )
            return {"error": f"Failed to fill credential for {want_origin}: {str(e)}"}
