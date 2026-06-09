"""REST ingest server — FastAPI endpoint for Postfix pipe handoff.

Accepts parsed email JSON, enforces rate limits, and forwards to agent webhook.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from parousia.config import load_config
from parousia.auth.accounts import AccountStore
from parousia.auth.middleware import AgentAuthMiddleware, get_account
from parousia.auth.onboard import OnboardRequest, OnboardResponse, handle_onboard

logger = logging.getLogger("parousia.rest")

app = FastAPI(title="Parousia Guard — REST Ingress")

# ── Account store & auth middleware ──────────────────

_account_store = AccountStore()


@app.on_event("startup")
async def startup_accounts():
    _account_store.connect()


app.add_middleware(
    AgentAuthMiddleware,
    account_store=_account_store,
    public_paths={
        "/health", "/onboard", "/docs", "/openapi.json",
        "/ingest", "/approval/pending", "/metrics", "/dashboard",
    },
)


class IngestRequest(BaseModel):
    sender: str = Field(..., description="From address of the email")
    recipient: str = Field(..., description="To address (agent@domain)")
    subject: str = Field(default="", description="Email subject line")
    body: str = Field(default="", description="Plain-text email body")
    agent_id: str = Field(..., description="Agent identifier (local part of recipient)")
    raw_mime: str = Field(default="", description="Raw RFC 822 message")
    dkim_verified: bool = Field(default=False)
    spf_verified: bool = Field(default=False)
    timestamp: Optional[str] = Field(default=None)


class IngestResponse(BaseModel):
    status: str = "accepted"
    agent_id: str
    task_id: str


@app.get("/health")
async def health():
    """Health check — returns server and Redis status."""
    try:
        import redis as redis_lib

        r = redis_lib.Redis(host="localhost", port=6379, db=0, socket_connect_timeout=2)
        r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return JSONResponse({
        "status": "ok",
        "redis": redis_ok,
        "postfix": True,  # assumed running if this server is up
    })


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest):
    """Accept parsed email, route to agent webhook.

    Called by the Postfix pipe script (parousia-guard ingest).
    Must return within 2 seconds so Postfix doesn't timeout.
    """
    config = load_config()

    # Look up agent config
    agent_cfg = config.agents.get(request.agent_id)
    if not agent_cfg:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {request.agent_id}")

    task_id = str(uuid.uuid4())[:12]

    # Fire-and-forget: forward to agent webhook asynchronously
    # Postfix pipe expects fast response — don't wait for agent
    import asyncio

    asyncio.create_task(_forward_to_agent(
        webhook_url=agent_cfg.webhook_url,
        agent_id=request.agent_id,
        task_id=task_id,
        sender=request.sender,
        subject=request.subject,
        body=request.body,
        raw_mime=request.raw_mime,
    ))

    logger.info(
        "ingest accepted",
        extra={
            "agent_id": request.agent_id,
            "task_id": task_id,
            "sender": request.sender,
            "subject": request.subject[:100],
        },
    )

    return IngestResponse(agent_id=request.agent_id, task_id=task_id)


async def _forward_to_agent(
    webhook_url: str,
    agent_id: str,
    task_id: str,
    sender: str,
    subject: str,
    body: str,
    raw_mime: str,
    max_retries: int = 3,
):
    """Forward task to agent webhook with retry on failure."""
    import httpx

    payload = {
        "task_type": "email",
        "task_id": task_id,
        "sender": sender,
        "subject": subject,
        "body": body,
        "raw_mime": raw_mime,
        "agent_id": agent_id,
    }

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code < 500:
                    logger.info(
                        "webhook delivered",
                        extra={"task_id": task_id, "status": resp.status_code, "attempt": attempt},
                    )
                    return
                logger.warning(
                    "webhook server error",
                    extra={"task_id": task_id, "status": resp.status_code, "attempt": attempt},
                )
        except Exception as e:
            logger.warning(
                "webhook unreachable",
                extra={"task_id": task_id, "error": str(e), "attempt": attempt},
            )

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # exponential backoff: 2s, 4s, 8s

    logger.error("webhook exhausted retries", extra={"task_id": task_id})


# ── Approval endpoints ──────────────────────────

@app.get("/approval/pending")
async def list_pending():
    """List all emails pending human approval."""
    config = load_config()
    import redis as redis_lib
    from parousia.guard.approval_queue import ApprovalQueue

    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )
    q = ApprovalQueue(r)
    return JSONResponse({"pending": q.list_pending()})


@app.post("/approval/{approval_id}/approve")
async def approve_email(approval_id: str):
    """Approve a pending email and send it."""
    config = load_config()
    import redis as redis_lib
    from parousia.guard.approval_queue import ApprovalQueue
    from parousia.guard.email_sender import send_email as _smtp_send

    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )
    q = ApprovalQueue(r)
    item = q.approve(approval_id)
    if not item:
        raise HTTPException(status_code=404, detail="Not found or already processed")

    try:
        msg_id = _smtp_send(
            to=item["to"], subject=item["subject"], body=item["body"],
            from_addr=item["from_addr"], reply_to=item.get("reply_to"),
        )
        return JSONResponse({"sent": True, "message_id": msg_id, "approval_id": approval_id})
    except Exception as e:
        return JSONResponse(
            {"sent": False, "error": str(e), "approval_id": approval_id},
            status_code=500,
        )


@app.post("/approval/{approval_id}/reject")
async def reject_email(approval_id: str, reason: str = ""):
    """Reject a pending email."""
    config = load_config()
    import redis as redis_lib
    from parousia.guard.approval_queue import ApprovalQueue

    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )
    q = ApprovalQueue(r)
    item = q.reject(approval_id, reason)
    if not item:
        raise HTTPException(status_code=404, detail="Not found or already processed")
    return JSONResponse({"rejected": True, "approval_id": approval_id})


# ── Monitoring endpoints ───────────────────────

@app.get("/metrics")
async def metrics():
    """Return health metrics as JSON."""
    config = load_config()
    import redis as redis_lib
    from parousia.monitoring.dashboard import collect_metrics

    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )
    return JSONResponse(collect_metrics(config, r))


@app.get("/dashboard")
async def dashboard():
    """Serve the monitoring dashboard HTML page."""
    from fastapi.responses import HTMLResponse

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Parousia — Monitoring</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui; background: #111; color: #eee; margin: 2rem; }
  .card { background: #1a1a1a; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem; border: 1px solid #333; }
  .ok { color: #4ade80; } .warn { color: #fbbf24; } .error { color: #f87171; }
  h1 { margin-top: 0; }
  pre { white-space: pre-wrap; font-size: 0.85em; }
</style>
</head>
<body>
<h1>Parousia Monitoring</h1>
<p>Auto-refreshing every 5 seconds via <a href="/metrics" style="color:#60a5fa;">/metrics</a></p>
<pre id="data">Loading...</pre>
<script>
async function refresh() {
  try {
    const r = await fetch('/metrics');
    const d = await r.json();
    document.getElementById('data').textContent = JSON.stringify(d, null, 2);
  } catch(e) {
    document.getElementById('data').textContent = 'Error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ── Onboarding & Account endpoints ──────────────────


@app.post("/onboard", response_model=OnboardResponse)
async def onboard(request: OnboardRequest):
    return handle_onboard(_account_store, request)


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


@app.post("/account/rotate-key")
async def rotate_key(request: Request):
    account = get_account(request)
    new_key = _account_store.rotate_key(account.account_id)
    if not new_key:
        raise HTTPException(status_code=500, detail="Key rotation failed")
    return {
        "account_id": account.account_id,
        "new_api_key": new_key,
        "message": "Key rotated! Save this new key — it will not be shown again.",
    }
