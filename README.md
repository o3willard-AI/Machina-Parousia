# Machina Parousia

**"The Machine's Presence"** — A sovereign home-base that gives AI agents persistent presence in the world: email identity, temporal awareness, spatial browsing, and semantic memory.

### The problem

Agents are stateless. They have no email address, no calendar, no ability to browse the web on their own. Every integration requires a human to provision a third-party service, share API keys, and manage the infrastructure. When the human leaves, the agent's presence evaporates.

### What Parousia gives agents

Parousia is a single self-hosted server that gives every agent under your domain four co-equal capabilities — all through stdio MCP transport (spawned per-agent as a subprocess):

| Capability | What it provides |
|-----------|-----------------|
| **📧 Email** | Every agent gets `agent@yourdomain.com`. Send, read, triage, and wait for mail through MCP tools. Push-based ingest — no polling, no IMAP, no mailboxes. |
| **🕐 Temporal** | Agents maintain a calendar, schedule events, set timers, and keep a research journal. Token-lean DSL output so calendar context costs minimal tokens. Auto-resolves scheduling conflicts. Exports to iCal/Google/MS Graph. |
| **🌐 Spatial** | Agents browse the web through persistent per-agent Chromium profiles. Returns SDOM (Structured DOM) — compressed, element-ID-addressable, interaction-ready. Click, type, scroll, select, hover — 8 action types. Includes secure credential fills and form introspection. |
| **🧠 Memory** | Automatic, cross-domain semantic memory: every recorded tool call is stored as a natural-language fact in a local Qdrant vector DB. Searchable per-agent; no external API calls. |

All four share the same MCP server, the same config file, and the same deployment. Each is an independent capability — you can use email without spatial, temporal without email — but they're designed to interoperate (email triggers calendar events, browse sessions feed into memory).

> **Version:** v0.2.0 · **Tests:** 716 passing · **License:** MIT

Every agent account is bound at onboarding to a named human sponsor, so Parousia records a declared **Registered Accountable Entity (RAE) at L0 (declared, unverified)** with actions attributable to that human rather than only to the agent — see the [RAE specification](https://github.com/o3willard-AI/RAE) and [docs/accountability.md](docs/accountability.md).

---

## What you need

Parousia runs on **any Linux host with a publicly routable IP address**. You control the domain, the DNS, and the server. No cloud lock-in.

| Requirement | Details |
|-----------|---------|
| **OS** | Ubuntu 22.04+ / Debian 12+ / RHEL 9+ / Fedora 40+. Tested on Ubuntu 24.04. |
| **Python** | ≥ 3.10 |
| **Postfix** | SMTP server for inbound + outbound mail |
| **Redis** | ≥ 5.0 — rate limit counters |
| **Qdrant** | Vector DB for the memory layer |
| **Chromium** | For spatial browsing. Playwright can self-install it. |
| **Public IP** | Static, routable. Required for MX records and reverse DNS. |
| **Inbound ports** | 25 (SMTP), 80/443 (TLS), 8080 (REST), 8081 (MCP) |
| **Outbound ports** | 25 (direct MX delivery) or 587 (SES/smarthost relay) |
| **DNS control** | MX, A, SPF, DKIM, DMARC records at your registrar |
| **Root access** | For Postfix config, port binding, certbot |

Full provisioning walkthrough: **[docs/getting-started.md](docs/getting-started.md)**

---

## MCP Tools

Agents connect to Parousia via MCP stdio transport — each agent spawns `parousia-guard serve --mcp` as a subprocess. All **25 tools** are available through a single MCP session, including the memory read tools (`mem0_search` / `mem0_profile` — recall your own recorded facts; recording itself still happens in the background).

### 📧 Email

| Tool | Description |
|------|-------------|
| `send_email` | Send email through Parousia. Rate-limited: 20/hr per account (enforced via the account's `rate_limit_per_hour`, default 20; the config `per_agent_per_hour` 100 is only the fallback when no account row exists), 500/day domain-wide. |
| `check_inbox` | List your inbox. Returns sender, subject, `body_preview` (first 200 chars), received time, read/archived flags. Supports `unread_only`, `include_archived` (default `false` — archived mail is excluded), and `limit` (default 10). |
| `read_message` | Read one full message by ID (returns the complete body). Does **not** mark it read — reading is side-effect-free; use `mark_read` to change read state. |
| `wait_for_email` | Poll your inbox for a message matching a subject/sender substring (within a timeout). Returns the message plus its body links — e.g. to `browse_to` a confirmation URL (double opt-in). |
| `mark_read` | Mark one of your messages read by ID (or bulk via `message_ids`). |
| `archive` | Archive one of your messages by ID (or bulk via `message_ids`). |
| `delete` | Delete one of your messages by ID. |

Triage tools are **account-scoped**: they only ever touch messages stored for the authenticated agent — another agent's message ID is refused.

### 🕐 Temporal

| Tool | Description |
|------|-------------|
| `get_temporal_context` | Return calendar in token-lean DSL. 4 modes: `standard` (past 24h + next 3d), `planning` (next 14d), `retrospective` (past 7d), `full` (30d). Includes conflict detection and consideration hints. |
| `schedule_event` | Create calendar event. Auto-resolves time conflicts by default. Supports flexibility levels (`high`/`low`/`none`). Returns iCal, Google Calendar, and MS Graph export payloads. |
| `cancel_event` | Soft-delete an event by short ID (e.g., `e3`). |
| `set_timer_alarm` | Set a countdown timer or absolute alarm. |
| `nominate_milestone` | Record research, decision, or shipped milestones in the temporal journal. |
| `resolve_conflicts` | Batch-detect and auto-resolve scheduling overlaps. Never moves `flexibility=none` events. |

### 🌐 Spatial

| Tool | Description |
|------|-------------|
| `browse_to` | Navigate to a URL and return SDOM. 3 extraction modes: `standard`, `content_only`, `interactive_only`. |
| `interact` | Perform actions on SDOM elements by ID. 8 actions: `click`, `type`, `scroll_into_view`, `select`, `check`, `uncheck`, `hover`, `press`. |
| `extract_page_state` | Extract current page as SDOM. 3 modes: `full`, `changes`, `context_only`. |
| `fill_credentials` | Fill a stored credential into a login form on the current origin (encrypted-at-rest vault; origin-bound; the agent never sees the secret). |
| `dump_form` | Introspect a form's controls — per-field type/label/options, with captcha detection. Password values are never returned. |
| `check_egress_ip` | Read-only verification of the agent-called transport: returns the actual egress IP (`{ip, proxy}` — transport by name) via an IP-echo fetch. |

Session cookies persist across calls (persistent per-agent Chromium profile). On a failed/refused `browse_to`/`interact`, the result carries **failure forensics** — a `block_vendor` classification (CloudFront/Cloudflare/Akamai), a screenshot path, and a DOM snapshot — so agents can tell "the tool is broken" from "the site is refusing us".

### 🔐 TOTP / 2FA

| Tool | Description |
|------|-------------|
| `enroll_totp` | Enroll a TOTP authenticator (RFC 6238) from the `otpauth://` URI a service emits when enabling app-based 2FA. The secret is stored server-side, encrypted; returns the enrollment summary (handle/issuer/account/algorithm/digits/period) — never the secret. |
| `get_totp_code` | Compute the current TOTP code for an enrolled handle. Returns `{code, valid_seconds}` — never the secret. |
| `list_totp` | List your enrolled TOTP authenticators (handles + metadata; never secrets). |
| `remove_totp` | Delete one of your enrolled authenticators by handle (another agent's handle is refused). |

Secrets are Fernet-encrypted at rest and account-scoped — an agent can only enroll/read/remove its own. 2FA codes and metadata are never written to the memory layer.

### 🧠 Memory (automatic)

Every recorded tool call is written in the background as a natural-language fact to a local Qdrant vector DB (fastembed, CPU-only — no external API). Facts are **agent-scoped**: `parousia-{agent_id}`. Read your own facts back with `mem0_search` (semantic recall) and `mem0_profile` (the full "what have I done" dump) — account-scoped by construction: the mem0 user_id is derived from the authenticated account, never from tool arguments, so an agent sees only its own memory. Memory reads are never themselves recorded. Credential fills are never recorded.

### Authentication

Parousia uses API-key authentication (bcrypt-hashed at rest in `accounts.db`). Include an `Authorization: Bearer <api_key>` header with every MCP request. Keys are issued at onboarding — see [ONBOARDING.md](ONBOARDING.md). `accounts.db` is the **single source of truth** for agent identities (the legacy `config.yaml` `agents:` block has been removed).

---

## Quick dev start

For local development without Postfix or DNS:

```bash
git clone https://github.com/o3willard-AI/Machina-Parousia.git
cd Machina-Parousia
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Generate a default config
parousia-guard setup --config
# Edit /etc/parousia/config.yaml with your domain

# Run the REST + MCP servers (no Postfix needed for dev)
parousia-guard serve
```

This starts the REST API on 127.0.0.1:8080. For MCP tools, agents spawn `parousia-guard serve --mcp` as a subprocess (stdio transport).

For a full production deployment with working email, see **[docs/getting-started.md](docs/getting-started.md)**.

---

## Architecture

```
                         INTERNET
                            │
                   ┌────────┴────────┐
                   │   Your Host     │
                   │                 │
  inbound mail ───→│ Postfix :25 ────→│ pipe ──→│  Parousia Guard  │
                   │                 │         │    REST :8080     │
                   │                 │         │                   │
  Agent spawns ───→│ parousia-guard  │  MCP stdio subprocess      │
  serve --mcp      │                 │  (per-agent session)       │
                   │                 │                             │
                   │  ┌──────────────┤
                   │  │  Temporal    │  SQLite: calendar, journal
                   │  │  Engine      │  DSL serializer, conflict resolver
                   │  ├──────────────┤
                   │  │  Spatial     │  Per-agent Chromium x N
                   │  │  Engine      │  SDOM serializer, browser pool,
                   │  │              │  credential store (encrypted)
                   │  ├──────────────┤
                   │  │  Inbox       │  Per-agent SQLite inboxes
                   │  │  Store       │  Push-based, no polling
                   │  ├──────────────┤
                   │  │  Memory      │  Mem0 recorder → Qdrant :6333
                   │  │  Engine      │  fire-and-forget, circuit breaker
                   │  ├──────────────┤
                   │  │  Account     │  SQLite (accounts.db), bcrypt keys
                   │  │  Store       │  Single agent registry
                   │  └──────────────┤
                   │                 │
                   │  Redis :6379    │  Rate-limit token buckets
                   │  Qdrant :6333   │  Vector memory
                   └─────────────────┘
```

Full component interactions and request lifecycle: **[docs/architecture.md](docs/architecture.md)**

---

## Documentation

| Doc | What it covers |
|-----|---------------|
| [Getting Started](docs/getting-started.md) | Full provisioning: host setup, DNS, Postfix, Redis, Qdrant, TLS, DKIM, verification |
| [Onboarding](ONBOARDING.md) | How an agent (or its sponsor) creates an account and connects via MCP |
| [Email Guide](docs/capabilities/email.md) | All 7 email tools with request/response examples |
| [Temporal Guide](docs/capabilities/temporal.md) | All 6 temporal tools, DSL format reference, conflict resolution rules |
| [Spatial Guide](docs/capabilities/spatial.md) | All 6 spatial tools, SDOM format reference, credential store, browser pool internals |
| [Memory Guide](docs/capabilities/memory.md) | The automatic memory layer, fact formatters, circuit breaker, querying |
| [TOTP Guide](docs/capabilities/totp.md) | The 4 TOTP/2FA tools, encrypted secret vault, account scoping |
| [Architecture](docs/architecture.md) | Full system diagram, component contracts, life-of-an-email, life-of-an-MCP-call |
| [Accountability (RAE)](docs/accountability.md) | The human sponsor behind every agent action; L0 (declared) assurance model and claim boundary |
| [Hosting Notes](docs/hosting.md) | Provider-specific notes: AWS (SES, port 25), Hetzner, DigitalOcean, Linode |
| [Changelog](DONE.md) | Completed features by story |

---

## License

MIT
