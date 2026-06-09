"""Tests for onboarding endpoint and account management."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from parousia.auth.accounts import AccountStore
from parousia.auth.middleware import AgentAuthMiddleware, get_account
from parousia.auth.onboard import OnboardRequest, OnboardResponse, handle_onboard


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "test_onboard.db"
    store = AccountStore(str(db))
    store.connect()
    app = FastAPI()
    app.add_middleware(
        AgentAuthMiddleware,
        account_store=store,
        public_paths={"/health", "/onboard", "/docs", "/openapi.json"},
    )

    @app.post("/onboard", response_model=OnboardResponse)
    async def onboard(request: OnboardRequest):
        return handle_onboard(store, request)

    @app.get("/account")
    async def account_info(request: Request):
        account = get_account(request)
        return {"account_id": account.account_id, "tier": account.tier}

    @app.post("/account/rotate-key")
    async def rotate_key(request: Request):
        account = get_account(request)
        new_key = store.rotate_key(account.account_id)
        if not new_key:
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail="Key rotation failed")
        return {"new_api_key": new_key}

    yield TestClient(app)
    store.close()


class TestOnboard:
    def test_onboard_free_creates_account(self, client):
        resp = client.post("/onboard", json={"account_id": "agent-b1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["account_id"] == "agent-b1"
        assert data["tier"] == "free"
        assert data["api_key"].startswith("po_")

    def test_onboard_duplicate_rejected(self, client):
        client.post("/onboard", json={"account_id": "dupe"})
        resp = client.post("/onboard", json={"account_id": "dupe"})
        assert resp.status_code == 409

    def test_onboard_invalid_name_rejected(self, client):
        resp = client.post("/onboard", json={"account_id": "INVALID"})
        assert resp.status_code == 422

    def test_onboarded_key_works_for_auth(self, client):
        resp = client.post("/onboard", json={"account_id": "auth-test"})
        key = resp.json()["api_key"]
        resp2 = client.get("/account", headers={"Authorization": f"Bearer {key}"})
        assert resp2.status_code == 200
        assert resp2.json()["account_id"] == "auth-test"


class TestAccount:
    def test_account_info_requires_auth(self, client):
        resp = client.get("/account")
        assert resp.status_code == 401

    def test_account_info_with_valid_key(self, client):
        r = client.post("/onboard", json={"account_id": "info-test"})
        key = r.json()["api_key"]
        resp = client.get("/account", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200
        assert resp.json()["tier"] == "free"

    def test_rotate_key_works(self, client):
        r = client.post("/onboard", json={"account_id": "rot-test"})
        old_key = r.json()["api_key"]
        resp = client.post("/account/rotate-key", headers={"Authorization": f"Bearer {old_key}"})
        assert resp.status_code == 200
        new_key = resp.json()["new_api_key"]
        assert new_key != old_key
        # Old key no longer works
        assert client.get("/account", headers={"Authorization": f"Bearer {old_key}"}).status_code == 401
        # New key works
        assert client.get("/account", headers={"Authorization": f"Bearer {new_key}"}).status_code == 200

    def test_rotate_key_requires_auth(self, client):
        resp = client.post("/account/rotate-key")
        assert resp.status_code == 401
