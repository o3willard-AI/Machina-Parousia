import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from parousia.auth.accounts import AccountStore
from parousia.auth.middleware import AgentAuthMiddleware, get_account


@pytest.fixture
def app_and_store(tmp_path):
    db = tmp_path / "test_mw.db"
    store = AccountStore(str(db))
    store.connect()

    app = FastAPI()
    app.add_middleware(
        AgentAuthMiddleware,
        account_store=store,
        public_paths={"/health", "/onboard", "/docs", "/openapi.json", "/public"},
    )

    @app.get("/public")
    async def public():
        return {"ok": True}

    @app.get("/private")
    async def private(request: Request):
        account = get_account(request)
        return {"account_id": account.account_id}

    yield app, store
    store.close()


class TestAuthMiddleware:
    def test_public_path_no_auth(self, app_and_store):
        """Public paths are accessible without auth."""
        app, _ = app_and_store
        client = TestClient(app)
        resp = client.get("/public")
        assert resp.status_code == 200

    def test_private_path_no_auth_returns_401(self, app_and_store):
        """Private paths without auth return 401."""
        app, _ = app_and_store
        client = TestClient(app)
        resp = client.get("/private")
        assert resp.status_code == 401

    def test_private_path_valid_key(self, app_and_store):
        """Valid Bearer token allows access and sets account_id."""
        app, store = app_and_store
        _, raw_key = store.create_account("test-agent")
        client = TestClient(app)
        resp = client.get(
            "/private", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert resp.status_code == 200
        assert resp.json()["account_id"] == "test-agent"

    def test_private_path_invalid_key(self, app_and_store):
        """Invalid Bearer token returns 401."""
        app, _ = app_and_store
        client = TestClient(app)
        resp = client.get(
            "/private", headers={"Authorization": "Bearer bad_key"}
        )
        assert resp.status_code == 401

    def test_suspended_account_denied(self, app_and_store):
        """Suspended accounts are denied even with a valid key."""
        app, store = app_and_store
        _, raw_key = store.create_account("suspended")
        store.set_status("suspended", "suspended")
        client = TestClient(app)
        resp = client.get(
            "/private", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert resp.status_code == 403
        assert "suspended" in resp.json()["detail"]
