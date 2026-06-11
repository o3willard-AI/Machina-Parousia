# Architecture

How Parousia's components fit together and what happens when things happen.

---

## Full system diagram

```
                         INTERNET
                            │
                   ┌────────┴────────┐
                   │   Your Host     │
                   │                 │
  inbound mail ───→│ Postfix :25 ────→│ pipe ──→ parousia_pipe.py ──→ POST /ingest
                   │                 │                                    │
  Agent spawns ───→│ MCP subprocess │                                    │
  parousia-guard   │ (stdio)        │                                    │
  serve --mcp      │                │                                    │
                   │                │                                    │
                   │  ┌──────────────┤                                    │
                   │  │  REST API    │←───────────────────────────────────┘
                   │  │  :8080       │
                   │  │              │  /health /ingest /metrics
                   │  │              │  /dashboard /approval/*
                   │  │              │  /onboarding /account/*
                   │  ├──────────────┤
                   │  │  MCP Server  │  Stdio transport (spawned per-agent)
                   │  │  (stdio)     │  11 tools (email ×2, temp ×6, spatial ×3)
                   │  ├──────────────┤
                   │  │  Auth        │  bcrypt API key validation
                   │  │  Middleware  │  AccountStore → tier/permissions
                   │  ├──────────────┤
                   │  │  Inbox       │  SQLite per-agent message store
                   │  │  Store       │  check_inbox queries, ingest writes
                   │  ├──────────────┤
                   │  │  Temporal    │  SQLite: events + journal
                   │  │  Engine      │  DSL serializer, conflict resolver
                   │  │              │  iCal/Google/MS Graph export
                   │  ├──────────────┤
                   │  │  Spatial     │  Playwright → per-agent Chromium
                   │  │  Engine      │  SDOM serializer, browser pool
                   │  ├──────────────┤
                   │  │  Account     │  SQLite: accounts + api_key_events
                   │  │  Store       │  bcrypt hashing, tier management
                   │  ├──────────────┤
                   │  │  Rate        │  Redis token bucket
                   │  │  Limiter     │  100/hr per agent, 500/day domain
                   │  └──────────────┤
                   │                 │
                   │  Redis :6379    │  Rate-limit counters
                   └─────────────────┘
```

---

## Transport model

Parousia has two server processes, started from the same `parousia-guard serve` entry point:

| Server | How it runs | Transport | Port |
|--------|-----------|-----------|------|
| **REST** | Systemd daemon (`parousia-guard serve --rest`) | HTTP | 127.0.0.1:8080 |
| **MCP** | Per-agent subprocess (`parousia-guard serve --mcp`) | Stdio (stdin/stdout) | None |

The REST server runs continuously as a systemd service. It handles health checks, email ingest from Postfix, metrics, onboarding, and account management.

The MCP server is **spawned on demand** by each agent — the agent's host runs `parousia-guard serve --mcp` as a subprocess and communicates via stdin/stdout using the MCP JSON-RPC protocol. This means:
- No network port needed for MCP
- Each agent gets its own MCP session with its own browser profile, DB connection, and auth context
- MCP processes are short-lived (one per agent session) and exit when the agent disconnects

Both servers share the same codebase, same config file, same database files, and same Redis instance. REST and MCP read/write to the same InboxStore, TemporalDB, AccountStore, and BrowserPool.

---

## Component contracts

### Postfix ↔ Parousia Guard

**Direction:** Postfix → Guard (inbound), Guard → Postfix (outbound)

**Inbound contract:** Postfix receives SMTP on :25. For domains in `relay_domains`, it invokes the `parousia` pipe transport defined in `master.cf`. The pipe transport:
- Spawns `/opt/parousia/parousia_pipe.py`
- Feeds raw MIME via stdin
- Expects exit code 0 within the pipe timeout
- Queues and retries on non-zero exit

**Outbound contract:** Guard's `email_sender.py` opens `smtplib.SMTP('localhost', 25)`, authenticates as a trusted local client (`permit_mynetworks`), and submits outbound mail. Postfix handles DNS MX lookup, queueing, retries, and bounce generation.

**Key property:** Postfix accepts mail before Guard processes it. If Guard is down, Postfix queues and retries. No mail is lost at the MTA level.

### Guard REST API ↔ Guard MCP Server

**Direction:** Bidirectional — both read from AccountStore, InboxStore, TemporalDB

**REST API** (`:8080`): Stateless HTTP endpoints for ingest, health, metrics, dashboard, onboarding, account management, and approval queue. Called by the pipe script, admin CLI, and monitoring systems.

**MCP Server** (stdio): Stateless per-invocation. Tools call into the same AccountStore, InboxStore, TemporalDB, and BrowserPool that the REST API uses. Auth context is passed through a context variable set by the stdio transport handler.

**Key property:** REST and MCP share the same data stores. An email ingested via REST POST is immediately available via MCP `check_inbox`. A calendar event created via MCP `schedule_event` is visible to the REST health/metrics endpoints.

### Temporal Engine ↔ Spatial Engine

**Direction:** Independent — no direct coupling

Agents can bridge them: browse a conference website (spatial), extract dates, schedule an event (temporal). The engines themselves don't call each other. Each is self-contained with its own SQLite tables, its own MCP tool handlers, its own serializer.

### AccountStore ↔ Everything

**Direction:** Read by all components on every authenticated request

Every MCP and REST request (except `/health` and `/onboarding`) goes through:
1. Extract account identity from auth context (MCP: context variable set by transport; REST: `Authorization: Bearer` header)
2. AccountStore.lookup() — validate API key
3. Resolve `account_id`, `tier`, `status`
4. Inject into request context
5. All downstream operations scope by `account_id`

Config-based agents (`config.yaml` → `agents:`) are a fallback when no auth header is present. This preserves backward compatibility for local dev.

---

## Life of an inbound email

```
1. External mail server connects to your-host:25
   → SMTP handshake (EHLO, MAIL FROM, RCPT TO, DATA)

2. Postfix accepts the message
   → Checks: is recipient domain in relay_domains? Yes (yourdomain.com)

3. Postfix routes via pipe transport
   → master.cf: parousia pipe → spawns /opt/parousia/parousia_pipe.py as user 'parousia'
   → Feeds raw MIME via stdin

4. parousia_pipe.py parses MIME
   → BytesParser(policy=policy.default).parsebytes(stdin)
   → Extracts: From, To, Subject, body, raw bytes
   → Determines agent_id from local part of To address

5. Pipe POSTs to REST ingest endpoint
   → POST http://127.0.0.1:8080/ingest
   → JSON payload: {sender, recipient, subject, body, agent_id}

6. REST /ingest handler
   → Validates required fields
   → Rate-limiter check (Redis token bucket)
   → DKIM validation (optional)
   → InboxStore.insert(message) → SQLite
   → Returns {"status": "accepted"}

7. Postfix sees exit code 0 → delivery complete
   → If exit ≠ 0: Postfix queues and retries

8. Agent reads via MCP check_inbox
   → Agent spawns parousia-guard serve --mcp
   → MCP tool call: check_inbox
   → InboxStore.get_messages(agent_id, unread_only=True)
   → Messages marked read
```

## Life of an MCP temporal call

```
1. Agent spawns MCP subprocess
   → parousia-guard serve --mcp
   → MCP handshake over stdin/stdout

2. Agent calls get_temporal_context
   → MCP message via stdin: {method: "tools/call", params: {name: "get_temporal_context", arguments: {mode: "standard"}}}

3. MCP server routes
   → Auth middleware: resolve agent_id from context
   → dispatch(): name matches TemporalToolHandlers
   → TemporalToolHandlers._handle_get_temporal_context(args, agent_id)

4. Temporal handler
   → TemporalDB.get_events(agent_id, status="confirmed")
   → TemporalDB.get_journal_entries(agent_id)
   → TemporalSerializer.to_dsl(agent_id, mode)
     → Now header: !NOW: YYYY-MM-DD ...
     → Past window, Planned window, Journal sections
   → TemporalSerializer.get_conflicts(agent_id)
   → OpportunisticInjector: add consideration hint

5. Response returns via stdout
   → JSON: {context: "#TIMEBOX\n...", conflicts: [...], consideration: "..."}
   → Agent processes the DSL, plans accordingly
```

## Life of an MCP spatial call

```
1. Agent calls browse_to via MCP stdio
   → {method: "tools/call", params: {name: "browse_to", arguments: {url: "https://..."}}}

2. MCP server routes
   → Auth → agent_id
   → dispatch(): SpatialToolHandlers._handle_browse_to(args, agent_id)

3. Browser pool
   → BrowserPoolManager.get_browser(agent_id)
   → Check: does agent have an existing browser?
     → Yes + alive: reuse
     → No: launch Chromium via Playwright
       → Create profile, acquire lock
       → --no-sandbox --headless=new --disable-gpu
   → page.goto(url)

4. SDOM extraction
   → page.content() → raw HTML
   → SpatialSerializer.to_sdom(html, extract_mode)
     → BeautifulSoup4 parse → interactive elements → filter invisible
     → Assign IDs: a1, a2, btn1, input0...
     → Classify page type
     → Compress + truncate

5. Response returns via stdout
   → SDOM JSON with meta, interactive_elements, content_sections
```

---

## Database layout

All databases are SQLite files in `/var/lib/parousia/` (configurable):

```
/var/lib/parousia/
├── temporal.db       # events + journal tables
├── accounts.db        # accounts + api_key_events tables
├── inbox.db           # inbox_messages table (or data/inbox.db)
└── browsers/          # per-agent Chromium profile directories
```

---

## Config file structure

```yaml
domain: yourdomain.com

server:
  rest_host: "127.0.0.1"
  rest_port: 8080
  mcp_host: "0.0.0.0"
  mcp_port: 8081          # for reference only — MCP uses stdio transport

agents:
  hermes:
    rate_limit_per_hour: 100

redis:
  host: localhost
  port: 6379
  db: 0

rate_limits:
  per_agent_per_hour: 100
  domain_per_day: 500

postfix:
  aliases_file: /etc/aliases
  guard_script: /usr/local/bin/parousia-guard

dkim:
  key_dir: /etc/parousia/dkim
  selector: default

spatial:
  enabled: true
  chromium_path: /usr/bin/chromium-browser
  profile_dir: /var/lib/parousia/browsers
  idle_timeout_seconds: 300
  max_instances: 10

approval:
  enabled: false
  queue_ttl_hours: 72
  require_approval_for: []

account_store:
  db_path: /var/lib/parousia/accounts.db

admin:
  api_key: ""

logging:
  level: info
  format: json
  output: syslog
```
