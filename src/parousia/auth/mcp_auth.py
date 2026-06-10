"""MCP auth — extract account from MCP transport headers.

For SSE transport: Authorization header is passed through context variable.
For stdio transport: Auth is optional — falls back to config-based agent resolution.
"""

import contextvars

from parousia.auth.accounts import Account, AccountStore

# Context variable set by the SSE transport handler or launcher
# to inject the authenticated account into tool dispatch.
_auth_context: contextvars.ContextVar[Account | None] = contextvars.ContextVar(
    "parousia_auth", default=None
)


def authenticate_mcp(account_store: AccountStore, headers: dict) -> Account:
    """Validate Bearer token from MCP headers. Raises ValueError on failure."""
    auth = headers.get("authorization", "") or headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise ValueError("Missing or invalid Authorization header")
    api_key = auth[7:]
    account = account_store.authenticate(api_key)
    if not account:
        raise ValueError("Invalid API key")
    if account.status != "active":
        raise ValueError(f"Account is {account.status}")
    return account


def set_auth_context(account: Account | None) -> None:
    """Set the auth context for the current MCP request (called by SSE handler)."""
    _auth_context.set(account)


def get_auth_context() -> Account | None:
    """Get the authenticated account from context, if any."""
    try:
        return _auth_context.get()
    except LookupError:
        return None
