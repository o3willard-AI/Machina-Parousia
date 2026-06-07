# Parousia Phase 1 Backlog — Implementation Plan

> **Goal:** Complete items 2–7 from the Phase 1 carry-forward backlog: postfwd Tier 2 rate
> limiting, human-in-the-loop batch approval, multi-agent routing, DKIM inbound validation,
> TLS certificates, and monitoring dashboard.

**Architecture:** Six independent server-side additions to the existing Parousia codebase.
Items 3, 4, 5, and 7 are Python code additions to the guard/temporal modules. Items 2 and 6
are infrastructure config files and CLI setup commands. All follow the existing conventions:
same package, same config file, same MCP/REST servers.

**Tech Stack:** Python 3.12+, FastAPI, Redis, Postfix (postfwd, certbot), Jinja2 (dashboard HTML)

**Ordering rationale:** Task order is semi-independent, but Item 4 (multi-agent) is a
prerequisite for Items 3 and 5 to work correctly. Execute sequentially for safety.

---

## Task 1: Multi-Agent Routing (Item 4)

**Objective:** Fix the MCP `send_email` tool to resolve `agent_id` from the `To` header
instead of hardcoding the first config agent. Also add a `from_agent` parameter so callers
can specify which agent is sending.

**Files:**
- Modify: `src/parousia/guard/mcp_server.py` — `_handle_send_email`, `_resolve_agent_id`
- Modify: `tests/test_mcp_server.py` — multi-agent routing tests
- Modify: `src/parousia/config.py` — no changes needed (already supports `dict[str, AgentConfig]`)

**Step 1: Add `from_agent` param to send_email schema**

In `mcp_server.py`, update the `send_email` Tool schema to include an optional `from_agent` field:

```python
Tool(
    name="send_email",
    description="Send an email through the Parousia agent mail system. Rate-limited: 100/hr per agent, 500/day domain-wide.",
    inputSchema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string", "description": "Email subject line"},
            "body": {"type": "string", "description": "Plain-text email body"},
            "reply_to": {"type": "string", "description": "Optional Reply-To address"},
            "from_agent": {"type": "string", "description": "Optional agent ID to send from (defaults to first configured agent)"},
        },
        "required": ["to", "subject", "body"],
    },
)
```

**Step 2: Update `_handle_send_email` to use `from_agent`**

Replace the hardcoded `agent_ids[0]` resolution:

```python
agent_id = arguments.get("from_agent")
if not agent_id:
    agent_ids = list(config.agents.keys())
    agent_id = agent_ids[0] if agent_ids else "default"

# Validate agent exists
if agent_id not in config.agents and agent_id != "default":
    return [TextContent(
        type="text",
        text=json.dumps({
            "sent": False,
            "error": f"Unknown agent: {agent_id}",
            "available_agents": list(config.agents.keys()),
        }),
    )]
```

**Step 3: Add test for multi-agent routing**

In `tests/test_mcp_server.py`, add:

```python
def test_send_email_with_from_agent_param():
    """send_email should use from_agent when provided."""
    # ... test that passing from_agent="hermes" uses that agent's config
    # ... test that unknown agent returns error with available_agents list

def test_send_email_defaults_to_first_agent():
    """send_email without from_agent should default to first configured agent."""
    # ... test the default fallback
```

**Step 4: Run tests**

```bash
pytest tests/test_mcp_server.py -v
```

**Verification:** Multi-agent routing works — any configured agent can send, unknown agents get a helpful error.

---

## Task 2: DKIM Inbound Validation (Item 5)

**Objective:** Verify DKIM signatures on incoming email in the ingest pipeline. The
`IngestRequest` model already has `dkim_verified: bool = False`. Add actual verification
using the `dkimpy` library.

**Files:**
- Modify: `src/parousia/guard/ingest.py` — add DKIM verification step
- Create: `src/parousia/guard/dkim_validator.py` — DKIM verification logic
- Create: `tests/test_dkim_validator.py` — verify DKIM checking
- Modify: `pyproject.toml` — add `dkimpy` dependency

**Step 1: Add `dkimpy` dependency**

In `pyproject.toml`:
```toml
dependencies = [
    # ... existing deps
    "dkimpy>=1.1.0",
]
```

**Step 2: Create `src/parousia/guard/dkim_validator.py`**

```python
"""DKIM signature verification for inbound email."""

import logging
from typing import Tuple

logger = logging.getLogger("parousia.dkim")


def verify_dkim(raw_email: bytes, dns_timeout: float = 5.0) -> Tuple[bool, str]:
    """Verify DKIM signatures in a raw RFC 822 email.

    Args:
        raw_email: Raw email bytes.
        dns_timeout: DNS lookup timeout in seconds.

    Returns:
        (verified, details) tuple. verified=True if at least one
        valid DKIM signature found. details is a human-readable string.
    """
    try:
        import dkim

        verified = dkim.verify(
            raw_email,
            dnsfunc=lambda name: _resolve_txt(name, dns_timeout),
        )
        if verified:
            return (True, "DKIM signature valid")
        else:
            return (False, "No valid DKIM signature found")
    except ImportError:
        logger.warning("dkimpy not installed — DKIM verification skipped")
        return (False, "dkimpy library not available")
    except Exception as e:
        logger.warning("DKIM verification error", extra={"error": str(e)})
        return (False, f"DKIM verification error: {e}")


def _resolve_txt(name: str, timeout: float) -> bytes:
    """Resolve a DNS TXT record, returning the joined string."""
    import socket

    try:
        answers = socket.getaddrinfo(name, None)
        # For simplicity, use the system resolver via subprocess
        import subprocess
        result = subprocess.run(
            ["dig", "+short", "TXT", name],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().encode()
    except Exception:
        pass
    return b""
```

Wait — that DNS resolver is clunky. Let me use the proper approach with `dkim`'s built-in DNS or a cleaner subprocess call.

Actually, the simplest reliable approach for a self-hosted mail server is:

```python
"""DKIM signature verification for inbound email."""

import logging
import subprocess
from typing import Tuple

logger = logging.getLogger("parousia.dkim")


def verify_dkim(raw_email: bytes, dns_timeout: float = 5.0) -> Tuple[bool, str]:
    """Verify DKIM signatures in a raw RFC 822 email."""
    try:
        import dkim

        def dns_resolver(name: bytes, timeout: float = dns_timeout) -> bytes:
            name_str = name.decode() if isinstance(name, bytes) else name
            try:
                result = subprocess.run(
                    ["dig", "+short", "TXT", name_str],
                    capture_output=True, text=True, timeout=timeout,
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Strip surrounding quotes from TXT record
                    txt = result.stdout.strip().strip('"')
                    return txt.encode()
            except (subprocess.TimeoutExpired, Exception):
                pass
            return b""

        verified = dkim.verify(raw_email, dnsfunc=dns_resolver)
        if verified:
            return (True, "DKIM signature valid")
        return (False, "No valid DKIM signature found")
    except ImportError:
        logger.warning("dkimpy not installed — DKIM verification skipped")
        return (False, "dkimpy not available")
    except Exception as e:
        logger.warning("DKIM verification error", extra={"error": str(e)})
        return (False, f"DKIM verification error: {e}")
```

**Step 3: Integrate into `src/parousia/guard/ingest.py`**

After the existing MIME parse in `main()`, add DKIM verification:

```python
from parousia.guard.dkim_validator import verify_dkim

# ... after raw_email is read, add:
dkim_ok, dkim_details = False, "not checked"
if raw_email.strip():
    dkim_ok, dkim_details = verify_dkim(raw_email.encode("utf-8", errors="replace"))

# Include in the payload:
payload = {
    # ... existing fields
    "dkim_verified": dkim_ok,
    "dkim_details": dkim_details,
}
```

**Step 4: Create test with a known-good DKIM-signed email fixture**

```bash
mkdir -p tests/fixtures
# Create a DKIM-signed email fixture (or use a mock)
```

```python
# tests/test_dkim_validator.py
from parousia.guard.dkim_validator import verify_dkim


def test_verify_dkim_no_signature():
    raw = b"From: test@example.com\r\nTo: agent@test.com\r\nSubject: Hi\r\n\r\nHello.\r\n"
    ok, details = verify_dkim(raw)
    assert not ok
    assert "No valid DKIM signature" in details
```

**Step 5: Run tests**

```bash
pytest tests/test_dkim_validator.py -v
```

**Verification:** Incoming email with valid DKIM → `dkim_verified: true` in agent webhook payload. Invalid/missing → `dkim_verified: false`.

---

## Task 3: Human-in-the-Loop Batch Approval (Item 3)

**Objective:** Add an approval queue so outbound emails can be held for human review
before sending. Agent calls `send_email` → held in Redis queue → human approves/rejects
via REST endpoint or CLI → email sent or discarded.

**Files:**
- Create: `src/parousia/guard/approval_queue.py` — Redis-backed approval queue
- Modify: `src/parousia/guard/rest_server.py` — add approval endpoints
- Modify: `src/parousia/guard/mcp_server.py` — integrate approval check
- Create: `src/parousia/cli/approval.py` — CLI commands for approve/reject/list
- Modify: `src/parousia/cli/main.py` — register approval command group
- Create: `tests/test_approval_queue.py`
- Modify: `src/parousia/config.py` — add approval config

**Step 1: Add approval config to `config.py`**

```python
class ApprovalConfig(BaseModel):
    enabled: bool = False  # Opt-in: off by default
    queue_ttl_hours: int = 72  # Auto-expire unapproved emails after 72h
    require_approval_for: list[str] = Field(default_factory=list)  # agent IDs that need approval


class ParousiaConfig(BaseModel):
    # ... existing fields
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
```

**Step 2: Create `src/parousia/guard/approval_queue.py`**

```python
"""Redis-backed human-in-the-loop approval queue for outbound email."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("parousia.approval")


class ApprovalQueue:
    """Holds outbound emails pending human approval."""

    QUEUE_KEY = "approval:pending"
    ITEM_PREFIX = "approval:item:"

    def __init__(self, redis_client):
        self._redis = redis_client

    def enqueue(
        self,
        agent_id: str,
        to: str,
        subject: str,
        body: str,
        from_addr: str,
        reply_to: Optional[str] = None,
        ttl_hours: int = 72,
    ) -> str:
        """Place an email in the approval queue. Returns the approval_id."""
        approval_id = str(uuid.uuid4())[:12]
        item = {
            "approval_id": approval_id,
            "agent_id": agent_id,
            "to": to,
            "subject": subject,
            "body": body,
            "from_addr": from_addr,
            "reply_to": reply_to,
            "status": "pending",  # pending | approved | rejected
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._redis.setex(
            f"{self.ITEM_PREFIX}{approval_id}",
            ttl_hours * 3600,
            json.dumps(item),
        )
        self._redis.lpush(self.QUEUE_KEY, approval_id)
        logger.info("email queued for approval", extra={"approval_id": approval_id, "agent_id": agent_id})
        return approval_id

    def list_pending(self, limit: int = 50) -> list[dict]:
        """List all pending approval items."""
        ids = self._redis.lrange(self.QUEUE_KEY, 0, limit - 1)
        items = []
        for aid in ids:
            raw = self._redis.get(f"{self.ITEM_PREFIX}{aid.decode() if isinstance(aid, bytes) else aid}")
            if raw:
                items.append(json.loads(raw))
        return items

    def get_item(self, approval_id: str) -> Optional[dict]:
        raw = self._redis.get(f"{self.ITEM_PREFIX}{approval_id}")
        return json.loads(raw) if raw else None

    def approve(self, approval_id: str) -> Optional[dict]:
        item = self.get_item(approval_id)
        if not item or item["status"] != "pending":
            return None
        item["status"] = "approved"
        item["approved_at"] = datetime.now(timezone.utc).isoformat()
        self._redis.setex(
            f"{self.ITEM_PREFIX}{approval_id}",
            3600,  # Keep approved items for 1h for traceability
            json.dumps(item),
        )
        self._redis.lrem(self.QUEUE_KEY, 0, approval_id)
        logger.info("email approved", extra={"approval_id": approval_id})
        return item

    def reject(self, approval_id: str, reason: str = "") -> Optional[dict]:
        item = self.get_item(approval_id)
        if not item or item["status"] != "pending":
            return None
        item["status"] = "rejected"
        item["rejected_at"] = datetime.now(timezone.utc).isoformat()
        item["reject_reason"] = reason
        self._redis.setex(
            f"{self.ITEM_PREFIX}{approval_id}",
            3600,
            json.dumps(item),
        )
        self._redis.lrem(self.QUEUE_KEY, 0, approval_id)
        logger.info("email rejected", extra={"approval_id": approval_id, "reason": reason})
        return item
```

**Step 3: Integrate into MCP `send_email` handler**

In `_handle_send_email`, after rate limit check, before SMTP send:

```python
# Check if agent requires human approval
if config.approval.enabled and agent_id in config.approval.require_approval_for:
    approval_queue = ApprovalQueue(redis_client)
    approval_id = approval_queue.enqueue(
        agent_id=agent_id,
        to=to,
        subject=subject,
        body=body,
        from_addr=from_addr,
        reply_to=reply_to,
        ttl_hours=config.approval.queue_ttl_hours,
    )
    return [TextContent(
        type="text",
        text=json.dumps({
            "sent": False,
            "queued_for_approval": True,
            "approval_id": approval_id,
            "message": "Email held for human review. It will be sent upon approval.",
        }),
    )]
```

**Step 4: Add REST endpoints for approval**

In `rest_server.py`, add:

```python
@app.get("/approval/pending")
async def list_pending():
    """List all emails pending human approval."""
    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db)
    q = ApprovalQueue(r)
    return JSONResponse({"pending": q.list_pending()})


@app.post("/approval/{approval_id}/approve")
async def approve_email(approval_id: str):
    """Approve a pending email for sending."""
    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db)
    q = ApprovalQueue(r)
    item = q.approve(approval_id)
    if not item:
        raise HTTPException(status_code=404, detail="Approval item not found or already processed")
    # Now actually send the email
    from parousia.guard.email_sender import send_email as _smtp_send
    try:
        msg_id = _smtp_send(
            to=item["to"], subject=item["subject"], body=item["body"],
            from_addr=item["from_addr"], reply_to=item.get("reply_to"),
        )
        return JSONResponse({"sent": True, "message_id": msg_id, "approval_id": approval_id})
    except Exception as e:
        return JSONResponse({"sent": False, "error": str(e), "approval_id": approval_id}, status_code=500)


@app.post("/approval/{approval_id}/reject")
async def reject_email(approval_id: str, reason: str = ""):
    """Reject a pending email."""
    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db)
    q = ApprovalQueue(r)
    item = q.reject(approval_id, reason)
    if not item:
        raise HTTPException(status_code=404, detail="Approval item not found or already processed")
    return JSONResponse({"rejected": True, "approval_id": approval_id})
```

**Step 5: Add CLI commands**

```python
# src/parousia/cli/approval.py
"""CLI commands for human-in-the-loop email approval."""

import click
import redis as redis_lib
from parousia.config import load_config
from parousia.guard.approval_queue import ApprovalQueue
from parousia.guard.email_sender import send_email as _smtp_send


@click.group("approval")
def approval_group():
    """Manage the email approval queue."""


@approval_group.command("list")
def list_pending():
    """List pending approval items."""
    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db)
    q = ApprovalQueue(r)
    items = q.list_pending()
    if not items:
        click.echo("No pending approval items.")
        return
    for item in items:
        click.echo(f"[{item['approval_id']}] {item['agent_id']} → {item['to']}: {item['subject'][:80]}")


@approval_group.command("approve")
@click.argument("approval_id")
def approve(approval_id):
    """Approve and send a pending email."""
    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db)
    q = ApprovalQueue(r)
    item = q.approve(approval_id)
    if not item:
        click.echo(f"Error: approval item {approval_id} not found or already processed.", err=True)
        return
    try:
        msg_id = _smtp_send(
            to=item["to"], subject=item["subject"], body=item["body"],
            from_addr=item["from_addr"], reply_to=item.get("reply_to"),
        )
        click.echo(f"Approved and sent: {msg_id}")
    except Exception as e:
        click.echo(f"Approved but send failed: {e}", err=True)


@approval_group.command("reject")
@click.argument("approval_id")
@click.option("--reason", default="", help="Rejection reason")
def reject(approval_id, reason):
    """Reject a pending email."""
    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db)
    q = ApprovalQueue(r)
    item = q.reject(approval_id, reason)
    if item:
        click.echo(f"Rejected: {approval_id}")
    else:
        click.echo(f"Error: approval item {approval_id} not found or already processed.", err=True)
```

**Step 6: Register in `main.py`**

```python
# In cli/main.py, add:
from parousia.cli.approval import approval_group
cli.add_command(approval_group)
```

**Step 7: Add tests**

```python
# tests/test_approval_queue.py
def test_enqueue_and_list():
    r = fakeredis.FakeRedis()
    q = ApprovalQueue(r)
    aid = q.enqueue("hermes", "test@example.com", "Subject", "Body", "hermes@domain.com")
    assert aid
    items = q.list_pending()
    assert len(items) == 1
    assert items[0]["status"] == "pending"

def test_approve():
    r = fakeredis.FakeRedis()
    q = ApprovalQueue(r)
    aid = q.enqueue("hermes", "test@example.com", "Subject", "Body", "hermes@domain.com")
    item = q.approve(aid)
    assert item["status"] == "approved"
    # Queue should be empty after approval
    assert len(q.list_pending()) == 0

def test_reject():
    r = fakeredis.FakeRedis()
    q = ApprovalQueue(r)
    aid = q.enqueue("hermes", "test@example.com", "Subject", "Body", "hermes@domain.com")
    item = q.reject(aid, "Spam")
    assert item["status"] == "rejected"
    assert item["reject_reason"] == "Spam"

def test_double_approve_is_noop():
    r = fakeredis.FakeRedis()
    q = ApprovalQueue(r)
    aid = q.enqueue("hermes", "test@example.com", "Subject", "Body", "hermes@domain.com")
    q.approve(aid)
    result = q.approve(aid)
    assert result is None
```

**Step 8: Run tests**

```bash
pytest tests/test_approval_queue.py -v
```

**Verification:**
- Agent in `require_approval_for` list → `send_email` returns `queued_for_approval: true`
- `parousia-guard approval list` shows pending items
- `parousia-guard approval approve <id>` sends the email
- `parousia-guard approval reject <id> --reason "spam"` discards it

---

## Task 4: Monitoring Dashboard & Alerting (Item 7)

**Objective:** Add a `/dashboard` HTML page to the REST server showing real-time
health metrics (queue depth, rate limits, Redis status, uptime). Add a CLI `monitor`
command for terminal-based status. Add a cron health-check script with alerting.

**Files:**
- Create: `src/parousia/monitoring/dashboard.py` — metrics collection + HTML render
- Create: `src/parousia/monitoring/__init__.py`
- Modify: `src/parousia/guard/rest_server.py` — add `/dashboard` endpoint
- Create: `src/parousia/monitoring/templates/dashboard.html` — Jinja2 HTML template
- Create: `src/parousia/cli/monitor.py` — terminal-based monitor
- Modify: `src/parousia/cli/main.py` — register monitor command
- Create: `tests/test_monitoring.py`

**Step 1: Create `src/parousia/monitoring/dashboard.py`**

```python
"""Health metrics collection for Parousia monitoring dashboard."""

import os
import time
import subprocess
from datetime import datetime, timezone


def collect_metrics(config, redis_client, temporal_db=None) -> dict:
    """Collect all health metrics into a single dict."""
    now = time.time()

    metrics = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server": {
            "uptime_seconds": _get_uptime(),
            "hostname": os.uname().nodename,
        },
        "redis": _check_redis(redis_client),
        "postfix": _check_postfix(),
        "rate_limits": _get_rate_limits(redis_client, config),
        "mail_queue": _get_mail_queue(),
    }

    if temporal_db:
        metrics["temporal"] = _get_temporal_stats(temporal_db)

    return metrics


def _get_uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.readline().split()[0])


def _check_redis(redis_client) -> dict:
    try:
        redis_client.ping()
        info = redis_client.info("memory")
        return {
            "status": "ok",
            "used_memory_human": info.get("used_memory_human", "unknown"),
            "connected_clients": info.get("connected_clients", 0),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_postfix() -> dict:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "postfix"],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip() == "active"
        return {"status": "ok" if active else "down", "active": active}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_rate_limits(redis_client, config) -> dict:
    agents = {}
    for agent_id in config.agents:
        try:
            count = redis_client.get(f"rate:agent:{agent_id}")
            agents[agent_id] = int(count) if count else 0
        except Exception:
            agents[agent_id] = -1
    try:
        domain_count = redis_client.get("rate:domain")
        domain = int(domain_count) if domain_count else 0
    except Exception:
        domain = -1
    return {
        "per_agent": agents,
        "domain": domain,
        "limits": {
            "per_agent_per_hour": config.rate_limits.per_agent_per_hour,
            "domain_per_day": config.rate_limits.domain_per_day,
        },
    }


def _get_mail_queue() -> dict:
    try:
        result = subprocess.run(
            ["mailq"], capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        # Last line of mailq is typically "Mail queue is empty" or "-- N Kbytes in M Requests"
        queue_size = 0
        if "empty" not in result.stdout:
            for line in lines:
                if "Requests" in line or "Request" in line:
                    try:
                        queue_size = int(line.strip().split()[-2])
                    except (ValueError, IndexError):
                        pass
        return {"size": queue_size, "status": "ok" if queue_size < 100 else "warning"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_temporal_stats(temporal_db) -> dict:
    try:
        import sqlite3
        db = temporal_db._conn if temporal_db._conn else sqlite3.connect(
            temporal_db.db_path or "/var/lib/parousia/temporal.db"
        )
        events = db.execute("SELECT COUNT(*) FROM temporal_events").fetchone()[0]
        journal = db.execute("SELECT COUNT(*) FROM temporal_journal").fetchone()[0]
        return {"events": events, "journal_entries": journal, "status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
```

**Step 2: Create HTML dashboard template**

```html
<!-- src/parousia/monitoring/templates/dashboard.html -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Parousia — Monitoring Dashboard</title>
    <style>
        :root { color-scheme: dark; }
        body { font-family: system-ui; background: #111; color: #eee; margin: 2rem; }
        .card { background: #1a1a1a; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem; border: 1px solid #333; }
        .ok { color: #4ade80; } .warn { color: #fbbf24; } .error { color: #f87171; }
        .metric { display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid #222; }
        h2 { margin-top: 0; }
    </style>
    <script>
        async function refresh() {
            const resp = await fetch('/metrics');
            const data = await resp.json();
            document.getElementById('content').textContent = JSON.stringify(data, null, 2);
            document.getElementById('updated').textContent = 'Updated: ' + data.timestamp;
        }
        setInterval(refresh, 5000);
        refresh();
    </script>
</head>
<body>
    <h1>Parousia Monitoring <span id="updated" style="font-size: 0.7em; color: #888;"></span></h1>
    <pre id="content" style="white-space: pre-wrap; font-size: 0.85em;">Loading...</pre>
</body>
</html>
```

**Step 3: Add REST endpoints in `rest_server.py`**

```python
@app.get("/metrics")
async def metrics():
    """Return health metrics as JSON."""
    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db, socket_connect_timeout=2)
    from parousia.monitoring.dashboard import collect_metrics
    return JSONResponse(collect_metrics(config, r))


@app.get("/dashboard")
async def dashboard():
    """Serve the monitoring dashboard HTML page."""
    from fastapi.responses import HTMLResponse
    import os
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "monitoring", "templates", "dashboard.html"
    )
    with open(template_path) as f:
        html = f.read()
    return HTMLResponse(html)
```

**Step 4: Add CLI monitor command**

```python
# src/parousia/cli/monitor.py
@click.command("monitor")
@click.option("--interval", default=5, help="Refresh interval in seconds")
@click.option("--once", is_flag=True, help="Print metrics once and exit")
def monitor(interval, once):
    """Monitor Parousia health metrics in real-time."""
    import time as _time
    import json as _json
    import redis as redis_lib
    from parousia.config import load_config
    from parousia.monitoring.dashboard import collect_metrics

    config = load_config()
    r = redis_lib.Redis(host=config.redis.host, port=config.redis.port, db=config.redis.db)

    while True:
        metrics = collect_metrics(config, r)
        click.clear()
        click.echo("=== Parousia Health Monitor ===\n")
        click.echo(f"Timestamp: {metrics['timestamp']}")
        click.echo(f"Host: {metrics['server']['hostname']}")
        click.echo(f"Uptime: {metrics['server']['uptime_seconds']:.0f}s")
        click.echo(f"Redis: {metrics['redis']['status']}")
        click.echo(f"Postfix: {metrics['postfix']['status']}")
        click.echo(f"Mail Queue: {metrics['mail_queue']['size']} items")
        click.echo(f"Rate Limits: agent={metrics['rate_limits']['per_agent']}, domain={metrics['rate_limits']['domain']}/{metrics['rate_limits']['limits']['domain_per_day']}")
        if once:
            break
        _time.sleep(interval)
```

**Step 5: Add alerting script**

```bash
# Create scripts/parousia-health-check.sh
#!/bin/bash
# Parousia health check — runs on cron, alerts on failure.
# Schedule: */5 * * * * /opt/parousia/scripts/parousia-health-check.sh

HEALTH=$(curl -s http://localhost:8080/health)
REDIS_OK=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('redis',False))")

if [ "$REDIS_OK" != "True" ]; then
    echo "Parousia ALERT: Redis is down on $(hostname) at $(date)" | \
        mail -s "ALERT: Parousia Redis Down" postmaster@localhost
fi

if ! systemctl is-active --quiet postfix; then
    echo "Parousia ALERT: Postfix is down on $(hostname) at $(date)" | \
        mail -s "ALERT: Parousia Postfix Down" postmaster@localhost
fi
```

**Step 6: Run tests**

```bash
pytest tests/test_monitoring.py -v
```

**Verification:**
- `curl http://localhost:8080/metrics` returns JSON with all metrics
- `curl http://localhost:8080/dashboard` returns HTML page
- `parousia-guard monitor --once` prints health summary
- Health check script runs in cron without errors

---

## Task 5: TLS Certificates for Postfix (Item 6)

**Objective:** Add a CLI command that sets up Let's Encrypt certificates via certbot
and configures Postfix for STARTTLS with proper certificates.

**Files:**
- Create: `src/parousia/cli/tls.py` — certbot setup and Postfix TLS config
- Modify: `src/parousia/cli/main.py` — register `setup --tls` command
- No Python dependencies needed (wraps certbot + postconf)

**Step 1: Create `src/parousia/cli/tls.py`**

```python
"""TLS certificate setup for Postfix via Let's Encrypt / certbot."""

import subprocess
import sys
from pathlib import Path

import click


DOMAIN_CONFIG = """
# TLS parameters (managed by parousia-guard setup --tls)
smtpd_tls_cert_file = {cert_path}
smtpd_tls_key_file = {key_path}
smtpd_tls_security_level = may
smtpd_tls_loglevel = 1
smtp_tls_security_level = may
smtp_tls_loglevel = 1
smtpd_tls_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtp_tls_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtpd_tls_mandatory_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtp_tls_mandatory_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtpd_tls_received_header = yes
"""


@click.command("tls")
@click.option("--domain", help="Domain for TLS certificate (default: mx.<domain> from config)")
@click.option("--email", help="Email for Let's Encrypt notifications")
@click.option("--staging", is_flag=True, help="Use Let's Encrypt staging (for testing)")
@click.option("--dry-run", is_flag=True, help="Print what would be done without doing it")
def setup_tls(domain, email, staging, dry_run):
    """Set up TLS certificates for Postfix using Let's Encrypt."""
    from parousia.config import load_config

    config = load_config()

    if not domain:
        domain = config.hostname

    if not email:
        click.echo("Error: --email is required for Let's Encrypt notifications.", err=True)
        sys.exit(1)

    cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"

    # Step 1: Run certbot
    cmd = [
        "sudo", "certbot", "certonly",
        "--standalone",
        "--non-interactive",
        "--agree-tos",
        "-d", domain,
        "-m", email,
    ]
    if staging:
        cmd.append("--staging")

    if dry_run:
        cmd.append("--dry-run")
        click.echo(f"Would run: {' '.join(cmd)}")
    else:
        click.echo("Obtaining Let's Encrypt certificate...")
        try:
            subprocess.run(cmd, check=True, timeout=120)
            click.echo(f"✓ Certificate obtained: {cert_path}")
        except subprocess.CalledProcessError:
            click.echo("✗ certbot failed. Check that:", err=True)
            click.echo("  - Port 80 is open and reachable", err=True)
            click.echo(f"  - DNS A record for {domain} points to this server", err=True)
            click.echo("  - No other web server is using port 80", err=True)
            sys.exit(1)

    # Step 2: Configure Postfix
    tls_config = DOMAIN_CONFIG.format(cert_path=cert_path, key_path=key_path)

    if dry_run:
        click.echo(f"\nWould add to Postfix main.cf:\n{tls_config}")
    else:
        # Write TLS snippet
        snippet_path = Path("/etc/postfix/tls.conf")
        snippet_path.write_text(tls_config)
        click.echo(f"✓ Wrote TLS config to {snippet_path}")

        # Include in main.cf if not already
        main_cf = Path("/etc/postfix/main.cf")
        content = main_cf.read_text()
        if "tls.conf" not in content:
            with main_cf.open("a") as f:
                f.write(f"\n# TLS configuration\n!include {snippet_path}\n")
            click.echo("✓ Added !include to /etc/postfix/main.cf")

        # Reload Postfix
        subprocess.run(["sudo", "systemctl", "reload", "postfix"], check=True)
        click.echo("✓ Postfix reloaded")

    # Step 3: Verify
    if not dry_run:
        click.echo("\nVerifying TLS...")
        result = subprocess.run(
            ["openssl", "s_client", "-connect", f"localhost:25", "-starttls", "smtp", "-brief"],
            input="QUIT\n", capture_output=True, text=True, timeout=10,
        )
        if "Verification: OK" in result.stdout or "Verification error" not in result.stderr:
            click.echo("✓ STARTTLS working on port 25")
        else:
            click.echo("⚠ STARTTLS check produced output:", err=True)
            click.echo(result.stdout[:500])

    # Step 4: Auto-renewal reminder
    click.echo("\n---")
    click.echo("Let's Encrypt certificates renew automatically via certbot timer.")
    click.echo("Verify renewal: sudo certbot renew --dry-run")
```

**Step 2: Register in `main.py`**

In `cli/main.py`, add `--tls` to the `setup` command group or register as standalone:

```python
# Add to setup command group
from parousia.cli.tls import setup_tls
setup_group.add_command(setup_tls, name="tls")
```

**Verification:**
- `parousia-guard setup tls --domain mx.example.com --email admin@example.com`
- Postfix main.cf gains `!include /etc/postfix/tls.conf`
- `openssl s_client -connect localhost:25 -starttls smtp` succeeds

---

## Task 6: postfwd Tier 2 Rate Limiting (Item 2)

**Objective:** Install and configure postfwd as a Postfix policy service for Tier 2
rate limiting at the SMTP level. This blocks spam before it reaches the guard.

**Files:**
- Create: `scripts/postfwd-setup.sh` — installation and config script
- Create: `config/postfwd/rules.cf` — postfwd rate-limit rules
- Modify: `src/parousia/cli/setup.py` — add `--postfwd` flag
- No Python code changes needed beyond CLI setup command

**Step 1: Create `config/postfwd/rules.cf`**

```
# Tier 2 rate limiting — SMTP-level before guard
# postfwd rules file (postfwd v2)

# Per-sender rate limit: max 30 emails per hour from any single sender
id=RATE_SENDER_PER_HOUR
    sender=~/.*/
    action=rate(sender/30/3600/450 4.7.1 Throttled — too many emails from sender, try again later)

# Per-recipient rate limit: max 60 emails per hour to any single agent
id=RATE_RECIPIENT_PER_HOUR
    recipient=~/.*@agents\..*/
    action=rate(recipient/60/3600/450 4.7.1 Throttled — agent receiving too many emails, try again later)

# Global rate limit: max 300 connections per hour total
id=RATE_GLOBAL_PER_HOUR
    action=rate(client_address/300/3600/450 4.7.1 Throttled — server busy, try again later)

# Reject if HELO is not a FQDN (basic spam check)
id=HELO_CHECK
    helo_name=~/[^.]/
    action=REJECT Invalid HELO — must be a fully qualified domain name
```

**Step 2: Create `scripts/postfwd-setup.sh`**

```bash
#!/bin/bash
# Install and configure postfwd for Tier 2 rate limiting
set -e

echo "=== Installing postfwd ==="
sudo apt-get update -qq
sudo apt-get install -y postfwd

echo "=== Deploying rules ==="
sudo cp config/postfwd/rules.cf /etc/postfix/postfwd.cf
sudo chmod 644 /etc/postfix/postfwd.cf

echo "=== Starting postfwd ==="
sudo systemctl enable postfwd
sudo systemctl start postfwd

echo "=== Configuring Postfix to use postfwd ==="
sudo postconf -e "smtpd_recipient_restrictions = check_policy_service inet:127.0.0.1:10040, permit_mynetworks, reject_unauth_destination"

echo "=== Reloading Postfix ==="
sudo systemctl reload postfix

echo "=== Verifying ==="
sudo systemctl status postfwd --no-pager
echo ""
echo "✓ postfwd Tier 2 rate limiting active."
echo "  Rules: /etc/postfix/postfwd.cf"
echo "  Policy service: 127.0.0.1:10040"
echo "  Logs: journalctl -u postfwd -f"
```

**Step 3: Add CLI setup integration**

In `src/parousia/cli/setup.py`, add:

```python
@setup_group.command("postfwd")
@click.option("--rules-file", default=None, help="Path to postfwd rules file")
def setup_postfwd(rules_file):
    """Configure postfwd Tier 2 rate limiting."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).parent.parent.parent.parent / "scripts" / "postfwd-setup.sh"
    if script.exists():
        subprocess.run(["sudo", "bash", str(script)], check=True)
    else:
        click.echo("postfwd-setup.sh not found. Install postfwd manually.")
```

**Verification:**
- `parousia-guard setup --postfwd` installs and configures postfwd
- `sudo systemctl status postfwd` shows active
- Sending >30 emails in an hour from same sender triggers 4.7.1 rejection

---

## Execution Order

```
Task 1: Multi-Agent Routing (foundational — needed by Tasks 3, 5)
    ↓
Task 2: DKIM Inbound Validation (independent, but benefits from multi-agent context)
    ↓
Task 3: Human-in-the-Loop Approval (depends on Task 1 for agent resolution)
    ↓
Task 4: Monitoring Dashboard (independent)
    ↓
Task 5: TLS Certificates (independent, infrastructure)
    ↓
Task 6: postfwd Tier 2 (independent, infrastructure)
```

## Test Plan

```bash
# After all tasks complete, run full suite
cd ~/workspace/Parousia
pip install -e ".[dev]"
pytest tests/ -v --tb=short

# Expected: all existing 180+ tests still pass + new tests for Tasks 2-4
```

---

## Config File Additions (cumulative)

```yaml
# Add to /etc/parousia/config.yaml:

# Human-in-the-loop approval (Task 3)
approval:
  enabled: false                     # Set true to enable
  queue_ttl_hours: 72               # Auto-expire after 72h
  require_approval_for: []           # Agent IDs needing approval, e.g. ["hermes"]

# Monitoring (Task 4) — uses existing config sections, no new keys needed
```
