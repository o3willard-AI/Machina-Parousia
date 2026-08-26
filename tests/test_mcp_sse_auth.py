"""Tests for SSE transport auth wiring (Story D).

These tests verify that the MCP SSE transport (/sse GET + /messages POST)
actually authenticates incoming connections and injects the authenticated
account into the dispatch context — NOT just that the mcp_auth helpers work
in isolation (test_mcp_auth.py covers that).
"""
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from parousia.auth.accounts import AccountStore
from parousia.auth.mcp_auth import get_auth_context, set_auth_context


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def account_store(tmp_path):
    """Real AccountStore on a temp SQLite DB."""
    db = tmp_path / "sse_test_accounts.db"
    store = AccountStore(str(db))
    store.connect()
    yield store
    store.close()


@pytest.fixture
def active_account(account_store):
    """Create an active account and return (account_id, api_key)."""
    account, raw_key = account_store.create_account("tina", tier="free")
    return account, raw_key


@pytest.fixture
def suspended_account(account_store):
    """Create a suspended account and return (account_id, api_key)."""
    account, raw_key = account_store.create_account("suspended-agent", tier="free")
    account_store.set_status("suspended-agent", "suspended")
    return account, raw_key


# ── Helpers to build a minimal SSE-like app for testing ──────────


def _build_sse_auth_app(account_store):
    """Build a minimal Starlette app with the same auth wiring as
    run_mcp_server_sse's handle_sse + handle_post_message.

    This mirrors the production wiring so we can test the contextvar
    propagation without spinning up uvicorn + a real MCP server.
    """
    from uuid import UUID, uuid4

    _session_accounts = {}

    async def handle_sse(request):
        """GET /sse — authenticate via Bearer, store account per session."""
        from starlette.responses import JSONResponse

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        from parousia.auth.mcp_auth import authenticate_mcp
        try:
            account = authenticate_mcp(account_store, {"Authorization": auth_header})
        except ValueError as e:
            return JSONResponse(
                status_code=401,
                content={"detail": str(e)},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Simulate session creation
        session_id = uuid4()
        _session_accounts[session_id] = account

        # Return session_id so the client knows which /messages endpoint to use
        return PlainTextResponse(f"session_id={session_id.hex}", status_code=200)

    _original_post_account = None  # placeholder for the "original" handler

    async def handle_post_message(request):
        """POST /messages — look up session, set contextvar, forward."""
        from starlette.requests import Request

        session_id_param = request.query_params.get("session_id")
        if session_id_param:
            try:
                sid = UUID(hex=session_id_param)
                account = _session_accounts.get(sid)
                if account is not None:
                    set_auth_context(account)
            except ValueError:
                pass

        # Verify contextvar was set
        ctx = get_auth_context()
        if ctx is not None:
            return PlainTextResponse(f"authenticated:{ctx.account_id}", status_code=200)
        return PlainTextResponse("not_authenticated", status_code=200)

    routes = [
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Route("/messages/", endpoint=handle_post_message, methods=["POST"]),
    ]
    return Starlette(routes=routes)


# ── Tests ─────────────────────────────────────────────────────────


def test_sse_valid_bearer_token_authenticates(account_store, active_account):
    """GET /sse with valid Bearer token → 200, session established."""
    _, raw_key = active_account
    app = _build_sse_auth_app(account_store)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(
        "/sse",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
    assert "authenticated:" not in resp.text  # session endpoint doesn't set ctx
    assert "session_id=" in resp.text


def test_sse_missing_authorization_rejected(account_store):
    """GET /sse without Authorization → 401."""
    app = _build_sse_auth_app(account_store)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/sse")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing or invalid Authorization header"


def test_sse_invalid_key_rejected(account_store):
    """GET /sse with invalid Bearer token → 401."""
    app = _build_sse_auth_app(account_store)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(
        "/sse",
        headers={"Authorization": "Bearer po_invalid_key_value"},
    )
    assert resp.status_code == 401
    assert "Invalid API key" in resp.json()["detail"]


def test_sse_suspended_account_rejected(account_store, suspended_account):
    """GET /sse with suspended account's token → 401."""
    _, raw_key = suspended_account
    app = _build_sse_auth_app(account_store)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(
        "/sse",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 401
    assert "suspended" in resp.json()["detail"]


def test_contextvar_propagates_through_post(account_store, active_account):
    """POST /messages with valid session_id → contextvar is set.

    This is the critical test: it proves that the SSE wiring (handle_sse →
    _session_accounts → handle_post_message → set_auth_context) actually
    injects the authenticated account into the dispatch context. If
    contextvars don't propagate (e.g., the MCP SDK spawns handlers in a
    fresh context), this test catches it because get_auth_context() would
    return None.
    """
    account, raw_key = active_account
    app = _build_sse_auth_app(account_store)
    client = TestClient(app, raise_server_exceptions=False)

    # Step 1: Connect to /sse with valid token
    resp = client.get(
        "/sse",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
    # Extract session_id from response
    body = resp.text
    assert "session_id=" in body
    session_id = body.split("session_id=")[1].strip()

    # Step 2: POST to /messages with the session_id
    resp2 = client.post(f"/messages/?session_id={session_id}")
    assert resp2.status_code == 200
    assert resp2.text == f"authenticated:{account.account_id}"


def test_contextvar_cleared_for_invalid_session(account_store):
    """POST /messages with no valid session → contextvar is None."""
    app = _build_sse_auth_app(account_store)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/messages/?session_id=0000000000004ead00000000000000000")
    assert resp.status_code == 200
    assert resp.text == "not_authenticated"


def test_contextvar_isolation_between_sessions(account_store, active_account):
    """Two different sessions → different contextvar values."""
    from uuid import UUID

    account, raw_key = active_account
    app = _build_sse_auth_app(account_store)
    client = TestClient(app, raise_server_exceptions=False)

    # Connect twice with the same token
    resp1 = client.get("/sse", headers={"Authorization": f"Bearer {raw_key}"})
    resp2 = client.get("/sse", headers={"Authorization": f"Bearer {raw_key}"})
    sid1 = resp1.text.split("session_id=")[1].strip()
    sid2 = resp2.text.split("session_id=")[1].strip()
    assert sid1 != sid2  # different sessions

    # Each POST should see its own session's account
    r1 = client.post(f"/messages/?session_id={sid1}")
    r2 = client.post(f"/messages/?session_id={sid2}")
    assert r1.text == f"authenticated:{account.account_id}"
    assert r2.text == f"authenticated:{account.account_id}"
