# Temporal — Capability Guide

Parousia gives every agent a calendar, a journal, and scheduling intelligence. The temporal engine builds on Phase 1's email — events can come from .ics email attachments or be created directly by agents.

All temporal data is stored in SQLite at `/var/lib/parousia/temporal.db` (configurable), scoped per-agent via `agent_id`.

---

## Tools

### `get_temporal_context`

Returns the agent's calendar in a token-lean DSL format. Designed for LLM context windows — compact, scannable, no JSON bloat.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `mode` | ❌ | Temporal window: `standard` (default), `planning`, `retrospective`, `full` |
| `agent_id` | ❌ | Auto-detected from auth session |

**Modes:**

| Mode | Past | Future | Journal | Use case |
|------|------|--------|---------|----------|
| `standard` | 24h | 3 days | ✅ | Daily context |
| `planning` | — | 14 days | ❌ | Scheduling new events |
| `retrospective` | 7 days | — | ✅ | Weekly review |
| `full` | 30 days | 30 days | ✅ | Deep context |

**Response — `standard` mode:**
```json
{
  "context": "#TIMEBOX\n!NOW: 2026-06-11 W24 Thu 14:30 | DOMAIN: GENERAL_CORP\n\n#PAST_WINDOW (1d)\n- 06-10 09:00|10:00 [id:e2] Standup *DONE\n- 06-10 14:00|15:00 [id:e1] Code review PR#47 *DONE\n\n#PLANNED_WINDOW (3d)\n- 06-11 15:00|16:00 [id:e3] Deploy Parousia v0.2.0\n- 06-11 16:00|17:00 [id:e5] Architecture review\n- 06-12 10:00|11:00 [id:e4] Sprint planning\n\n#JOURNAL\n- 06-09 [j1] *milestone* MR-Krabs pipeline hardening complete\n- 06-10 [j2] *decision* Use SES for outbound until port 25 approved",
  "mode": "standard",
  "event_count": 5,
  "conflicts": [
    {
      "events": ["e3", "e5"],
      "overlap": "2026-06-11T16:00",
      "severity": "partial_overlap"
    }
  ],
  "consideration": "You have 1 scheduling conflict(s). Review before planning."
}
```

**DSL format reference:**

| Section | Format | Example |
|---------|--------|---------|
| Header | `!NOW: YYYY-MM-DD Www DDD HH:MM \| DOMAIN: NAME` | `!NOW: 2026-06-11 W24 Thu 14:30 \| DOMAIN: GENERAL_CORP` |
| Past event | `MM-DD HH:MM\|HH:MM [id:XN] title *DONE` | `06-10 09:00\|10:00 [id:e2] Standup *DONE` |
| Upcoming event | `MM-DD HH:MM\|HH:MM [id:XN] title` | `06-12 10:00\|11:00 [id:e4] Sprint planning` |
| Journal entry | `MM-DD [jN] *type* description` | `06-10 [j2] *decision* Use SES for outbound` |
| Timer | `MM-DD HH:MM\|HH:MM [id:XN] title ⏰` | `06-11 14:45\|14:45 [id:t1] Review reminder ⏰` |

Event IDs are short (`e3`, `j2`) — use them as-is with `cancel_event` and `interact` tools.

---

### `schedule_event`

Create a calendar event. Returns export payloads for iCal, Google Calendar, and Microsoft Graph.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `title` | ✅ | Event title |
| `start_time` | ✅ | ISO 8601 datetime |
| `end_time` | ❌ | ISO 8601 datetime (default: start + 1 hour) |
| `flexibility` | ❌ | `high` (default), `low`, or `none` |
| `auto_resolve` | ❌ | Auto-resolve time conflicts (default: `true`) |
| `stakeholders` | ❌ | Comma-separated stakeholder list |
| `metadata` | ❌ | Arbitrary key-value metadata |

**Request:**
```json
{
  "tool": "schedule_event",
  "arguments": {
    "title": "Deploy Parousia v0.2.0",
    "start_time": "2026-06-11T15:00:00Z",
    "end_time": "2026-06-11T16:00:00Z",
    "flexibility": "low"
  }
}
```

**Response (no conflicts):**
```json
{
  "scheduled": true,
  "event_id": "e6",
  "title": "Deploy Parousia v0.2.0",
  "conflicts": []
}
```

**Response (conflict auto-resolved):**
```json
{
  "scheduled": true,
  "event_id": "e6",
  "title": "Deploy Parousia v0.2.0",
  "conflicts": [
    {"events": ["e6", "e5"], "overlap": "2026-06-11T15:30", "severity": "partial_overlap"}
  ],
  "resolution": {
    "resolved": 1,
    "unresolved": 0,
    "actions": [
      "Moved e5 (flexibility=high) from 15:00-17:00 to 16:00-18:00 to accommodate e6"
    ]
  }
}
```

---

### `cancel_event`

Soft-delete an event by its short ID.

**Request:**
```json
{
  "tool": "cancel_event",
  "arguments": {
    "event_id": "e3"
  }
}
```

**Response:**
```json
{
  "cancelled": true,
  "event_id": "e3",
  "title": "Deploy Parousia v0.2.0"
}
```

Cancelled events remain in the database with `status: cancelled`. They're excluded from `get_temporal_context` output. Use short IDs — `e3` not `hermes:e3`.

---

### `set_timer_alarm`

Set a countdown timer or absolute alarm. Exactly one of `duration_minutes` or `trigger_at` must be provided.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `title` | ✅ | What the timer is for |
| `duration_minutes` | ❌ | Countdown duration (mutually exclusive with `trigger_at`) |
| `trigger_at` | ❌ | Absolute alarm time in ISO 8601 (mutually exclusive with `duration_minutes`) |

**Timer request:**
```json
{
  "tool": "set_timer_alarm",
  "arguments": {
    "title": "Check build status",
    "duration_minutes": 15
  }
}
```

**Response:**
```json
{
  "set": true,
  "alarm_id": "t3",
  "type": "timer",
  "remaining_seconds": 900
}
```

**Alarm request:**
```json
{
  "tool": "set_timer_alarm",
  "arguments": {
    "title": "Sprint review",
    "trigger_at": "2026-06-13T10:00:00Z"
  }
}
```

---

### `nominate_milestone`

Record a research finding, decision, shipped feature, or general milestone in the temporal journal.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `title` | ✅ | Milestone title |
| `occurred_at` | ✅ | ISO 8601 date or datetime when it happened |
| `entry_type` | ❌ | `milestone` (default), `research`, `decision`, or `shipped` |
| `description` | ❌ | Free-text summary |
| `tags` | ❌ | Comma-separated tags |

**Request:**
```json
{
  "tool": "nominate_milestone",
  "arguments": {
    "title": "Pipeline hardening complete — 928 tests, 5 tasks",
    "occurred_at": "2026-06-11",
    "entry_type": "shipped",
    "tags": "mr-krabs,pipeline,hardening"
  }
}
```

**Response:**
```json
{
  "recorded": true,
  "journal_id": "j5",
  "title": "Pipeline hardening complete — 928 tests, 5 tasks"
}
```

The Monthly Nomination Pulse (cron-driven) periodically prompts agents to review their journal and nominate new milestones.

---

### `resolve_conflicts`

Detect and auto-resolve scheduling conflicts across all confirmed events. Call this before planning sessions to ensure a clean calendar.

**Request:**
```json
{
  "tool": "resolve_conflicts",
  "arguments": {}
}
```

**Response:**
```json
{
  "resolved": 2,
  "unresolved": 0,
  "actions": [
    "Moved e5 (flexibility=high) from 15:00-17:00 to 16:00-18:00 to accommodate e3",
    "Moved e7 (flexibility=low) from 16:00-16:30 to 18:00-18:30 to accommodate e5"
  ]
}
```

---

## Conflict resolution rules

When `schedule_event` detects overlap (default `auto_resolve=true`), or when `resolve_conflicts` is called standalone:

1. **`flexibility='none'` events are NEVER moved** — they're immovable
2. **`high` gives way to `low`, `low` gives way to `none`** — more flexible events move
3. **Equal flexibility → move the shorter-duration event** — minimizes displacement
4. **Equal duration → move the newer event** — preserves existing commitments

The moved event slides to the slot immediately after the blocker ends, with forward collision scanning to avoid creating new conflicts. Conflicts between two `none` events are reported as `unresolved` — they require manual intervention.

---

## Export formats

Every `schedule_event` call also generates export payloads in three formats:

- **iCal (.ics)** — for Apple Calendar, Thunderbird, etc.
- **Google Calendar API JSON** — for programmatic Google Calendar integration
- **Microsoft Graph API JSON** — for Outlook/Exchange integration

These are attached to the response as `exports.ics`, `exports.google`, and `exports.ms_graph`. Use them to sync agent calendars to external systems.

---

## .ics email ingest

When an email arrives with a `.ics` attachment (meeting invite), the temporal ingest pipeline automatically:
1. Parses the `.ics` file
2. Extracts event details (title, time, attendees, recurrence)
3. Creates a tentative event in the agent's temporal DB
4. Tags it with `source: ics-email`

The agent sees these in `get_temporal_context` with the source annotation. No manual event creation needed for calendar invites.
