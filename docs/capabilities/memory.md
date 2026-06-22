# Memory — Capability Guide

Parousia's 4th presence domain. Every tool call — sending email, scheduling events, browsing web pages — is automatically recorded as a natural-language fact and stored in a vector database. Agents can search across their own history and discover facts from other agents, giving the Machine a persistent, searchable memory of everything it has done.

All facts are stored in Qdrant on the Parousia host (port 6333). Embeddings use fastembed (BAAI/bge-small-en-v1.5, 384-dim) — no external API calls, no GPU required. Writes are fire-and-forget on a daemon thread and return in <1ms; they never block tool calls.

---

## How it works

```
Agent calls tool → handle_call_tool() dispatches → returns result to agent
                         │
                         └── _record_to_mem0() (daemon thread, <1ms)
                                  │
                         ┌────────┴────────┐
                         │  Circuit breaker │
                         │  5 failures →    │
                         │  120s cooldown   │
                         └────────┬────────┘
                                  │
                         ┌────────┴────────┐
                         │  Fact formatter  │
                         │  (one per tool)  │
                         └────────┬────────┘
                                  │
                         ┌────────┴────────┐
                         │  mem0.Memory     │
                         │  .add(fact,      │
                         │   user_id=...,   │
                         │   infer=False)   │
                         └────────┬────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
              Qdrant :6333              SQLite history DB
              (vector search)           (audit log)
```

Each agent's facts are namespaced under `parousia-{agent_id}`. Read-only tools (`get_temporal_context`, `extract_page_state`) are skipped — only write-side actions are recorded. Failed tool calls are also skipped, so memory only contains successful actions.

---

## Fact formatters

Every tool has a dedicated formatter that converts the tool call into a single natural-language sentence. Below are the facts each tool produces.

### Email tools

| Tool | Fact produced | Example |
|------|--------------|---------|
| `send_email` | ✅ | `Sent email to team@example.com: "v0.3.0 rollout plan".` |
| `send_email` (queued) | ✅ | `Queued email to team@example.com: "v0.3.0 rollout plan" for approval.` |
| `send_email` (failed) | ✅ | `Failed to send email to team@example.com: rate_limit_exceeded.` |
| `check_inbox` | ⚠️ Only if unread messages found | `Checked inbox: 3 unread message(s) from alice@corp.com, bob@corp.com.` |
| `check_inbox` (no unread) | ❌ Skipped | — |

### Temporal tools

| Tool | Fact produced | Example |
|------|--------------|---------|
| `schedule_event` | ✅ | `Scheduled "Deploy v0.3.0" for 2026-06-27T14:00:00Z (medium flexibility). 1 conflict(s) resolved.` |
| `cancel_event` | ✅ | `Cancelled event "Deploy v0.3.0".` |
| `set_timer_alarm` | ✅ | `Set alarm "QA review deadline" — fires in ~1380 min.` |
| `nominate_milestone` | ✅ | `Recorded release: "Mem0 memory layer v0.3.0" at 2026-06-22.` |
| `resolve_conflicts` | ✅ | `Resolved temporal conflicts: 2 moved, 0 skipped.` |
| `get_temporal_context` | ❌ Skipped | Read-only informational tool |

### Spatial tools

| Tool | Fact produced | Example |
|------|--------------|---------|
| `browse_to` | ✅ | `Browsed to https://github.com/NousResearch/hermes-agent/releases.` |
| `browse_to` (failed) | ❌ Skipped | — |
| `interact` (click) | ✅ | `Clicked #submit-button.` |
| `interact` (type) | ✅ | `Typed "hello world" into #search-input.` |
| `interact` (failed) | ❌ Skipped | — |
| `extract_page_state` | ❌ Skipped | Read-only extraction |

> **Total:** 9 tools produce facts. 3 are read-only skips. 4 skip on empty results or errors.

---

## Querying memory

### Within an MCP session (agent's own facts)

Agents connected via MCP can search their own memory:

**Search by meaning:**
```json
{
  "tool": "mem0_search",
  "arguments": {
    "query": "deployment events June 2026",
    "top_k": 5
  }
}
```
```json
{
  "results": [
    {
      "memory": "Scheduled \"Deploy v0.3.0 to production\" for 2026-06-27T14:00:00Z (medium flexibility).",
      "score": 0.89
    },
    {
      "memory": "Sent email to team@example.com: \"v0.3.0 rollout plan\".",
      "score": 0.76
    }
  ],
  "count": 2
}
```

**Full profile:**
```json
{
  "tool": "mem0_profile",
  "arguments": {}
}
```
Returns all stored facts for the agent.

### Cross-agent search (direct Qdrant)

To discover facts from OTHER agents, query Qdrant directly:

```bash
# All facts from the hermes agent
curl -s http://localhost:6333/collections/mem0/points/scroll \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"user_id","match":{"value":"parousia-hermes"}}]},"limit":20,"with_payload":true}'

# All facts across ALL agents (prefix match)
curl -s http://localhost:6333/collections/mem0/points/scroll \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"user_id","match":{"text":"parousia-"}}]},"limit":50,"with_payload":true}'
```

Each point's payload contains:
| Field | Description |
|-------|-------------|
| `user_id` | `parousia-{agent_id}` — which agent recorded this fact |
| `agent_id` | Always `"parousia"` — the system ID |
| `data` | The natural-language fact text |
| `hash` | Content hash for deduplication |
| `created_at` | ISO 8601 timestamp |

---

## Circuit breaker

Memory writes go through a circuit breaker that prevents hammering a down Qdrant backend:

| State | Behavior |
|-------|----------|
| **Closed** (normal) | All writes go through. Failures are counted. |
| **Open** (tripped) | All writes are silently skipped. Tool calls succeed normally. |
| **Half-open** (recovery) | After 120s cooldown, first write attempt reconnects. |

**Trip threshold:** 5 consecutive failures within the cooldown window.

**Recovery:** Circuit resets after 120 seconds. If the first post-reset write succeeds, the circuit closes. If it fails, the circuit re-opens for another 120s.

**Key guarantee: Tool calls NEVER block on memory.** The circuit breaker ensures that even if Qdrant is unreachable for hours, every `send_email`, `schedule_event`, and `browse_to` call completes normally.

### Circuit breaker log output

```
WARNING  parousia.memory  Parousia Mem0 circuit breaker tripped after 5 failures.
                           Pausing for 120s.
... tool calls continue normally, memory writes skipped ...
INFO     parousia.memory  Mem0 circuit breaker reset — writes resumed.
```

---

## Configuration

Memory is configured via `/etc/parousia/mem0.yaml`:

```yaml
mode: local                    # Always "local" — self-hosted Qdrant
user_id_prefix: parousia-      # Prepended to every agent's user_id

# Vector store
vector_store_host: 127.0.0.1   # Qdrant host
vector_store_port: 6333         # Qdrant gRPC/REST port
vector_store_provider: qdrant

# Embeddings (CPU-only, no external API)
embedder_provider: fastembed
embedder_model: BAAI/bge-small-en-v1.5
embedding_model_dims: 384

# LLM extraction (optional — set provider to "" to disable)
llm_provider: ""
llm_model: ""
llm_base_url: ""
```

**Minimal setup checklist:**

1. Qdrant installed and running: `systemctl status qdrant`
2. Config file at `/etc/parousia/mem0.yaml`
3. `mem0ai` Python package installed in the Parousia venv
4. Parousia guard restarted after config changes: `systemctl restart parousia-guard`

---

## Verification

### Quick smoke test

```bash
# Check Qdrant health
curl -s http://localhost:6333/healthz
# healthz check passed

# Check collection exists
curl -s http://localhost:6333/collections/mem0

# Count facts
curl -s http://localhost:6333/collections/mem0/points/count -d '{}'
# {"result":{"count":73},"status":"ok","time":...}
```

### Multi-agent test suite

A comprehensive cross-agent verification script lives at `tests/multi_agent_mem0_test.py`. It exercises all 5 scenarios from the implementation plan (cross-agent temporal awareness, email+event synthesis, spatial discovery, timeline reconstruction, circuit breaker resilience) and can be run directly on the Parousia host:

```bash
/opt/parousia/venv/bin/python3 tests/multi_agent_mem0_test.py
```

Expected: `34 passed, 0 failed — MULTI-AGENT VERIFICATION: PASSED`

---

## Storage

| Store | Location | Purpose |
|-------|----------|---------|
| Qdrant | `:6333`, collection `mem0` | Vector embeddings for semantic search |
| History DB | `/var/lib/parousia/mem0_history.db` | SQLite audit log of all writes |

Qdrant uses ~74MB RAM with fastembed. The history DB grows with every write — facts are ~200 bytes each. At 10,000 tool calls/day, expect ~2MB/day of history DB growth.

---

## Design decisions

**Why fire-and-forget?** Tool calls must return to the agent in milliseconds. Waiting for a Qdrant write (embedding + index) would add 50-200ms latency to every tool call. The daemon thread decouples tool dispatch from memory recording.

**Why `infer=False`?** Mem0's built-in LLM extraction rephrases facts using an external model. We skip this — the fact formatters already produce clean natural language. `infer=False` means facts are stored verbatim, making them exactly predictable and eliminating LLM latency/cost.

**Why fastembed?** Parousia runs on CPU-only hosts (t3.small, m7i-flex). fastembed provides 384-dim embeddings via ONNX runtime with no GPU, no Docker, and no external API calls. Embedding latency is ~5ms per fact.

**Why circuit breaker?** Without it, 5 failed Qdrant writes would block 5 daemon threads for their full TCP timeout (~30s each), piling up and consuming thread pool resources. The circuit breaker stops attempting writes after 5 failures, letting tool calls flow unimpeded until Qdrant recovers.
