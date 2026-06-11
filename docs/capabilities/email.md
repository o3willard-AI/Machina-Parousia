# Email — Capability Guide

Agents send and receive email at `agent@yourdomain.com`. No IMAP, no mailboxes, no polling. Push-based ingest pipes every inbound message directly to your agent's context.

---

## Tools

### `send_email`

Send email from an agent identity.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `to` | ✅ | Recipient email address |
| `subject` | ✅ | Email subject line |
| `body` | ✅ | Plain-text email body |
| `from_agent` | ❌ | Agent ID to send from (defaults to first configured agent) |
| `reply_to` | ❌ | Reply-To address |

**Rate limits:** 100/hr per agent, 500/day domain-wide. Redis-backed token bucket.

**Request example:**
```json
{
  "tool": "send_email",
  "arguments": {
    "to": "colleague@example.com",
    "subject": "Build passed on PR #47",
    "body": "All tests green. Ready for review.",
    "from_agent": "hermes"
  }
}
```

**Response:**
```json
{
  "sent": true,
  "message_id": "<20260611abc123@yourdomain.com>"
}
```

**Errors:**
```json
{
  "sent": false,
  "error": "Rate limit exceeded for agent 'hermes'. Try again in 12 minutes.",
  "retry_after_seconds": 720
}
```

```json
{
  "sent": false,
  "error": "Unknown agent: ghost",
  "available_agents": ["hermes", "mr-krabs", "architect"]
}
```

---

### `check_inbox`

Read an agent's email inbox. Messages are stored per-agent in SQLite, delivered push-style via Postfix pipe — no polling overhead.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `unread_only` | ❌ | If `true`, return only unread messages (default: `true`) |
| `limit` | ❌ | Max messages to return (default: 20, max: 100) |

**Request:**
```json
{
  "tool": "check_inbox",
  "arguments": {
    "unread_only": true,
    "limit": 5
  }
}
```

**Response:**
```json
{
  "agent_id": "hermes",
  "count": 2,
  "messages": [
    {
      "id": "msg_abc123",
      "sender": "stephen@example.com",
      "recipient": "hermes@yourdomain.com",
      "subject": "Deploy Parousia to production",
      "body": "The staging tests passed. Let's ship it today.",
      "received_at": "2026-06-11T14:30:00Z",
      "read": false
    },
    {
      "id": "msg_def456",
      "sender": "github@notifications.github.com",
      "recipient": "hermes@yourdomain.com",
      "subject": "[MR-Krabs] PR #48 merged",
      "body": "Pull request #48 was merged into main.",
      "received_at": "2026-06-11T13:15:00Z",
      "read": false
    }
  ]
}
```

Messages are marked read when returned. To re-read old messages, set `unread_only: false`.

---

## How inbound mail flows

```
Internet → Postfix :25
  → master.cf pipe transport
  → /opt/parousia/parousia_pipe.py (stdin MIME parse)
  → POST http://127.0.0.1:8080/ingest
  → InboxStore.insert() (SQLite)
  → "accepted" response (within 250ms)
```

Key properties:
- **Fire-and-forget at the MTA level** — Postfix accepts mail before Parousia processes it. If Parousia is down, Postfix queues and retries.
- **Per-agent SQLite inboxes** — no shared mailbox, no cross-agent reads.
- **No IMAP/POP3** — agents read via MCP `check_inbox` only. Clean, simple, no legacy protocol baggage.

---

## How outbound mail flows

```
Agent → MCP send_email (stdio transport)
  → AccountStore.auth() (validate API key)
  → RateLimiter.check() (Redis token bucket)
  → smtplib.SMTP('localhost', 25)
  → Postfix local submission
  → DNS MX lookup for recipient domain
  → Direct delivery to recipient MTA (or SES relay if configured)
```

Postfix handles queueing, retries, and bounce handling. Parousia never directly connects to external mail servers — Postfix is the sole MTA.

---

## Rate limiting

Two layers of rate limiting:

| Layer | Mechanism | Limit |
|-------|-----------|-------|
| **Application** (Parousia Guard) | Redis token bucket | 100/hr per agent, 500/day domain-wide |
| **MTA** (postfwd) | SMTP-level rules | Configurable in `config/postfwd/rules.cf` |

When rate limits are hit, `send_email` returns an error with `retry_after_seconds`. The agent should wait and retry.

---

## Approval queue (human-in-the-loop)

Parousia supports an optional approval queue for outbound mail. When enabled, emails from specified agents are held for human review before delivery.

**Enable in config:**
```yaml
approval:
  enabled: true
  queue_ttl_hours: 72
  require_approval_for:
    - mr-krabs
    - architect
```

**CLI commands:**
```bash
parousia-guard approval list              # Show pending approvals
parousia-guard approval approve <id>      # Approve and send
parousia-guard approval reject <id> --reason "Spam content"
```

**REST endpoints:**
- `GET /approval/pending` — list pending messages
- `POST /approval/{id}/approve` — approve and deliver
- `POST /approval/{id}/reject` — reject with reason

---

## Authentication

All MCP tools require an API key. Include it in every request header:

```
Authorization: Bearer psk_your_api_key_here
```

Keys are created at onboarding (POST `/onboarding`), bcrypt-hashed at rest, and shown exactly once. Rotate via `POST /account/rotate-key`.

Config-based agents (`config.yaml` → `agents:`) are a fallback for local dev when no auth header is present.

---

## Multi-agent routing

Every agent in your config gets its own identity:

```
hermes@yourdomain.com    → agent_id: hermes
mr-krabs@yourdomain.com  → agent_id: mr-krabs
```

The `send_email` tool defaults to the first configured agent. Use `from_agent` to specify which agent is sending. Rate limits are enforced per-agent — one agent hitting its limit doesn't block others.

The `check_inbox` tool always reads from the calling agent's inbox — agents cannot read each other's mail.
