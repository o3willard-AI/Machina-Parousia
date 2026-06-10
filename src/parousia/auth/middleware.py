"""FastAPI middleware for agent API key authentication."""

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from parousia.auth.accounts import AccountStore


class AgentAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer tokens against the account store.

    Sets request.state.account and request.state.account_id on success.
    Skips public_paths (no auth needed).

    IMPORTANT: Returns JSONResponse for auth failures, does NOT raise
    HTTPException — BaseHTTPMiddleware + HTTPException breaks TestClient.
    """

    def __init__(self, app, account_store: AccountStore, public_paths: set | None = None):
        super().__init__(app)
        self.store = account_store
        self.public_paths = public_paths or {
            "/health", "/onboard", "/docs", "/openapi.json"
        }

    async def dispatch(self, request: Request, call_next):
        # Public paths skip auth — supports both exact match and prefix match
        path = request.url.path
        for public in self.public_paths:
            if path == public or path.startswith(public + "/") or path.startswith(public + "?"):
                return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        api_key = auth_header[7:]  # strip "Bearer "
        account = self.store.authenticate(api_key)
        if not account:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        if account.status != "active":
            return JSONResponse(
                status_code=403,
                content={"detail": f"Account is {account.status}"},
            )

        request.state.account = account
        request.state.account_id = account.account_id
        return await call_next(request)


def get_account(request: Request):
    """FastAPI dependency: extract the authenticated Account from request state.

    This is called inside route handlers (not middleware), so raise
    HTTPException is safe here — FastAPI's exception handling works correctly.
    """
    if not hasattr(request.state, "account"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.state.account
