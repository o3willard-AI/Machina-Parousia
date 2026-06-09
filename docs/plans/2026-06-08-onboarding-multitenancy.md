# Onboarding & Multi-Tenancy — Implementation Plan

> **For Hermes:** Implement task-by-task with TDD. Each task commits independently.

**Goal:** Replace the static YAML agent registry with a self-service onboarding system that gives each agent a secure, sovereign account (account_id + API key), supports free and paid tiers, and enforces per-agent data isolation.

**Architecture:** SQLite-backed `accounts` table replaces `config.agents` for runtime lookups. Onboarding endpoint creates accounts and returns a one-time API key (bcrypt-hashed at rest). All MCP and REST endpoints require `Authorization: Bearer <api_key>` — scoped to the agent's account. Config-based agents remain as a fallback for local dev.

**Tech Stack:** Python 3.12+, SQLite, bcrypt, FastAPI middleware, MCP SSE transport with auth headers

---

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Account store | SQLite (`accounts` table) | Same pattern as temporal DB; no Redis dependency |
| Key hashing | bcrypt | Industry standard; never store plaintext |
| Key delivery | Shown once at onboarding | Like GitHub PATs — agent must save it |
| MCP auth | `Authorization: Bearer <key>` in SSE headers | MCP SSE transport supports custom headers |
| Free onboarding | Self-service, email-verified | Low friction |
| Paid onboarding | Admin-created, invite-code | Controlled access for paying customers |
| Data isolation | `account_id` column on all tables, app-level WHERE clause | Simple, auditable, no per-agent DB files |
| Backward compat | `config.agents` fallback when no auth header | Local dev doesn't break |

## Data Model

```sql
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,        -- agent-chosen name (e.g. "hermes-7a3f")
    display_name TEXT,                  -- human-friendly name
    api_key_hash TEXT NOT NULL,         -- bcrypt hash of the API key
    tier TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'paid'
    status TEXT NOT NULL DEFAULT 'active', -- 'active' | 'suspended'
    email TEXT,                         -- verification / recovery email
    email_verified INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    rate_limit_per_hour INTEGER DEFAULT 100,
    browser_max_instances INTEGER DEFAULT 1,
    storage_bytes_used INTEGER DEFAULT 0,
    metadata TEXT DEFAULT '{}'          -- JSON blob for extensibility
);

CREATE TABLE api_key_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    event_type TEXT NOT NULL,           -- 'created' | 'rotated' | 'revoked'
    key_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);
```

### Tier defaults

| Capability | Free | Paid |
|-----------|------|------|
| Emails / day | 10 | 100 |
| Browses / day | 50 | 500 |
| Chromium profiles | 1 | 5 |
| Storage | 10 MB | 100 MB |
| Rate limit / hour | 20 | 100 |
| Onboarding | Self-service | Invite-only |

---

## Endpoints

### New

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/onboard` | None | Create free account. Body: `{account_id, email?}` → `{account_id, api_key}` (key shown once) |
| `POST` | `/onboard/verify` | None | Verify email ownership. Body: `{account_id, code}` |
| `GET` | `/account` | Bearer | Get current account info (tier, limits, usage) |
| `POST` | `/account/rotate-key` | Bearer | Rotate API key → `{new_api_key}` (shown once) |
| `GET` | `/inbox` | Bearer | List agent's emails (paginated) |
| `GET` | `/inbox/{message_id}` | Bearer | Read specific email |
| `POST` | `/admin/accounts` | Admin | Create paid account. Body: `{account_id, tier, email?}` → `{account_id, api_key}` |
| `POST` | `/admin/accounts/{id}/suspend` | Admin | Suspend account |
| `POST` | `/admin/accounts/{id}/reactivate` | Admin | Reactivate account |

### Modified

| Method | Path | Change |
|--------|------|--------|
| `POST` | `/ingest` | Accept mail for any `agent_id` that has an account (not just config.agents). Fallback to config check. |
| All MCP tools | — | Require `Authorization` header; scope tool access to authenticated account_id |

### Removed

- `config.agents` static lookup for runtime auth (kept as dev fallback only)

---

## Files

| Action | File | Purpose |
|--------|------|---------|
| **Create** | `src/parousia/auth/__init__.py` | Auth module init |
| **Create** | `src/parousia/auth/accounts.py` | Account model, CRUD, key hashing, verification |
| **Create** | `src/parousia/auth/middleware.py` | FastAPI middleware that validates Bearer tokens |
| **Create** | `src/parousia/auth/mcp_auth.py` | MCP auth wrapper — extracts account from transport headers |
| **Create** | `src/parousia/auth/onboard.py` | Onboarding endpoint logic (free + paid flows) |
| **Create** | `src/parousia/guard/inbox.py` | Inbox REST endpoints (list, read) |
| **Create** | `src/parousia/guard/inbox_store.py` | SQLite-backed inbox storage per agent |
| **Modify** | `src/parousia/config.py` | Add `AccountStore`, `TierConfig`, `AdminConfig` |
| **Modify** | `src/parousia/guard/rest_server.py` | Add `/onboard`, `/account`, `/inbox`, admin endpoints; add auth middleware |
| **Modify** | `src/parousia/guard/mcp_server.py` | Require auth on all tool calls; scope by account_id |
| **Modify** | `src/parousia/guard/ingest.py` | Route by account_id from DB, not config.agents |
| **Create** | `tests/test_auth_accounts.py` | Account CRUD + key hashing tests |
| **Create** | `tests/test_auth_middleware.py` | Auth middleware tests |
| **Create** | `tests/test_onboard.py` | Onboarding flow tests |
| **Create** | `tests/test_inbox.py` | Inbox storage + REST tests |
| **Modify** | `tests/test_mcp_server.py` | Add auth-required tests |
| **Modify** | `tests/test_rest_server.py` | Add auth-required tests |
| **Modify** | `pyproject.toml` | Add `bcrypt>=4.0.0` dependency |

---

## Task Breakdown

### Story A: Account Infrastructure (auth/accounts.py)

#### Task A1: Create Account model and DB schema

**Objective:** Define the `Account` dataclass and SQLite schema for the `accounts` table.

**Files:**
- Create: `src/parousia/auth/__init__.py`
- Create: `src/parousia/auth/accounts.py`

**Step 1: Write the AccountStore class**

```python
"""Account store — SQLite-backed agent accounts with bcrypt key hashing."""

import sqlite3
import uuid
import bcrypt
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Account:
    account_id: str
    display_name: str = ""
    api_key_hash: str = ""
    tier: str = "free"
    status: str = "active"
    email: str = ""
    email_verified: bool = False
    created_at: str = ""
    last_seen_at: str = ""
    rate_limit_per_hour: int = 20
    browser_max_instances: int = 1
    storage_bytes_used: int = 0
    metadata: str = "{}"


DEFAULT_DB_PATH = "/var/lib/parousia/accounts.db"


class AccountStore:
    """Manages agent accounts in SQLite."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                api_key_hash TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'free',
                status TEXT NOT NULL DEFAULT 'active',
                email TEXT NOT NULL DEFAULT '',
                email_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL DEFAULT '',
                rate_limit_per_hour INTEGER NOT NULL DEFAULT 20,
                browser_max_instances INTEGER NOT NULL DEFAULT 1,
                storage_bytes_used INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS api_key_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
            CREATE INDEX IF NOT EXISTS idx_accounts_tier ON accounts(tier);
            CREATE INDEX IF NOT EXISTS idx_key_events_account ON api_key_events(account_id);
        """)
        self._conn.commit()

    # ── Key hashing ───────────────────────────────
    @staticmethod
    def hash_key(api_key: str) -> str:
        return bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_key(api_key: str, key_hash: str) -> bool:
        return bcrypt.checkpw(api_key.encode(), key_hash.encode())

    @staticmethod
    def generate_key() -> str:
        """Generate a random API key. Format: po_<32 hex chars>"""
        return f"po_{uuid.uuid4().hex}"

    # ── CRUD ─────────────────────────────────────

    def create_account(
        self,
        account_id: str,
        tier: str = "free",
        email: str = "",
        display_name: str = "",
    ) -> tuple[Account, str]:
        """Create an account and return (account, raw_api_key).
        The raw key is returned ONCE — it is not stored.
        """
        api_key = self.generate_key()
        key_hash = self.hash_key(api_key)
        now = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """INSERT INTO accounts (account_id, display_name, api_key_hash, tier,
               email, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (account_id, display_name, key_hash, tier, email, now),
        )
        self._conn.execute(
            "INSERT INTO api_key_events (account_id, event_type, key_hash, created_at) VALUES (?, 'created', ?, ?)",
            (account_id, key_hash, now),
        )
        self._conn.commit()

        account = self.get_account(account_id)
        return account, api_key

    def get_account(self, account_id: str) -> Optional[Account]:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if not row:
            return None
        return Account(
            account_id=row["account_id"],
            display_name=row["display_name"],
            api_key_hash=row["api_key_hash"],
            tier=row["tier"],
            status=row["status"],
            email=row["email"],
            email_verified=bool(row["email_verified"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            rate_limit_per_hour=row["rate_limit_per_hour"],
            browser_max_instances=row["browser_max_instances"],
            storage_bytes_used=row["storage_bytes_used"],
            metadata=row["metadata"],
        )

    def authenticate(self, api_key: str) -> Optional[Account]:
        """Look up an account by API key. Returns Account or None."""
        # This scans all accounts — acceptable at small scale.
        # For production, add an api_key_hash index.
        rows = self._conn.execute(
            "SELECT account_id, api_key_hash FROM accounts WHERE status = 'active'"
        ).fetchall()
        for row in rows:
            if self.verify_key(api_key, row["api_key_hash"]):
                return self.get_account(row["account_id"])
        return None

    def rotate_key(self, account_id: str) -> Optional[str]:
        """Generate a new API key, replacing the old one. Returns raw key (once)."""
        account = self.get_account(account_id)
        if not account:
            return None
        new_key = self.generate_key()
        new_hash = self.hash_key(new_key)
        now = datetime.now(timezone.utc).isoformat()
        old_hash = account.api_key_hash

        self._conn.execute(
            "UPDATE accounts SET api_key_hash = ? WHERE account_id = ?",
            (new_hash, account_id),
        )
        self._conn.execute(
            "INSERT INTO api_key_events (account_id, event_type, key_hash, created_at) VALUES (?, 'rotated', ?, ?)",
            (account_id, new_hash, now),
        )
        self._conn.execute(
            "INSERT INTO api_key_events (account_id, event_type, key_hash, created_at) VALUES (?, 'revoked', ?, ?)",
            (account_id, old_hash, now),
        )
        self._conn.commit()
        return new_key

    def set_status(self, account_id: str, status: str) -> bool:
        cur = self._conn.execute(
            "UPDATE accounts SET status = ? WHERE account_id = ?",
            (status, account_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def account_exists(self, account_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        return row is not None

    def close(self):
        if self._conn:
            self._conn.close()
```

**Step 2: Write tests**

In `tests/test_auth_accounts.py`:

```python
import pytest
from parousia.auth.accounts import AccountStore, Account


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test_accounts.db"
    s = AccountStore(str(db))
    s.connect()
    yield s
    s.close()


class TestKeyHashing:
    def test_hash_and_verify(self, store):
        key = "po_testkey123"
        h = store.hash_key(key)
        assert h != key
        assert store.verify_key(key, h)
        assert not store.verify_key("wrong_key", h)

    def test_generate_key_format(self, store):
        key = store.generate_key()
        assert key.startswith("po_")
        assert len(key) == 35  # "po_" + 32 hex


class TestAccountCRUD:
    def test_create_and_get(self, store):
        account, raw_key = store.create_account("agent-1")
        assert account.account_id == "agent-1"
        assert account.tier == "free"
        assert account.status == "active"
        assert raw_key.startswith("po_")

    def test_authenticate_success(self, store):
        _, raw_key = store.create_account("agent-auth")
        account = store.authenticate(raw_key)
        assert account is not None
        assert account.account_id == "agent-auth"

    def test_authenticate_wrong_key(self, store):
        store.create_account("agent-auth")
        account = store.authenticate("po_wrongkey")
        assert account is None

    def test_authenticate_suspended(self, store):
        _, raw_key = store.create_account("agent-sus")
        store.set_status("agent-sus", "suspended")
        account = store.authenticate(raw_key)
        assert account is None

    def test_rotate_key(self, store):
        _, old_key = store.create_account("agent-rot")
        new_key = store.rotate_key("agent-rot")
        assert new_key is not None
        assert new_key != old_key
        # Old key no longer works
        assert store.authenticate(old_key) is None
        # New key works
        assert store.authenticate(new_key) is not None

    def test_rotate_key_nonexistent(self, store):
        assert store.rotate_key("nobody") is None

    def test_account_exists(self, store):
        assert not store.account_exists("nobody")
        store.create_account("somebody")
        assert store.account_exists("somebody")

    def test_create_duplicate_fails(self, store):
        store.create_account("dupe")
        with pytest.raises(Exception):
            store.create_account("dupe")
```

**Step 3: Run tests — expect ALL PASS**

```bash
cd ~/workspace/Parousia
python3 -m pytest tests/test_auth_accounts.py -v
```

**Step 4: Commit**

```bash
git add src/parousia/auth/ tests/test_auth_accounts.py
git commit -m "feat(auth): AccountStore with bcrypt key hashing, CRUD, and key rotation"
```

---

#### Task A2: Auth middleware for FastAPI

**Objective:** Create middleware that validates `Authorization: Bearer <key>` on protected routes.

**Files:**
- Create: `src/parousia/auth/middleware.py`

```python
"""FastAPI middleware for agent API key authentication."""

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from parousia.auth.accounts import AccountStore


class AgentAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer tokens against the account store.
    
    Sets request.state.account on success.
    Skips paths listed in public_paths.
    """

    def __init__(self, app, account_store: AccountStore, public_paths: set = None):
        super().__init__(app)
        self.store = account_store
        self.public_paths = public_paths or {
            "/health", "/onboard", "/onboard/verify", "/docs", "/openapi.json"
        }

    async def dispatch(self, request: Request, call_next):
        # Skip auth for public paths
        if request.url.path in self.public_paths or request.url.path.startswith("/admin/"):
            # Admin paths use a different auth (API key whitelist) — skip here
            pass  # Still fall through for now; admin auth is added later

        if request.url.path in self.public_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

        api_key = auth_header[7:]  # strip "Bearer "
        account = self.store.authenticate(api_key)
        if not account:
            raise HTTPException(status_code=401, detail="Invalid API key")

        # Attach account to request state for downstream handlers
        request.state.account = account
        request.state.account_id = account.account_id
        return await call_next(request)


def get_account(request: Request):
    """Dependency: extract the authenticated account from request state."""
    if not hasattr(request.state, "account"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.state.account
```

**Tests** in `tests/test_auth_middleware.py`:

```python
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
    app.add_middleware(AgentAuthMiddleware, account_store=store)

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
        app, store = app_and_store
        client = TestClient(app)
        resp = client.get("/public")
        assert resp.status_code == 200

    def test_private_path_no_auth_returns_401(self, app_and_store):
        app, store = app_and_store
        client = TestClient(app)
        resp = client.get("/private")
        assert resp.status_code == 401

    def test_private_path_valid_key(self, app_and_store):
        app, store = app_and_store
        _, raw_key = store.create_account("test-agent")
        client = TestClient(app)
        resp = client.get("/private", headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200
        assert resp.json()["account_id"] == "test-agent"

    def test_private_path_invalid_key(self, app_and_store):
        app, store = app_and_store
        client = TestClient(app)
        resp = client.get("/private", headers={"Authorization": "Bearer badkey"})
        assert resp.status_code == 401

    def test_suspended_account_denied(self, app_and_store):
        app, store = app_and_store
        _, raw_key = store.create_account("suspended")
        store.set_status("suspended", "suspended")
        client = TestClient(app)
        resp = client.get("/private", headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 401
```

Run: `python3 -m pytest tests/test_auth_middleware.py -v`

Commit:
```bash
git add src/parousia/auth/middleware.py tests/test_auth_middleware.py
git commit -m "feat(auth): FastAPI middleware for Bearer token authentication"
```

---

### Story B: Onboarding (auth/onboard.py)

#### Task B1: Free onboarding endpoint

**Objective:** `POST /onboard` — agent claims an account_id, gets an API key back (once).

**Files:**
- Create: `src/parousia/auth/onboard.py`
- Modify: `src/parousia/guard/rest_server.py`

Add to `rest_server.py`:

```python
from parousia.auth.accounts import AccountStore
from parousia.auth.onboard import OnboardRequest, OnboardResponse, handle_onboard

# Initialize account store at startup
_account_store = AccountStore()

@app.on_event("startup")
async def startup():
    _account_store.connect()

@app.post("/onboard", response_model=OnboardResponse)
async def onboard(request: OnboardRequest):
    return handle_onboard(_account_store, request)
```

`onboard.py`:

```python
"""Onboarding logic for free and paid agent accounts."""

from pydantic import BaseModel, Field
from fastapi import HTTPException
from parousia.auth.accounts import AccountStore


class OnboardRequest(BaseModel):
    account_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    email: str = Field(default="")
    display_name: str = Field(default="")


class OnboardResponse(BaseModel):
    account_id: str
    api_key: str  # Shown once!
    tier: str
    message: str


def handle_onboard(store: AccountStore, request: OnboardRequest) -> OnboardResponse:
    """Create a free-tier account."""
    if store.account_exists(request.account_id):
        raise HTTPException(status_code=409, detail=f"Account '{request.account_id}' already exists")

    account, api_key = store.create_account(
        account_id=request.account_id,
        tier="free",
        email=request.email,
        display_name=request.display_name,
    )

    return OnboardResponse(
        account_id=account.account_id,
        api_key=api_key,
        tier=account.tier,
        message=(
            "Account created! Save your API key — it will not be shown again. "
            "Use it in the Authorization header: 'Bearer <your_key>'"
        ),
    )
```

**Tests** in `tests/test_onboard.py`:
- `test_onboard_free_creates_account` — 200, returns account_id + api_key
- `test_onboard_duplicate_rejected` — 409
- `test_onboard_invalid_name_rejected` — 422
- `test_onboard_key_works_for_auth` — use returned key to hit /account

Run: `python3 -m pytest tests/test_onboard.py -v`

Commit:
```bash
git add src/parousia/auth/onboard.py src/parousia/guard/rest_server.py tests/test_onboard.py
git commit -m "feat(auth): free-tier self-service onboarding endpoint"
```

---

#### Task B2: Account info endpoint

**Objective:** `GET /account` returns the authenticated agent's account details.

Add to `rest_server.py`:

```python
from parousia.auth.middleware import get_account

@app.get("/account")
async def account_info(request: Request):
    account = get_account(request)
    return {
        "account_id": account.account_id,
        "display_name": account.display_name,
        "tier": account.tier,
        "status": account.status,
        "email": account.email,
        "email_verified": account.email_verified,
        "rate_limit_per_hour": account.rate_limit_per_hour,
        "browser_max_instances": account.browser_max_instances,
        "created_at": account.created_at,
    }
```

Test: `test_account_info_authenticated` / `test_account_info_unauthenticated`

Commit:
```bash
git add src/parousia/guard/rest_server.py
git commit -m "feat(auth): GET /account endpoint for authenticated agents"
```

---

#### Task B3: Key rotation endpoint

**Objective:** `POST /account/rotate-key` — agent can rotate their own API key.

```python
@app.post("/account/rotate-key")
async def rotate_key(request: Request):
    account = get_account(request)
    new_key = _account_store.rotate_key(account.account_id)
    if not new_key:
        raise HTTPException(status_code=500, detail="Key rotation failed")
    return {
        "account_id": account.account_id,
        "new_api_key": new_key,
        "message": "Key rotated! Save this new key — it will not be shown again."
    }
```

Tests:
- `test_rotate_key_success` — old key stops working, new key works
- `test_rotate_key_requires_auth` — 401 without token

Commit:
```bash
git add src/parousia/guard/rest_server.py
git commit -m "feat(auth): POST /account/rotate-key endpoint"
```

---

### Story C: Inbox Storage

#### Task C1: Inbox store (SQLite)

**Objective:** Replace the dead `/webhook` forward with SQLite-backed inbox storage.

**Files:**
- Create: `src/parousia/guard/inbox_store.py`

```python
"""Per-agent inbox storage in SQLite."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class InboxMessage:
    message_id: str
    account_id: str
    sender: str
    recipient: str
    subject: str
    body: str
    raw_mime: str
    received_at: str
    read: bool = False


DEFAULT_INBOX_PATH = "/var/lib/parousia/inbox.db"


class InboxStore:
    def __init__(self, db_path: str = DEFAULT_INBOX_PATH):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS inbox (
                message_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                sender TEXT NOT NULL DEFAULT '',
                recipient TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                raw_mime TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL,
                read INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_inbox_account ON inbox(account_id, received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_inbox_unread ON inbox(account_id, read) WHERE read = 0;
        """)
        self._conn.commit()

    def store(self, account_id: str, sender: str, recipient: str,
              subject: str, body: str, raw_mime: str, message_id: str = None) -> str:
        import uuid
        mid = message_id or str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO inbox (message_id, account_id, sender, recipient,
               subject, body, raw_mime, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, account_id, sender, recipient, subject, body, raw_mime, now),
        )
        self._conn.commit()
        return mid

    def list_messages(self, account_id: str, limit: int = 50, unread_only: bool = False) -> list[InboxMessage]:
        query = "SELECT * FROM inbox WHERE account_id = ?"
        if unread_only:
            query += " AND read = 0"
        query += " ORDER BY received_at DESC LIMIT ?"
        rows = self._conn.execute(query, (account_id, limit)).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def get_message(self, account_id: str, message_id: str) -> Optional[InboxMessage]:
        row = self._conn.execute(
            "SELECT * FROM inbox WHERE account_id = ? AND message_id = ?",
            (account_id, message_id),
        ).fetchone()
        return self._row_to_msg(row) if row else None

    def mark_read(self, account_id: str, message_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE inbox SET read = 1 WHERE account_id = ? AND message_id = ? AND read = 0",
            (account_id, message_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def unread_count(self, account_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE account_id = ? AND read = 0",
            (account_id,),
        ).fetchone()
        return row[0] if row else 0

    def _row_to_msg(self, row) -> InboxMessage:
        return InboxMessage(
            message_id=row["message_id"],
            account_id=row["account_id"],
            sender=row["sender"],
            recipient=row["recipient"],
            subject=row["subject"],
            body=row["body"],
            raw_mime=row["raw_mime"],
            received_at=row["received_at"],
            read=bool(row["read"]),
        )

    def close(self):
        if self._conn:
            self._conn.close()
```

Tests in `tests/test_inbox.py`:
- `test_store_and_list` — store 3 messages, list them
- `test_unread_count` — 2 stored, 0 read, count = 2; mark one read, count = 1
- `test_get_message` — retrieve specific message
- `test_isolation` — agent A can't see agent B's messages
- `test_mark_read` — marks message as read

Commit:
```bash
git add src/parousia/guard/inbox_store.py tests/test_inbox.py
git commit -m "feat(inbox): SQLite-backed per-agent inbox storage"
```

---

#### Task C2: Wire inbox into the ingest pipeline

**Objective:** Replace the dead `_forward_to_agent()` webhook POST with inbox storage.

Modify `rest_server.py` — in the `ingest` endpoint:

```python
# AFTER the agent_lookup and BEFORE returning:
# Store in inbox instead of fire-and-forget to webhook
from parousia.guard.inbox_store import InboxStore

_inbox_store = InboxStore()

@app.on_event("startup")
async def startup_inbox():
    _inbox_store.connect()

# In the ingest function:
_inbox_store.store(
    account_id=request.agent_id,
    sender=request.sender,
    recipient=request.recipient,
    subject=request.subject,
    body=request.body,
    raw_mime=request.raw_mime,
)
```

Test: Send email via SMTP → check inbox via `list_messages(agent_id)`.

Commit:
```bash
git add src/parousia/guard/rest_server.py
git commit -m "feat(inbox): wire inbox store into ingest pipeline, replacing dead webhook"
```

---

#### Task C3: Inbox REST endpoints

**Objective:** `GET /inbox` and `GET /inbox/{message_id}` for authenticated agents.

```python
@app.get("/inbox")
async def list_inbox(request: Request, unread_only: bool = False, limit: int = 50):
    account = get_account(request)
    messages = _inbox_store.list_messages(account.account_id, limit=limit, unread_only=unread_only)
    return {
        "messages": [
            {
                "message_id": m.message_id,
                "sender": m.sender,
                "subject": m.subject,
                "received_at": m.received_at,
                "read": m.read,
            }
            for m in messages
        ],
        "unread_count": _inbox_store.unread_count(account.account_id),
    }


@app.get("/inbox/{message_id}")
async def read_inbox_message(request: Request, message_id: str):
    account = get_account(request)
    msg = _inbox_store.get_message(account.account_id, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    _inbox_store.mark_read(account.account_id, message_id)
    return {
        "message_id": msg.message_id,
        "sender": msg.sender,
        "recipient": msg.recipient,
        "subject": msg.subject,
        "body": msg.body,
        "received_at": msg.received_at,
        "read": msg.read,
    }
```

Tests:
- `test_list_inbox_authenticated` — returns messages
- `test_list_inbox_unauthenticated` — 401
- `test_list_inbox_isolated` — agent A can't see agent B's mail
- `test_read_message_marks_read` — unread_count decrements

Commit:
```bash
git add src/parousia/guard/rest_server.py tests/test_inbox.py
git commit -m "feat(inbox): GET /inbox and GET /inbox/{id} REST endpoints"
```

---

### Story D: MCP Auth

#### Task D1: Auth-wrapped MCP tools

**Objective:** All MCP tool calls require a valid API key. Tool results are scoped to the authenticated agent.

**Files:**
- Modify: `src/parousia/guard/mcp_server.py`
- Create: `src/parousia/auth/mcp_auth.py`

`mcp_auth.py`:

```python
"""MCP auth — extract account from MCP transport headers."""

from parousia.auth.accounts import AccountStore, Account


def authenticate_mcp(account_store: AccountStore, headers: dict) -> Account:
    """Validate Bearer token from MCP headers. Raises ValueError on failure."""
    auth = headers.get("authorization", "") or headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise ValueError("Missing or invalid Authorization header")
    api_key = auth[7:]
    account = account_store.authenticate(api_key)
    if not account:
        raise ValueError("Invalid API key")
    return account
```

Modify `mcp_server.py` — add `account_store` to `_build_server()` and auth check in `handle_call_tool`:

```python
# In _build_server():
account_store = AccountStore()
account_store.connect()

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # Auth: extract account from transport context
    try:
        # MCP SSE transport attaches request headers to the session
        # For stdio transport, auth is not enforced (local dev)
        account = None
        # In SSE mode, the server.run() passes initialization options
        # We extract from a thread-local or context var set by the SSE handler
        if hasattr(_auth_context, "account"):
            account = _auth_context.account
    except Exception:
        return [TextContent(type="text", text=json.dumps({"error": "authentication required"}))]

    if account:
        agent_id = account.account_id
    else:
        agent_id = _resolve_agent_id(config, arguments)

    # ... rest of tool dispatch
```

> **Note:** MCP SSE transport auth requires the SSE handler in `launcher.py` to extract the `Authorization` header from the HTTP request and inject it into a context variable before calling `server.run()`. This is a two-part change: (1) the SSE handler passes headers, (2) the tool dispatcher reads them.

Commit:
```bash
git add src/parousia/auth/mcp_auth.py src/parousia/guard/mcp_server.py
git commit -m "feat(auth): MCP tools require API key authentication"
```

---

### Story E: Admin Endpoints

#### Task E1: Admin account creation (paid tier)

**Objective:** `POST /admin/accounts` — admin creates paid accounts. Protected by an admin API key in config.

```python
ADMIN_API_KEY = os.environ.get("PAROUSIA_ADMIN_KEY", "")

def require_admin(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Admin access required")

@app.post("/admin/accounts")
async def admin_create_account(request: Request):
    require_admin(request)
    body = await request.json()
    account, api_key = _account_store.create_account(
        account_id=body["account_id"],
        tier=body.get("tier", "paid"),
        email=body.get("email", ""),
        display_name=body.get("display_name", ""),
    )
    return {"account_id": account.account_id, "api_key": api_key, "tier": account.tier}


@app.post("/admin/accounts/{account_id}/suspend")
async def admin_suspend(account_id: str, request: Request):
    require_admin(request)
    ok = _account_store.set_status(account_id, "suspended")
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"account_id": account_id, "status": "suspended"}


@app.post("/admin/accounts/{account_id}/reactivate")
async def admin_reactivate(account_id: str, request: Request):
    require_admin(request)
    ok = _account_store.set_status(account_id, "active")
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"account_id": account_id, "status": "active"}
```

Tests:
- `test_admin_create_paid_account` — 200, returns api_key
- `test_admin_create_without_key` — 403
- `test_admin_suspend_reactivate` — suspend blocks auth, reactivate restores

Commit:
```bash
git add src/parousia/guard/rest_server.py
git commit -m "feat(admin): paid account creation, suspend, reactivate endpoints"
```

---

### Story F: Integration & Polish

#### Task F1: Wire `_forward_to_agent` → inbox + remove dead webhook

Remove the `_forward_to_agent()` function call from ingest and use inbox_store directly. Remove the `webhook_url` from AgentConfig as it's no longer needed.

#### Task F2: Add `check_inbox` MCP tool

```python
Tool(
    name="check_inbox",
    description="Check your Parousia inbox for new emails.",
    inputSchema={
        "type": "object",
        "properties": {
            "unread_only": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "default": 10},
        },
    },
)
```

Handler queries `InboxStore.list_messages(account_id, unread_only=..., limit=...)` and returns formatted results.

#### Task F3: Update `config.yaml` model

Add to `ParousiaConfig`:

```python
class AccountStoreConfig(BaseModel):
    db_path: str = "/var/lib/parousia/accounts.db"

class AdminConfig(BaseModel):
    api_key: str = ""  # Set via PAROUSIA_ADMIN_KEY env var

# In ParousiaConfig:
account_store: AccountStoreConfig = Field(default_factory=AccountStoreConfig)
admin: AdminConfig = Field(default_factory=AdminConfig)
```

#### Task F4: Full regression

```bash
python3 -m pytest tests/ -v --tb=short
```

Target: all existing 127 tests + new auth/inbox tests pass.

---

## Execution Order

```
A1: AccountStore + key hashing ──┐
A2: Auth middleware ─────────────┤
                                  ├── Foundation
B1: Free onboarding ─────────────┤
B2: Account info endpoint ───────┤
B3: Key rotation ────────────────┘
                                  │
C1: Inbox store ─────────────────┐
C2: Wire inbox into ingest ──────┤── Core feature
C3: Inbox REST endpoints ────────┘
                                  │
D1: MCP auth wrapper ────────────┘── MCP integration
                                  │
E1: Admin endpoints ─────────────┘── Paid tier
                                  │
F1-F4: Integration, polish, regr  ┘── Cleanup
```

---

## Config Additions

```yaml
# /etc/parousia/config.yaml additions

account_store:
  db_path: /var/lib/parousia/accounts.db

admin:
  # Set PAROUSIA_ADMIN_KEY env var, not in file
  # api_key: ""  
```

---

## Dependencies

Add to `pyproject.toml`:
```toml
dependencies = [
    # ... existing
    "bcrypt>=4.0.0",
]
```

---

## Acceptance Criteria

1. Agent visits `POST /onboard` with `{account_id: "hermes-42"}` → gets back `{account_id, api_key}`
2. Agent sends email to `hermes-42@machinaparousia.ai` → lands in inbox
3. Agent calls `GET /inbox` with `Authorization: Bearer <key>` → sees their email
4. Agent calls MCP `check_inbox` → sees their email
5. Agent B cannot read Agent A's inbox (403 or empty results)
6. Admin creates paid account → higher limits
7. Admin suspends account → auth fails
8. All 127 existing tests still pass
