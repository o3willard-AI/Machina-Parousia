# Machina Parousia

**"The Machine's Presence"** — A sovereign home-base that gives AI agents persistent presence in the world: email identity, temporal awareness, and spatial browsing.

### The problem

Agents are stateless. They have no email address, no calendar, no ability to browse the web on their own. Every integration requires a human to provision a third-party service, share API keys, and manage the infrastructure. When the human leaves, the agent's presence evaporates.

### What Parousia gives agents

Parousia is a single self-hosted server that gives every agent under your domain three co-equal capabilities — all through one MCP connection on port 8081:

| Capability | What it provides |
|-----------|-----------------|
| **📧 Email** | Every agent gets `agent@yourdomain.com`. Send and receive mail through MCP tools. Push-based ingest — no polling, no IMAP, no mailboxes. |
| **🕐 Temporal** | Agents maintain a calendar, schedule events, set timers, and keep a research journal. Token-lean DSL output so calendar context costs minimal tokens. Auto-resolves scheduling conflicts. Exports to iCal/Google/MS Graph. |
| **🌐 Spatial** | Agents browse the web through persistent per-agent Chromium profiles. Returns SDOM (Structured DOM) — compressed, element-ID-addressable, interaction-ready. Click, type, scroll, select, hover — 8 action types. |

All three share the same MCP server, the same config file, and the same deployment. Each is an independent capability — you can use email without spatial, temporal without email — but they're designed to interoperate (email triggers calendar events, browse sessions feed into journal entries).

> **Version:** v0.2.0 · **Tests:** 320 passing · **License:** MIT

---

## What you need

Parousia runs on **any Linux host with a publicly routable IP address**. You control the domain, the DNS, and the server. No cloud lock-in.

| Requirement | Details |
|-----------|---------|
| **OS** | Ubuntu 22.04+ / Debian 12+ / RHEL 9+ / Fedora 40+. Tested on Ubuntu 24.04. |
| **Python** | ≥ 3.10 |
| **Postfix** | SMTP server for inbound + outbound mail |
| **Redis** | ≥ 5.0 — rate limit counters |
| **Chromium** | For spatial browsing. Playwright can self-install it. |
| **Public IP** | Static, routable. Required for MX records and reverse DNS. |
| **Inbound ports** | 25 (SMTP), 80/443 (TLS), 8080 (REST), 8081 (MCP) |
| **Outbound ports** | 25 (direct MX delivery) or 587 (SES/smarthost relay) |
| **DNS control** | MX, A, SPF, DKIM, DMARC records at your registrar |
| **Root access** | For Postfix config, port binding, certbot |

Full provisioning walkthrough: **[docs/getting-started.md](docs/getting-started.md)**

---

## MCP Tools

Agents connect at `http://<host>:8081/sse`. All 11 tools share one MCP server:

### 📧 Email

| Tool | Description |
|------|-------------|
| `send_email` | Send email through Parousia. Rate-limited: 100/hr per agent, 500/day domain-wide. |
| `check_inbox` | Read agent inbox. Returns sender, subject, body, received time. Supports `unread_only` and `limit`. |

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

### Authentication

Parousia uses API-key authentication (bcrypt-hashed at rest). Include an `Authorization: Bearer <api_key>` header with every MCP request. Keys are issued at onboarding — see [docs/capabilities/email.md](docs/capabilities/email.md).

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
# Edit /etc/parousia/config.yaml with your domain and agents

# Run the REST + MCP servers (no Postfix needed for dev)
parousia-guard serve
```

This starts the REST API on `:8080` and MCP on `:8081`. Email tools will attempt `localhost:25` delivery — which will fail without Postfix, but the MCP server and all temporal/spatial tools work.

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
  Agent ──────────→│ Parousia Guard ──→│ Postfix :25 ──→ outbound mail
                   │   MCP :8081     │
                   │                 │
                   │  ┌──────────────┤
                   │  │  Temporal    │  SQLite: calendar, journal
                   │  │  Engine      │  DSL serializer, conflict resolver
                   │  ├──────────────┤
                   │  │  Spatial     │  Per-agent Chromium × N
                   │  │  Engine      │  SDOM serializer, browser pool
                   │  ├──────────────┤
                   │  │  Inbox       │  Per-agent SQLite inboxes
                   │  │  Store       │  Push-based, no polling
                   │  ├──────────────┤
                   │  │  Account     │  SQLite, bcrypt-hashed API keys
                   │  │  Store       │  Free/paid tiers, admin endpoints
                   │  └──────────────┤
                   │                 │
                   │  Redis :6379    │  Rate-limit token buckets
                   └─────────────────┘
```

Full component interactions and request lifecycle: **[docs/architecture.md](docs/architecture.md)**

---

## Documentation

| Doc | What it covers |
|-----|---------------|
| [Getting Started](docs/getting-started.md) | Full provisioning: host setup, DNS, Postfix, Redis, TLS, DKIM, verification |
| [Email Guide](docs/capabilities/email.md) | `send_email` + `check_inbox` with request/response examples |
| [Temporal Guide](docs/capabilities/temporal.md) | All 6 temporal tools, DSL format reference, conflict resolution rules |
| [Spatial Guide](docs/capabilities/spatial.md) | All 3 spatial tools, SDOM format reference, browser pool internals |
| [Architecture](docs/architecture.md) | Full system diagram, component contracts, life-of-an-email, life-of-an-MCP-call |
| [Hosting Notes](docs/hosting.md) | Provider-specific notes: AWS (SES, port 25), Hetzner, DigitalOcean, Linode |
| [Changelog](DONE.md) | Completed features by story |

---

## License

MIT
