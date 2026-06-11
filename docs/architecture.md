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
  Agent ──────────→│ Parousia Guard ──→ Postfix :25 ──→ outbound mail    │
                   │   MCP :8081     │   (localhost relay)               │
                   │                 │                                    │
                   │  ┌──────────────┤                                    │
                   │  │  REST API    │←───────────────────────────────────┘
                   │  │  :8080       │
                   │  │              │  /health /ingest /metrics
                   │  │              │  /dashboard /approval/*
                   │  │              │  /onboarding /account/*
                   │  ├──────────────┤
                   │  │  MCP Server  │  /sse endpoint
                   │  │  :8081       │  11 tools (email ×2, temp ×6, spatial ×3)
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

**MCP Server** (`:8081`): Stateful SSE connection per agent. Tools call into the same AccountStore, InboxStore, TemporalDB, and BrowserPool that the REST API uses. Auth middleware extracts `Authorization: Bearer`, validates against AccountStore, and injects `agent_id` into the tool context.

**Key property:** REST and MCP share the same data stores. An email ingested via REST POST is immediately available via MCP `check_inbox`. A calendar event created via MCP `schedule_event` is visible to the REST health/metrics endpoints.

### Temporal Engine ↔ Spatial Engine

**Direction:** Independent — no direct coupling

Agents can bridge them: browse a conference website (spatial), extract dates, schedule an event (temporal). The engines themselves don't call each other. Each is self-contained with its own SQLite tables, its own MCP tool handlers, its own serializer.

### AccountStore ↔ Everything

**Direction:** Read by all components on every authenticated request

Every MCP and REST request (except `/health` and `/onboarding`) goes through:
1. Extract `Authorization: Bearer *** header
2. AccountStore.lookup(key_hash) — bcrypt compare
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
   → Checks: smtpd_relay_restrictions (permit_mynetworks for local, reject_unauth_destination for external)

3. Postfix routes via pipe transport
   → master.cf: parousia  unix  -  n  n  -  -  pipe  flags=R user=parousia argv=/opt/parousia/parousia_pipe.py
   → Spawns process as user 'parousia'
   → Feeds raw MIME via stdin

4. parousia_pipe.py parses MIME
   → BytesParser(policy=policy.default).parsebytes(stdin)
   → Extracts: From header, To header, Subject, text/plain body, raw bytes
   → Determines agent_id from local part of To: hermes@yourdomain.com → "hermes"

5. Pipe POSTs to REST ingest endpoint
   → POST http://127.0.0.1:8080/ingest
   → JSON payload: {sender, recipient, subject, body, raw_mime, agent_id}
   → Timeout: 10 seconds

6. REST /ingest handler
   → Validates required fields (sender, recipient, subject, body)
   → Rate-limiter check (Redis token bucket)
   → DKIM validation (optional, if dkimpy available)
   → InboxStore.insert(message) → SQLite
   → Returns {"status": "accepted"} within ~250ms

7. Postfix sees exit code 0 → delivery complete
   → If exit ≠ 0: Postfix queues and retries (deferred queue)

8. Agent reads via MCP check_inbox
   → Authorization: Bearer *** → AccountStore → agent_id
   → InboxStore.get_messages(agent_id, unread_only=True)
   → Messages marked read
   → Returns to agent's MCP session
```

## Life of an MCP temporal call

```
1. Agent connects to :8081/sse
   → MCP SSE transport with Authorization header
   → Server accepts, session established

2. Agent calls get_temporal_context
   → MCP message: {method: "tools/call", params: {name: "get_temporal_context", arguments: {mode: "standard"}}}

3. MCP server routes
   → Auth middleware: validate API key → resolve agent_id
   → dispatch(): name matches TemporalToolHandlers
   → TemporalToolHandlers._handle_get_temporal_context(args, agent_id)

4. Temporal handler
   → TemporalDB.get_events(agent_id, status="confirmed")
   → TemporalDB.get_journal_entries(agent_id)
   → TemporalSerializer.to_dsl(agent_id, mode)
     → Now header: !NOW: YYYY-MM-DD ...
     → Past window: events with status=completed
     → Planned window: events with status=confirmed
     → Journal: milestone entries
   → TemporalSerializer.get_conflicts(agent_id)
     → Overlap detection between confirmed events
   → OpportunisticInjector: add consideration hint if conflicts exist

5. Response returns to agent
   → JSON: {context: "#TIMEBOX\n...", conflicts: [...], consideration: "..."}
   → Agent processes the DSL, plans accordingly
```

## Life of an MCP spatial call

```
1. Agent calls browse_to
   → {method: "tools/call", params: {name: "browse_to", arguments: {url: "https://..."}}}

2. MCP server routes
   → Auth → agent_id
   → dispatch(): name matches SpatialToolHandlers
   → SpatialToolHandlers._handle_browse_to(args, agent_id)

3. Browser pool
   → BrowserPoolManager.get_browser(agent_id)
   → Check: does agent have an existing browser?
     → Yes + alive: reuse, update last_used_at
     → No: launch new Chromium via Playwright
       → Create profile dir: /var/lib/parousia/browsers/{agent_id}/
       → Acquire profile lock (.lock file with PID)
       → Launch with: --no-sandbox --headless=new --disable-gpu
   → browser.new_page()
   → page.goto(url, timeout=timeout_ms)

4. SDOM extraction
   → page.content() → raw HTML
   → SpatialSerializer.to_sdom(html, extract_mode)
     → BeautifulSoup4 parse
     → Detect interactive elements: a, button, input, select, textarea, [role=]
     → Filter invisible elements (display:none, visibility:hidden, zero-size)
     → Assign sequential IDs: a1, a2, btn1, input0...
     → Classify page type: login/search_results/form/error/dashboard/article/product/generic
     → Extract content sections by heading hierarchy
     → Compress + truncate long text

5. Response returns to agent
   → SDOM JSON with meta, interactive_elements, content_sections, context
   → Agent reads SDOM, plans interactions
```

---

## Database layout

All databases are SQLite files in `/var/lib/parousia/` (configurable):

```
/var/lib/parousia/
├── temporal.db       # events + journal tables
├── accounts.db        # accounts + api_key_events tables
├── inbox.db           # inbox_messages table
└── browsers/          # per-agent Chromium profile directories
    ├── hermes/
    │   └── profile.lock
    ├── mr-krabs/
    └── ...
```

**Temporal DB schema:**
```sql
CREATE TABLE events (
    id TEXT PRIMARY KEY,           -- agent_id:short_id (e.g., "hermes:e3")
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_time TEXT NOT NULL,      -- ISO 8601
    end_time TEXT,
    flexibility TEXT DEFAULT 'high',
    status TEXT DEFAULT 'confirmed',
    event_type TEXT DEFAULT 'event',
    stakeholders TEXT,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE journal (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    entry_type TEXT DEFAULT 'milestone',
    occurred_at TEXT NOT NULL,
    tags TEXT
);
```

**Account DB schema:**
```sql
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    display_name TEXT,
    api_key_hash TEXT NOT NULL,    -- bcrypt
    tier TEXT DEFAULT 'free',
    status TEXT DEFAULT 'active',
    email TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    rate_limit_per_hour INTEGER DEFAULT 100,
    browser_max_instances INTEGER DEFAULT 1
);

CREATE TABLE api_key_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    event_type TEXT NOT NULL,       -- created | rotated | revoked
    key_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

**Inbox DB schema:**
```sql
CREATE TABLE inbox_messages (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT,
    body_text TEXT,
    body_html TEXT,
    received_at TEXT NOT NULL,
    read BOOLEAN DEFAULT 0,
    archived BOOLEAN DEFAULT 0
);
```

---

## Config file structure

```yaml
domain: yourdomain.com

server:
  rest_host: "127.0.0.1"
  rest_port: 8080
  mcp_host: "0.0.0.0"
  mcp_port: 8081

agents:
  hermes:
    rate_limit_per_hour: 100
  mr-krabs:
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
  api_key: ""   # Set via PAROUSIA_ADMIN_KEY env var

logging:
  level: info
  format: json
  output: syslog
```
