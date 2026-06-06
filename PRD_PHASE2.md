# Parousia — Phase 2 PRD: Temporal Presence

> **Goal**: Give AI agents a sovereign, persistent "presence in time" — a calendar-aware
> temporal architecture that moves them from stateless executors to entities with an
> anchored chronological landscape. Built as a co-equal capability alongside email,
> sharing the same MCP server, config, and deployment footprint.

**Version**: 1.0 — Phase 2  
**Builds on**: Phase 1 (email: Postfix pipe → guard → agent webhook + MCP send_email)  
**Target**: Same AWS EC2 Ubuntu 24.04 host, Python 3.12+, SQLite (default) / PostgreSQL (optional)  

---

## Architecture Overview

```
                          INTERNET
                             │
                    ┌────────┴────────┐
                    │   AWS EC2       │
                    │   Ubuntu 24.04  │
                    │                 │
   inbound mail ───→│ Postfix :25 ────→│ pipe ──→│ parousia-guard │──→ Agent webhook
                    │                 │         │   REST :8080    │    (Clubhouse)
                    │                 │         │                 │
   Agent ──────────→│ parousia-guard ──→│ Postfix :25 ──→ outbound mail
                    │   MCP  :8081    │
                    │                 │
                    │                 │  ═══════════ Phase 2 additions ═══════════
                    │                 │
                    │   ┌─────────────┤
                    │   │  Temporal   │
   .ics invite ────→│   │  Ingest     │──→ parse → store → temporal DB
   (email attach)   │   │  Pipeline   │         (SQLite / PostgreSQL)
                    │   └─────────────┤
                    │                 │
   Agent ──────────→│ MCP temporal    │  New tools on :8081:
                    │ tools           │  · get_temporal_context
                    │                 │  · schedule_event
                    │                 │  · cancel_event
                    │                 │  · set_timer_alarm
                    │                 │  · nominate_milestone
                    │                 │
                    │   ┌─────────────┤
                    │   │ Translation │  Outbound formats:
                    │   │   Layer     │  · .ics (iCalendar)
                    │   │             │  · Google Calendar API JSON
                    │   │             │  · MS Graph API JSON
                    │   └─────────────┤
                    │                 │
                    │   ┌─────────────┤
                    │   │ Opportunist │  Lightweight keyword classifier
                    │   │   Injector  │  + consideration hints on tool returns
                    │   └─────────────┤
                    │                 │
                    │   ┌─────────────┐
                    │   │  Temporal   │  Agent bio / research milestones
                    │   │   Journal   │  Monthly nomination pulse (cron)
                    │   └─────────────┘
                    └─────────────────┘
```

### Component Map

| Component | Role | Protocol | Storage |
|-----------|------|----------|---------|
| Postfix (Phase 1) | MTA — accepts inbound, sends outbound | SMTP | — |
| parousia-guard REST (Phase 1) | Inbound: Postfix pipe → parse → route to agent | HTTP | — |
| parousia-guard MCP (Phase 1 + 2) | Outbound email + temporal tools | MCP (JSON-RPC) | Redis (rate limits) |
| **Temporal DSL Serializer** | DB → token-lean text for agent context | — | — |
| **Temporal Ingest Pipeline** | `.ics` parser, structured JSON, optional LLM slot | — | SQLite/PostgreSQL |
| **Translation Layer** | `.ics`, Google Calendar, MS Graph export | — | — |
| **Opportunistic Injector** | Keyword classifier + consideration hints | — | — |
| **Temporal Journal** | Agent bio / research milestone storage | — | SQLite/PostgreSQL |
| **Monthly Pulse Cron** | Parousia-side trigger: "nominate milestones" | — | — |

---

## Phase 2 Scope

### In Scope

1. **Temporal DSL** — Token-lean text format for LLM context windows
2. **Hybrid Storage** — SQLite (default) or PostgreSQL (install-time option), agent-scoped with `agent_id` FK on all tables
3. **MCP Temporal Tools** — `get_temporal_context`, `schedule_event`, `cancel_event`, `set_timer_alarm` on port 8081
4. **Translation Layer** — Inbound: `.ics` parser + structured JSON + optional cheap LLM slot. Outbound: `.ics`, Google Calendar API JSON, MS Graph API JSON
5. **Sliding Window** — Standard / Planning / Retrospective modes with dynamic time horizons
6. **Opportunistic Temporal Injection** — Lightweight keyword classifier gates auto-injection; consideration hints on select MCP/CLI returns
7. **Email ↔ Calendar Bridge** — Auto-detect `.ics` attachments in incoming email → route to temporal ingest
8. **Temporal Journal** — Separate agent bio / milestone table for non-calendar temporal annotations
9. **Monthly Nomination Pulse** — Parousia-side cron that prompts the agent: "Nominate any research or action milestones from the past month"
10. **CLI Extensions** — `parousia-guard temporal setup`, `validate`, `status`, `export`
11. **Test Suite** — Unit + integration tests for all temporal subsystems

### Out of Scope (Phase 3+)

- Human interjection / circuit breaker guardrail (deferred per Doc 2 resolution)
- All Phase 1 carry-forward items (Hostinger DNS, postfwd, dashboard, TLS, DKIM validation, multi-agent routing)
- Cross-agent calendar sharing / multi-agent scheduling queries
- Medical/Emergency domain modes
- PENDING_APPROVAL state machine for high-stakes environments

---

## User Stories

### US-10: Temporal Database Schema & Storage Layer

**As a** platform engineer  
**I want** a relational schema for events, alarms, and a temporal journal  
**So that** temporal data is durable, queryable, and agent-scoped  

**Schema:**

```sql
-- Calendar events (scheduled, ingested, or nominated)
CREATE TABLE temporal_events (
    id            TEXT PRIMARY KEY,           -- short ID: "e3", "e7"
    agent_id      TEXT NOT NULL,              -- FK to config.agents keys
    title         TEXT NOT NULL,
    start_time    TIMESTAMP NOT NULL,
    end_time      TIMESTAMP,
    flexibility   TEXT DEFAULT 'high',        -- 'high', 'low', 'none'
    event_type    TEXT DEFAULT 'event',       -- 'event', 'timer', 'alarm', 'milestone'
    status        TEXT DEFAULT 'confirmed',   -- 'confirmed', 'cancelled', 'completed'
    source        TEXT DEFAULT 'manual',      -- 'manual', 'ics_import', 'email_parse', 'nl_parse', 'nomination'
    stakeholders  TEXT,                        -- comma-separated or JSON array
    metadata      TEXT,                        -- JSON blob for extensibility
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Temporal journal (agent bio / research milestones — separate entity)
CREATE TABLE temporal_journal (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT,                        -- free-text summary
    entry_type    TEXT DEFAULT 'milestone',    -- 'milestone', 'research', 'decision', 'shipped'
    occurred_at   TIMESTAMP NOT NULL,          -- when it happened
    tags          TEXT,                         -- comma-separated
    metadata      TEXT,                         -- JSON blob
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_events_agent_time ON temporal_events(agent_id, start_time);
CREATE INDEX idx_journal_agent_time ON temporal_journal(agent_id, occurred_at);
```

**Acceptance Criteria:**
- SQLite by default at `/var/lib/parousia/temporal.db`
- PostgreSQL connection string accepted at install time (`parousia-guard temporal setup --pg "postgresql://..."`)
- All tables have `agent_id` FK — no cross-agent queries possible without explicit join
- `parousia-guard temporal validate` checks DB connectivity and schema
- Migration support: `parousia-guard temporal setup` auto-creates tables if missing

**Technical Notes:**
- Use `sqlite3` from stdlib for SQLite path. Add `psycopg2` as optional dependency for PostgreSQL.
- Event IDs use the short token-lean format (`e1`, `e2`, ...) — generated sequentially per agent.
- The `metadata` JSON blob holds format-specific fields (iCalendar UID, Google event ID, MS Graph ID) for idempotent import and cross-format linking.

---

### US-11: Temporal DSL Serializer

**As an** AI agent  
**I want** my temporal context represented in a dense, token-lean text format  
**So that** I can understand my timeline without consuming thousands of tokens on JSON  

**Format specification:**

```
!NOW: 2026-06-14 W24 Sun 14:30 | DOMAIN: GENERAL_CORP
#PAST_WINDOW (3d)
- 06-11 09:00|10:00 [id:e1] Kickoff w/ Sarah *DONE
- 06-12 14:00|15:00 [id:e2] Review PR #402 *DONE
#PLANNED_WINDOW (7d)
- 06-15 10:00|11:00 [id:e3] [F:high] Sync w/ Product
- 06-18 13:00|14:30 [id:e4] [F:low] Deep Work: Architecture
#TIMERS_ALARMS
- T: 45m remaining [id:t1] Refactor script
- A: 06-16 08:00 [id:a1] Ping Dev team
#JOURNAL (recent)
- 06-10 [id:j1] Shipped Parousia Phase 1 — email pipeline live
- 06-13 [id:j2] Research: AWS SES vs Postfix deliverability
```

**Acceptance Criteria:**
- `TemporalSerializer.to_dsl(agent_id, mode)` queries the DB and produces the DSL string
- Modes: `standard` (past 24h + next 3d), `planning` (next 14d), `retrospective` (past 7d), `full` (past 30d + next 30d)
- Journal entries included in `standard` and `retrospective` modes (last 5 entries)
- Empty sections omitted entirely (no `#TIMERS_ALARMS` header if none exist)
- Completed events (>1 day past `end_time`) are excluded from all modes except `retrospective` and `full`
- Flexibility `[F:high/low/none]` appended only for future events in `#PLANNED_WINDOW`
- Token count measured: target <200 tokens for `standard` mode with typical agent load (~15 events)

**Technical Notes:**
- The serializer is a pure function: `dsl_string = serialize(rows, mode, now)` — no side effects, easily testable.
- Date format: `MM-DD` for current year, `YYYY-MM-DD` for different years. Time: `HH:MM` (24h).
- Week number (`W24`) and weekday (`Sun`) in `!NOW` header provide quick temporal orientation.

---

### US-12: MCP Temporal Tools

**As an** AI agent (Hermes, Claude, OpenClaw)  
**I want** MCP tools to read my temporal context, schedule events, and manage timers  
**So that** I can integrate temporal awareness into my reasoning without raw DB access  

**Tools added to existing MCP server (port 8081):**

#### `get_temporal_context`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `mode` | string | no | `standard` (default), `planning`, `retrospective`, `full` |
| `agent_id` | string | no | Agent ID (auto-detected from session) |

Response:
```json
{
  "context": "!NOW: 2026-06-14 W24 Sun 14:30 ...\n#PAST_WINDOW (3d)\n...",
  "mode": "standard",
  "event_count": 8,
  "conflicts": [],
  "consideration": "Your next task involves scheduling. Loading temporal context may help identify open slots."
}
```

The `consideration` field is the "opportunistic hint" — present only when the classifier or context suggests the agent would benefit from temporal awareness.

#### `schedule_event`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | Event title |
| `start_time` | string | yes | ISO 8601 datetime |
| `end_time` | string | no | ISO 8601 datetime (default: start + 1h) |
| `flexibility` | string | no | `high`, `low`, `none` (default: `high`) |
| `stakeholders` | string | no | Comma-separated list |
| `metadata` | object | no | Arbitrary key-value pairs |

Response:
```json
{
  "scheduled": true,
  "event_id": "e5",
  "conflicts": [],
  "export_formats": {
    "ics": "BEGIN:VCALENDAR\n...",
    "google_calendar": { ... },
    "ms_graph": { ... }
  }
}
```

#### `cancel_event`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_id` | string | yes | Short ID (e.g., `e3`) |

Response:
```json
{
  "cancelled": true,
  "event_id": "e3",
  "title": "Sync w/ Product"
}
```

#### `set_timer_alarm`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | What the timer/alarm is for |
| `duration_minutes` | integer | no | Relative timer (mutually exclusive with `trigger_at`) |
| `trigger_at` | string | no | Absolute alarm time (ISO 8601) |

Response:
```json
{
  "set": true,
  "alarm_id": "t2",
  "type": "timer",
  "remaining_seconds": 2700
}
```

#### `nominate_milestone`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | Milestone title |
| `description` | string | no | Free-text summary |
| `entry_type` | string | no | `milestone`, `research`, `decision`, `shipped` |
| `occurred_at` | string | yes | When it happened (ISO 8601 date or datetime) |
| `tags` | string | no | Comma-separated |

Response:
```json
{
  "recorded": true,
  "journal_id": "j5",
  "title": "Shipped Parousia Phase 1"
}
```

**Acceptance Criteria:**
- All 5 tools registered on existing MCP server alongside `send_email`
- `get_temporal_context` returns the token-lean DSL string
- `schedule_event` detects conflicts and returns them (but does NOT auto-resolve — that's Phase 3)
- `cancel_event` soft-deletes (sets `status='cancelled'`, does not delete rows)
- `set_timer_alarm` accepts EITHER `duration_minutes` OR `trigger_at`, rejects both/neither
- `nominate_milestone` writes to `temporal_journal` table (separate from calendar events)
- All tools are agent-scoped — `agent_id` from MCP session metadata or config
- Rate limiting is NOT applied to temporal tools (unlike `send_email`)

**Technical Notes:**
- Tools are registered on the same `Server("parousia-guard-mcp")` instance as `send_email`. The `handle_list_tools` handler returns the union of email + temporal tools.
- `agent_id` resolution: Phase 1 hardcoded to first config agent. Phase 2 should check for `X-Agent-ID` header in HTTP transport or use the named agent from MCP session. Fallback to first config agent.
- The `consideration` field on `get_temporal_context` is generated by the Opportunistic Injector (US-15). When the injector is not active, the field is omitted.
- Conflict detection uses simple time-range overlap (start1 < end2 AND start2 < end1). No AI-driven resolution in Phase 2.

---

### US-13: Temporal Ingest Pipeline

**As a** system operator  
**I want** Parousia to ingest calendar data from `.ics` files, structured JSON, and unstructured text  
**So that** agents accumulate temporal context from the outside world  

**Ingest paths:**

```
┌─────────────────┐
│ Email Pipeline  │──→ .ics attachment detected ──→ auto-route to temporal ingest
│ (Phase 1)       │
└─────────────────┘

┌─────────────────┐
│ .ics File/URL   │──→ parse → extract VEVENT → store in temporal_events
└─────────────────┘

┌─────────────────┐
│ Structured JSON │──→ validate schema → store
│ (API/Webhook)   │
└─────────────────┘

┌─────────────────┐
│ Unstructured    │──→ optional cheap LLM → structured params → store
│ Text (email/NL) │
└─────────────────┘
```

**Acceptance Criteria:**

*Email Bridge:*
- Phase 1 ingest pipeline detects `Content-Type: text/calendar` or `.ics` file attachments
- `.ics` content extracted and routed to temporal ingest automatically
- Agent receives a combined payload: email content + parsed calendar event
- If `.ics` parsing fails, the attachment is still delivered to the agent (graceful degradation)

*.ics Parser:*
- Parses `VEVENT`, `VTODO`, `VALARM` components
- Handles `RRULE` (recurrence) — stores as single event with recurrence metadata in `metadata` JSON
- Handles `ATTENDEE`, `ORGANIZER` — mapped to `stakeholders`
- `UID` stored in `metadata` for idempotent re-import
- Timezone handling: converts to UTC for storage, preserves original timezone in metadata
- `parousia-guard temporal ingest --ics path/to/invite.ics`

*Structured JSON Ingest:*
- POST endpoint at `localhost:8080/temporal/ingest` (REST server expansion)
- Schema matches `schedule_event` tool parameters
- Returns `event_id` on success
- Idempotent: duplicate `metadata.uid` → 200 with existing `event_id`, no duplicate created

*Unstructured Text Ingest:*
- Config key: `temporal.llm_parse_endpoint` — URL of a cheap LLM service
- When set: POST raw text → receive structured `{title, start_time, end_time, flexibility}`
- When unset (default): unstructured text passed through to agent as-is with a hint
- LLM call is fire-and-forget with 5s timeout — failure falls through to passthrough

**Technical Notes:**
- `.ics` parsing uses Python's `icalendar` library (add to `pyproject.toml` dependencies).
- Recurrence: `dateutil.rrule` for computing instances. Store the RRULE string in metadata; expansion happens at query time via the serializer.
- The email bridge is an extension of Phase 1's `ingest.py` — after MIME parsing, check for calendar parts before constructing the agent webhook payload.
- The structured JSON ingest endpoint lives on the existing REST server (port 8080). It's separate from the agent webhook path.

---

### US-14: Translation Layer (Outbound)

**As a** system operator  
**I want** Parousia to export calendar data to standard human formats  
**So that** agents can interoperate with Google Calendar, Outlook, and Apple Calendar  

**Three export formats:**

1. **iCalendar (`.ics`)** — universal standard
2. **Google Calendar API JSON** — for direct Google Calendar mutation
3. **Microsoft Graph API JSON** — for Outlook/Office 365 enterprise calendars

**Acceptance Criteria:**

*`.ics` Export:*
- `parousia-guard temporal export --format ics --agent-id hermes`
- Generates valid RFC 5545 iCalendar file
- Includes all non-cancelled events in the specified time range (default: next 30 days)
- `VEVENT` with `DTSTART`, `DTEND`, `SUMMARY`, `DESCRIPTION`, `UID`
- Output to stdout or file (`--output /path/to/calendar.ics`)

*Google Calendar API Payload:*
- `parousia-guard temporal export --format google --event-id e5`
- Returns the JSON body for `events.insert()` Google Calendar API v3 call
- Includes `summary`, `start`, `end`, `description`, `attendees`
- Does NOT call the API — emits the payload for the agent to use

*MS Graph API Payload:*
- `parousia-guard temporal export --format msgraph --event-id e5`
- Returns the JSON body for `POST /me/events` Microsoft Graph API call
- Includes `subject`, `start`, `end`, `body`, `attendees`
- Does NOT call the API — emits the payload for the agent to use

*Integration with MCP:*
- `schedule_event` response includes all three format payloads in `export_formats`
- `get_temporal_context` response includes an `export_formats` section when mode is `planning` with an `--export` flag implicitly set

**Technical Notes:**
- `.ics` generation uses `icalendar` library (same dependency as parser).
- Google and MS Graph payloads are pure dict construction — no external API calls.
- The agent (not Parousia) is responsible for actually POSTing to Google/MS APIs with its own OAuth tokens. Parousia only produces the correct payload shape.
- Format-specific IDs (Google `id`, MS Graph `id`) are stored in the `metadata` JSON blob of `temporal_events` for future sync operations.

---

### US-15: Opportunistic Temporal Injection

**As an** AI agent framework  
**I want** temporal context to be injected when relevant, not on every pass  
**So that** I get temporal awareness without wasting context window tokens  

**Two mechanisms:**

#### 1. Lightweight Keyword Classifier

A pre-pass filter that checks the agent's pending input against temporal keywords:

```python
TEMPORAL_KEYWORDS = [
    "schedule", "calendar", "meeting", "appointment", "event",
    "next week", "tomorrow", "yesterday", "remind", "deadline",
    "due date", "upcoming", "this month", "timeline", "agenda",
    "invite", "rsvp", "when are you", "when is", "what time",
]
```

- If ≥1 keyword matches: inject `get_temporal_context("standard")` into the agent's next context
- If 0 matches: skip injection (save tokens)
- Keyword list is configurable via `temporal.injection_keywords` in `config.yaml`
- Classification runs synchronously, sub-millisecond — no overhead

#### 2. Consideration Hints on MCP Returns

Select MCP tool responses carry an optional `consideration` field:

| Tool | When hint appears | Hint example |
|------|-------------------|--------------|
| `send_email` | Email body contains temporal keyword | "This email mentions a deadline. Your temporal context may be relevant." |
| `get_temporal_context` | Always (this IS the temporal tool) | "You have 3 upcoming events in the next 24 hours." |
| `schedule_event` | On success with no conflicts | "Event scheduled. You have 4 remaining slots this week." |
| Nomination pulse (cron) | Always | "It's been 30 days since your last temporal milestone review." |

**Acceptance Criteria:**
- Keyword classifier is packaged as a pure function: `def temporal_relevant(text: str) -> bool`
- Configurable keyword list in `config.yaml` under `temporal.injection_keywords`
- Consideration hints are appended to MCP tool responses as a top-level `consideration` string field
- Hints never exceed 120 characters — designed to be scanned, not read
- Both mechanisms can be disabled via config: `temporal.opportunistic: false`
- Agent framework (Hermes/Claude) can call `get_temporal_context` explicitly even when classifier says no — the classifier is advisory, not blocking

**Technical Notes:**
- The classifier lives in `src/parousia/temporal/injector.py`.
- Integration point: the agent framework (or a middleware shim) calls `temporal_relevant()` on the pending user message before the LLM pass. Parousia provides the function; the framework decides when to call it.
- Consideration hints are computed at MCP tool response time. The `consideration` field is always optional — deterministic/success-only tools (like `cancel_event`) don't include it.

---

### US-16: Temporal Journal & Monthly Nomination Pulse

**As an** AI agent  
**I want** a temporal journal separate from my calendar  
**So that** I can remember what I researched, decided, or shipped — building a persistent agent "bio"  

**Schema:** (see US-10 — `temporal_journal` table)

**The Monthly Nomination Pulse:**

On a cron schedule (default: first of each month at 09:00 UTC), Parousia sends the agent a structured prompt:

```
[NOMINATION PULSE]
It has been {days_since_last_nomination} days since your last temporal milestone review.

Your recent activity spans {event_count} calendar events. 
Your journal currently holds {journal_count} entries.

Consider: did you complete significant research, ship something notable, make 
an important decision, or reach a milestone in the past month? If so, use 
nominate_milestone to record it in your temporal journal.
```

**Acceptance Criteria:**

- `parousia-guard temporal pulse --agent-id hermes` sends the nomination prompt to the agent's webhook
- Cron install: `parousia-guard temporal setup --pulse` creates a cron job (or systemd timer) that fires the pulse
- First pulse fires immediately on setup ("initialization"), then monthly thereafter
- Pulse includes summary stats: days since last nomination, current event count, current journal count
- Agent can also call `nominate_milestone` at any time via MCP (not just during pulse)
- Journal entries are immutable — no update/delete via MCP (CLI admin can modify DB directly)
- Journal entries appear in the DSL under `#JOURNAL (recent)` section (last 5 entries)

**Technical Notes:**
- The pulse is a Parousia-side cron (systemd timer or `cronjob` via Hermes). It POSTs to the agent webhook.
- The prompt is templated — `parousia-guard temporal pulse --dry-run` prints the prompt without sending.
- Journal entries can reference calendar events via `metadata.event_id` but are stored separately.

---

### US-17: CLI Extensions

**As a** DevOps engineer  
**I want** CLI commands to set up, validate, and manage the temporal subsystem  
**So that** I can administer temporal presence alongside email from the same tool  

**New CLI commands:**

```bash
# Setup
parousia-guard temporal setup              # Initialize DB, create tables, configure pulse
parousia-guard temporal setup --pg "..."   # Use PostgreSQL instead of SQLite
parousia-guard temporal setup --pulse      # Install monthly nomination cron

# Validation
parousia-guard temporal validate           # Check DB connectivity, schema, serializer

# Status
parousia-guard temporal status             # Show event counts, DB size, last pulse, journal stats

# Export
parousia-guard temporal export --format ics --agent-id hermes
parousia-guard temporal export --format google --event-id e5
parousia-guard temporal export --format msgraph --event-id e5

# Ingest
parousia-guard temporal ingest --ics path/to/invite.ics --agent-id hermes
parousia-guard temporal ingest --json '{"title":"...","start_time":"..."}' --agent-id hermes

# Pulse
parousia-guard temporal pulse --agent-id hermes
parousia-guard temporal pulse --agent-id hermes --dry-run

# DB management
parousia-guard temporal db --stats         # Table sizes, row counts
parousia-guard temporal db --vacuum        # SQLite VACUUM
```

**Acceptance Criteria:**
- All commands use existing Click CLI group (`parousia-guard temporal ...`)
- `validate` exits 0 on healthy system, non-zero with diagnostics on failure
- `status` shows event counts, DB path, agent IDs, last pulse timestamp
- `setup` is idempotent — running twice does not destroy data
- All commands respect `--config` flag for custom config path
- DB path defaults to `/var/lib/parousia/temporal.db` (configurable in `config.yaml`)

---

### US-18: Integration Test Suite (Temporal)

**As a** developer  
**I want** comprehensive tests covering all temporal subsystems  
**So that** I can refactor with confidence  

**Test Cases:**

| Test | What it verifies |
|------|-----------------|
| `test_serializer_standard_mode` | DSL output for standard mode matches spec |
| `test_serializer_empty_agent` | Agent with no events → minimal DSL (header only) |
| `test_serializer_token_count` | Standard mode <200 tokens for 15 events |
| `test_serializer_planning_mode` | Planning mode returns 14-day forward window |
| `test_serializer_retrospective_mode` | Retrospective returns past 7 days completed |
| `test_serializer_journal_entries` | Journal entries appear in DSL under #JOURNAL |
| `test_db_schema_creates_tables` | Schema creation succeeds, indexes exist |
| `test_db_agent_isolation` | Agent A cannot see Agent B's events |
| `test_mcp_get_temporal_context` | Tool returns DSL string with correct mode |
| `test_mcp_schedule_event` | Event stored, event_id returned, export formats included |
| `test_mcp_schedule_event_conflict` | Overlapping event → conflict returned |
| `test_mcp_cancel_event` | Event status → 'cancelled', still in DB |
| `test_mcp_set_timer` | Timer stored with type='timer', duration set |
| `test_mcp_set_alarm` | Alarm stored with type='alarm', trigger_at set |
| `test_mcp_nominate_milestone` | Journal entry created, returned in get_temporal_context |
| `test_ics_parser_vevent` | Standard .ics → correct event rows |
| `test_ics_parser_recurrence` | RRULE stored in metadata, expansion testable |
| `test_ics_parser_timezone` | DTSTART with TZID → converted to UTC |
| `test_email_bridge_ics_attach` | Email with .ics attachment → temporal ingest triggered |
| `test_email_bridge_broken_ics` | Invalid .ics → attachment still delivered to agent |
| `test_export_ics` | Generated .ics validates with icalendar parser |
| `test_export_google` | Google Calendar JSON matches API schema |
| `test_export_msgraph` | MS Graph JSON matches API schema |
| `test_injector_keyword_match` | "schedule a meeting" → True |
| `test_injector_no_match` | "what's the weather" → False |
| `test_injector_custom_keywords` | Config-provided keywords respected |
| `test_pulse_dry_run` | Dry run prints prompt, does not POST |
| `test_cli_temporal_setup` | DB created, tables exist |
| `test_cli_temporal_validate` | Healthy system → exit 0 |
| `test_cli_temporal_status` | Shows counts, last pulse |

**Technical Notes:**
- Tests use `pytest` with existing `conftest.py` fixtures
- SQLite tests use in-memory DB (`:memory:`) — fast and isolated
- MCP tool tests extend existing `test_mcp_server.py` patterns
- `.ics` test fixtures stored in `tests/fixtures/` directory
- Email bridge tests build on Phase 1's `test_ingest.py`

---

## Config File Additions

New section in `/etc/parousia/config.yaml`:

```yaml
# Temporal presence configuration (Phase 2)
temporal:
  enabled: true

  # Database
  db:
    path: /var/lib/parousia/temporal.db       # SQLite path
    # postgres_url: "postgresql://..."         # Uncomment for PostgreSQL

  # DSL serialization
  dsl:
    timezone: UTC
    default_mode: standard
    modes:
      standard:
        past_days: 1
        future_days: 3
        include_journal: true
      planning:
        past_days: 0
        future_days: 14
        include_journal: false
      retrospective:
        past_days: 7
        future_days: 0
        include_journal: true
      full:
        past_days: 30
        future_days: 30
        include_journal: true

  # Opportunistic injection
  opportunistic:
    enabled: true
    injection_keywords:
      - schedule
      - calendar
      - meeting
      - appointment
      - event
      - next week
      - tomorrow
      - yesterday
      - remind
      - deadline
      - due date
      - upcoming
      - this month
      - timeline
      - agenda
      - invite
      - rsvp

  # Unstructured text parsing (optional)
  llm_parse_endpoint: ""                      # e.g., "http://localhost:8001/parse-time"

  # Monthly nomination pulse
  pulse:
    enabled: true
    schedule: "0 9 1 * *"                    # First of month, 09:00 UTC
    prompt_template: |
      [NOMINATION PULSE]
      It has been {days_since_last} days since your last temporal milestone review.
      Your recent activity spans {event_count} calendar events.
      Your journal currently holds {journal_count} entries.
      Consider: did you complete significant research, ship something notable,
      make an important decision, or reach a milestone in the past month?
      If so, use nominate_milestone to record it in your temporal journal.

  # Export defaults
  export:
    default_format: ics
    default_range_days: 30
```

---

## Filesystem Layout (additions)

```
/var/lib/parousia/
  temporal.db                            # SQLite temporal database (new)

/etc/parousia/
  config.yaml                            # Updated with temporal section (modified)

/usr/local/bin/parousia-guard            # Unchanged — same CLI entry point

/var/log/parousia/
  temporal.log                           # Temporal subsystem logs (new)
```

---

## Dependency Graph (Implementation Order)

```
US-10 (DB Schema) ─────┐
                        ├──→ US-11 (DSL Serializer)
                        │         │
                        │         ├──→ US-12 (MCP Temporal Tools)
                        │         │         │
US-13 (Ingest Pipeline)─┤         │         ├──→ US-14 (Translation Layer)
                        │         │         │
                        │         │         ├──→ US-15 (Opportunistic Injector)
                        │         │         │
                        │         │         └──→ US-16 (Journal + Pulse)
                        │         │                   │
                        └─────────┴───────────────────┤
                                                      ▼
                                              US-17 (CLI Extensions)
                                                      │
                                                      ▼
                                              US-18 (Integration Tests)
```

**Parallelization note:** US-13 (Ingest Pipeline) and US-14 (Translation Layer) both depend on US-10 (DB Schema) but are independent of each other. US-15 (Injector) and US-16 (Journal) are independent of each other and US-13/US-14.

---

## Edge Cases & Failure Modes

| Scenario | Behavior |
|----------|----------|
| `.ics` attachment with no VEVENT component | Parse succeeds, 0 events extracted, attachment still delivered to agent |
| Recurrence rule with no end date | Cap expansion at 365 days from now. Store `X-PAROUSIA-MAX-EXPANSION` in metadata. |
| Agent sends `schedule_event` with past `start_time` | Accept and store (retroactive scheduling is valid for journaling). Log warning. |
| Conflict detection: 3 overlapping events | Return all 3 conflicts in array. Agent decides resolution (Phase 3 auto-resolve). |
| SQLite DB locked (concurrent writes) | WAL mode enabled by default. Retry up to 3x with 100ms backoff. |
| PostgreSQL connection lost | Raise error to agent. Temporal tools return 503. Email tools unaffected. |
| Agent ID not in config but found in MCP session | Dynamic agent provisioning: create agent-scoped views on demand (no config pre-registration needed) |
| Monthly pulse fires but agent webhook is down | Queue pulse (same retry logic as Phase 1 email queue). 3 attempts, then skip. |
| Journal grows to 10,000+ entries | `#JOURNAL (recent)` section caps at 5 entries. Full journal queryable via `temporal db --stats`. |
| Timezone in `.ics` is unknown/Olson ID | Map to UTC offset. Store original TZID in metadata. If unmappable, treat as UTC. |
| Structured JSON ingest with missing `agent_id` | Reject with 400. `agent_id` is required for all temporal writes. |

---

## Success Metrics (Phase 2)

- DSL serialization: standard mode <200 tokens for 15 events
- Ingest: `.ics` with 100 VEVENTs parsed in <1 second
- MCP: `get_temporal_context` response time <50ms (DB query + serialization)
- Export: `.ics` output validates against RFC 5545 (use `icalendar` round-trip)
- Test coverage: >80% line coverage on `src/parousia/temporal/`
- Email ↔ Calendar: `.ics` attachment in incoming mail → event stored before agent webhook returns (≤2 second add to existing pipeline)
- Opportunistic classifier: false positive rate acceptable (optimize for recall over precision)
- Schema: agent isolation verifiable — Agent A `get_temporal_context` returns 0 events from Agent B
