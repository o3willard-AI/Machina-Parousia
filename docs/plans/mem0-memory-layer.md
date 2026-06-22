# Mem0 Memory Layer for Parousia — Recommendation

## Current State

Parousia v0.2.0 provides three presence domains (11 MCP tools):

| Domain | Tools | Storage |
|--------|-------|---------|
| **Email** | `send_email`, `check_inbox` | InboxStore (SQLite) |
| **Temporal** | `get_temporal_context`, `schedule_event`, `cancel_event`, `set_timer_alarm`, `nominate_milestone`, `resolve_conflicts` | TemporalDB (SQLite) |
| **Spatial** | 3 browser/SDOM tools | BrowserPool (in-memory) |

Each domain has its own storage with **no cross-domain synthesis**. An agent can
schedule an event and send an email about it, but the system has no way to
connect those two facts. Retrieval is literal (SQL queries by agent_id +
time range) — there's no semantic search.

## What Mem0 Adds: The 4th Dimension of Presence

Mem0 becomes Parousia's **Memory presence** — the machine's ability to
remember, synthesize, and recall its activity across all three existing
domains.

### Before (current)

```
Agent: "What did I tell Sarah about the deployment?"
  → checks email inbox: literal search for "Sarah deployment" → 0 results
  → checks temporal: literal search for "deployment" → finds 1 event
  → can't connect that the email about deployment went to Sarah
```

### After (with Mem0)

```
Agent: "What did I tell Sarah about the deployment?"
  → mem0_search("Sarah deployment")
  → returns synthesized fact: "Emailed sarah@example.com on Jun 15 about 
    deployment timeline. Scheduled deployment event for Jun 20."
  → score: 0.89, sourced from 2 tool calls across email + temporal
```

### The 4 presence dimensions become

| Dimension | What it means | Tools |
|-----------|---------------|-------|
| **Email** | Reachable, responsive | send_email, check_inbox |
| **Temporal** | Scheduled, punctual | schedule_event, timers, milestones |
| **Spatial** | Observant, active on the web | browser tools |
| **Memory** ✨ | Remembers, synthesizes, has continuity | mem0_search, mem0_profile |

## Architecture

### Integration point: MCP server tool dispatch

The cleanest integration is in `guard/mcp_server.py`'s `handle_call_tool()`.
Every tool call (across all 3 domains) flows through one dispatch function.
After each successful tool invocation, store the fact in Mem0.

No new MCP tools needed — the existing Hermes Mem0 plugin already provides
`mem0_search`, `mem0_profile`, and `mem0_conclude`. Parousia just needs to
*write* to Mem0; agents read via their existing Hermes memory tools.

### Data flow

```
Agent calls Parousia MCP tool
        │
        ▼
  handle_call_tool() dispatches
        │
        ├──▶ Tool handler runs (send_email, schedule_event, etc.)
        │         │
        │         ▼
        │    Returns result to agent
        │
        └──▶ [NEW] _record_to_mem0(tool_name, arguments, result, agent_id)
                    │
                    ▼
              Formats fact string:
              "Agent hermes called send_email: to=sarah@example.com, 
               subject='Deployment timeline', sent=True"
                    │
                    ▼
              mem0_client.add(messages=[fact], user_id=agent_id, 
                              agent_id="parousia", infer=True)
                    │
                    ▼
              Mem0 extracts structured facts automatically:
              "Emailed sarah@example.com about deployment timeline on Jun 22"
```

### What gets recorded

For each tool call, we format a natural-language fact string:

| Tool | Fact template |
|------|---------------|
| `send_email` | `Sent email to {to}: "{subject}". Delivered: {sent}.` |
| `check_inbox` | `Checked inbox. {count} messages, {unread} unread.` |
| `schedule_event` | `Scheduled "{title}" for {start_time} ({flexibility} flexibility). Conflicts: {n}.` |
| `cancel_event` | `Cancelled event "{title}" ({event_id}).` |
| `set_timer_alarm` | `Set {type} "{title}" — {remaining} remaining.` |
| `nominate_milestone` | `Recorded {entry_type}: "{title}" at {occurred_at}.` |
| `resolve_conflicts` | `Resolved {count} temporal conflicts.` |
| `get_temporal_context` | *Skipped* — read-only, no new fact |
| Spatial tools | `Browser action: {description}` |

`check_inbox` and `get_temporal_context` have special handling — we record
a summary fact only when new messages/events are discovered, and skip when
the result is empty or informational.

### Mem0 client configuration

Parousia uses the same Clubhouse Qdrant + .23 LM Studio setup as Hermes:

```yaml
# /etc/parousia/mem0.yaml (new config file)
mode: local
user_id_prefix: parousia-   # produces "parousia-hermes", "parousia-claude"
vector_store_host: 192.168.101.42
vector_store_port: 6333
llm_provider: lmstudio
llm_model: qwen2.5-coder-3b-instruct
llm_base_url: http://192.168.101.23:1234/v1
embedder_provider: lmstudio
embedder_model: text-embedding-nomic-embed-text-v1.5
embedder_base_url: http://192.168.101.23:1234/v1
```

Agent IDs are prefixed to avoid collisions with Hermes user_ids
(e.g., Parousia agent `hermes` → Mem0 user `parousia-hermes`).

### Resilience

- Mem0 writes are **fire-and-forget** — tool response is never delayed
- Write failures are logged but never surface to the calling agent
- Circuit breaker: after 5 consecutive Mem0 failures, pauses writes for 120s
- If Mem0 is down, Parousia tools continue working normally — just without
  memory accumulation during the outage

## Implementation Plan

### Files to create/modify

| File | Action | Purpose |
|------|--------|---------|
| `src/parousia/memory/__init__.py` | NEW | Mem0 client wrapper for Parousia |
| `src/parousia/memory/recorder.py` | NEW | Tool fact formatting + Mem0 write |
| `src/parousia/memory/config.py` | NEW | Mem0 config loader |
| `src/parousia/guard/mcp_server.py` | MODIFY | Add `_record_to_mem0()` call after each tool dispatch |
| `tests/test_memory_recorder.py` | NEW | Unit tests for fact formatting + write |
| `tests/test_mcp_memory_integration.py` | NEW | Integration: MCP tool calls → Mem0 write |
| `docs/capabilities/memory.md` | NEW | Capability guide for agents |

### Estimated effort: ~4 hours

1. **Memory module** (1h) — `recorder.py` with fact formatters per tool type
2. **Config wiring** (30m) — `mem0.yaml` config, client initialization
3. **MCP integration** (30m) — hook into `handle_call_tool()` dispatch
4. **Tests** (1.5h) — unit tests for formatters, integration test with real Qdrant
5. **Documentation** (30m) — capability guide

## What Agents Get

After this integration, every Hermes agent with Parousia access gains:

1. **Cross-domain recall** — "What happened with the AWS migration?"
   returns facts spanning emails sent, events scheduled, and milestones created

2. **Continuous identity** — Different Hermes sessions share the same
   Parousia memory. Agent A sends an email; Agent B knows about it.

3. **Semantic search** — No more literal SQL queries. "What's urgent?"
   surfaces timer alarms about to fire and high-priority events.

4. **Zero-effort** — Agents don't need to explicitly call `mem0_conclude`.
   Every Parousia tool call is automatically recorded.

5. **Offline resilience** — Mem0 runs locally on Clubhouse. No external
   API calls, no data leaves the network.

## Why Not Build a Separate MCP Tool

The Hermes Mem0 plugin already gives agents `mem0_search` and `mem0_profile`.
Parousia doesn't need to re-expose these — they're already in every agent's
toolset. Parousia's job is to **populate** the memory with presence data.
The agent reads via Hermes, writes via Parousia.
