# Onboarding to Machina Parousia

Parousia gives your AI agent a sovereign presence on the internet — email, calendar, browser, and semantic memory, all through a single MCP connection.

This guide covers what you and your agent need to do to get connected.

---

## Prerequisites

- A human sponsor who has agreed to vouch for your agent
- An invite code from that sponsor (a one-time key, format: `po_inv_<24 hex chars>`)
- The Parousia host URL: **`https://machinaparousia.ai`**

---

## Step-by-Step

### 1. Get an Invite Code

Ask your human sponsor to contact Stephen. Once approved, Stephen will generate an invite code for you. It looks like:

```
po_inv_4a1397bb7d0440809c1a6e71
```

Each code is **single-use** and **non-recoverable**. If you lose it, you'll need a new one.

### 2. Onboard Your Agent

Your agent (or you on its behalf) calls the public onboarding endpoint:

```bash
curl -X POST https://machinaparousia.ai/onboard \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "your-agent-name",
    "invite_code": "po_inv_4a1397bb7d0440809c1a6e71",
    "display_name": "My Agent"
  }'
```

**`account_id` rules:**
- 3–64 characters
- Lowercase letters, digits, hyphens, underscores only
- Must start with a letter or digit
- Must be unique across all Parousia agents

**Response:**

```json
{
  "account_id": "your-agent-name",
  "api_key": "po_e94f1a2b3c4d...",
  "tier": "free",
  "message": "Account created! Save your API key — it will not be shown again. Use it in the Authorization header: 'Bearer <your_key>'"
}
```

> **⚠️ Save the `api_key` immediately.** It is shown exactly once and cannot be recovered. If you lose it, you'll need a new invite code and a fresh account.

### 3. Connect via MCP

Point your agent's MCP client at the SSE endpoint:

```
http://machinaparousia.ai:8081/sse
```

Your MCP connection carries your account context automatically. Once connected, your agent gains access to **11 tools** across four capability domains.

---

## Available Tools

### 📧 Email

| Tool | What It Does |
|------|-------------|
| `send_email` | Send email from `your-agent@machinaparousia.ai` via direct MX delivery (TLS, DKIM-signed) |
| `check_inbox` | Read messages sent to your agent's inbox |

### 📅 Temporal

| Tool | What It Does |
|------|-------------|
| `get_temporal_context` | Return your calendar as a token-lean DSL (~200 tokens for a week view) |
| `schedule_event` | Create a calendar event with automatic conflict resolution |
| `cancel_event` | Soft-delete an event by ID |
| `set_timer_alarm` | Set a relative timer or absolute alarm |
| `nominate_milestone` | Record research/decision/shipped journal entries |
| `resolve_conflicts` | Batch-resolve scheduling conflicts |

### 🌐 Spatial

| Tool | What It Does |
|------|-------------|
| `browse_to` | Navigate to a URL and return a token-optimized DOM (SDOM, ~500–800 tokens) |
| `interact` | Click, type, scroll, select, check, hover, or press on page elements |
| `extract_page_state` | Extract full, changes-only, or context-only SDOM from the current page |

### 🧠 Memory

Memory is automatic and cross-domain — every tool call is recorded as a semantic fact in the background. As your agent uses email, calendar, and browser tools, Parousia builds a searchable memory layer. Facts are agent-scoped (your agent can't see another agent's memory).

---

## Limits (Free Tier)

| Resource | Limit |
|----------|-------|
| Email rate | 20/hour |
| Browser instances | 1 persistent Chromium profile |
| Storage | Tracked, not hard-capped |

---

## Managing Your Account

```bash
# View your account info
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://machinaparousia.ai:8081/account

# Rotate your API key (old key stops working immediately)
curl -X POST -H "Authorization: Bearer YOUR_API_KEY" \
  https://machinaparousia.ai:8081/account/rotate-key
```

---

## Troubleshooting

| Problem | Likely Cause |
|---------|-------------|
| `Invalid invite code` | Code was already used, revoked, or doesn't exist. Ask your sponsor for a new one. |
| `Account already exists` | Your `account_id` is taken. Pick a different name. |
| `422 validation error` | Your `account_id` doesn't match the format rules (see Step 2). |
| `401 Unauthorized` | Your API key is wrong or missing. Keys are shown once — you may need to re-onboard. |
| MCP connection refused | Check that your client is connecting to port **8081** (not 8080). |

---

## Getting Help

Contact your sponsor or Stephen for invite codes and account issues.
