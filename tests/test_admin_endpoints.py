"""Tests for admin endpoints (Story E)."""

import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from parousia.auth.accounts import AccountStore
from parousia.guard.rest_server import app


@pytest.fixture
def admin_store():
    """AccountStore on a temp DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    store = AccountStore(db_path)
    store.connect()
    yield store
    store.close()
    os.unlink(db_path)


@pytest.fixture
def client_with_admin(admin_store):
    """TestClient with admin key set and account store patched."""
    with patch("parousia.guard.rest_server._account_store", admin_store), \
         patch("parousia.guard.rest_server._ADMIN_API_KEY", "admin-secret-key"):
        yield TestClient(app)


def test_admin_create_paid_account(client_with_admin):
    """Admin can create a paid tier account."""
    resp = client_with_admin.post(
        "/admin/accounts",
        json={"account_id": "paid-agent-1", "tier": "paid", "display_name": "Paid Agent"},
        headers={"Authorization": "Bearer admin-secret-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == "paid-agent-1"
    assert data["tier"] == "paid"
    assert data["api_key"].startswith("po_")
    assert len(data["api_key"]) == 35  # "po_" + 32 hex chars (UUID4 hex)


def test_admin_create_with_user_tier(client_with_admin):
    """Admin can specify tier. Defaults to paid if omitted."""
    resp = client_with_admin.post(
        "/admin/accounts",
        json={"account_id": "free-agent-admin", "tier": "free"},
        headers={"Authorization": "Bearer admin-secret-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["tier"] == "free"


def test_admin_create_without_key_returns_403(client_with_admin):
    """Missing admin key returns 403."""
    resp = client_with_admin.post(
        "/admin/accounts",
        json={"account_id": "no-key"},
    )
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["detail"]


def test_admin_create_wrong_key_returns_403(client_with_admin):
    """Wrong admin key returns 403."""
    resp = client_with_admin.post(
        "/admin/accounts",
        json={"account_id": "wrong-key"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 403


def test_admin_create_no_admin_key_configured():
    """When PAROUSIA_ADMIN_KEY is empty, returns 501."""
    store = AccountStore(":memory:")
    store.connect()
    with patch("parousia.guard.rest_server._account_store", store), \
         patch("parousia.guard.rest_server._ADMIN_API_KEY", ""):
        client = TestClient(app)
        resp = client.post(
            "/admin/accounts",
            json={"account_id": "test"},
            headers={"Authorization": "Bearer anything"},
        )
        assert resp.status_code == 501
        assert "not configured" in resp.json()["detail"]


def test_admin_suspend_reactivate(client_with_admin, admin_store):
    """Admin can suspend and reactivate an account."""
    # Create a free account
    resp = client_with_admin.post(
        "/admin/accounts",
        json={"account_id": "suspend-test", "tier": "free"},
        headers={"Authorization": "Bearer admin-secret-key"},
    )
    assert resp.status_code == 200
    api_key = resp.json()["api_key"]

    # Verify the account can authenticate (active)
    account = admin_store.authenticate(api_key)
    assert account is not None
    assert account.status == "active"

    # Suspend
    resp = client_with_admin.post(
        "/admin/accounts/suspend-test/suspend",
        headers={"Authorization": "Bearer admin-secret-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"

    # Verify suspended
    account = admin_store.authenticate(api_key)
    assert account.status == "suspended"

    # Reactivate
    resp = client_with_admin.post(
        "/admin/accounts/suspend-test/reactivate",
        headers={"Authorization": "Bearer admin-secret-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    # Verify active again
    account = admin_store.authenticate(api_key)
    assert account.status == "active"


def test_admin_suspend_nonexistent(client_with_admin):
    """Suspend nonexistent account returns 404."""
    resp = client_with_admin.post(
        "/admin/accounts/nope/suspend",
        headers={"Authorization": "Bearer admin-secret-key"},
    )
    assert resp.status_code == 404


def test_admin_reactivate_nonexistent(client_with_admin):
    """Reactivate nonexistent account returns 404."""
    resp = client_with_admin.post(
        "/admin/accounts/nope/reactivate",
        headers={"Authorization": "Bearer admin-secret-key"},
    )
    assert resp.status_code == 404


def test_admin_suspend_without_auth(client_with_admin):
    """Admin endpoints reject requests without auth."""
    resp = client_with_admin.post("/admin/accounts/any/suspend")
    assert resp.status_code == 403
