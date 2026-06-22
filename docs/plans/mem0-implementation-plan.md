# Mem0 Memory Layer — Implementation Plan

> **Target release:** Parousia v0.3.0  
> **Dependency:** Clubhouse Qdrant + .23 LM Studio (already deployed)  
> **Estimated effort:** 4 hours

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File-by-File Implementation](#2-file-by-file-implementation)
3. [Integration Point: MCP Server](#3-integration-point-mcp-server)
4. [Fact Formatting Rules](#4-fact-formatting-rules)
5. [Configuration](#5-configuration)
6. [QA Test Suite](#6-qa-test-suite)
7. [Functional Verification](#7-functional-verification)
8. [Risk Register](#8-risk-register)

---

## 1. Architecture Overview

```
                         MCP Server (handle_call_tool)
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
   send_email            temporal tools         spatial tools
   check_inbox           (6 tools)              (3 tools)
          │                     │                     │
          └─────────────────────┼─────────────────────┘
                                │
                    [NEW] _record_to_mem0()
                                │
                    ┌───────────┴───────────┐
                    │  MemoryRecorder       │
                    │  .record_tool_call()  │
                    │  ._format_fact()      │
                    │  ._should_record()    │
                    └───────────┬───────────┘
                                │
                    ┌───────────┴───────────┐
                    │  mem0.Memory          │
                    │  (local mode)         │
                    │  .add(fact,           │
                    │       user_id=...,    │
                    │       agent_id=...,   │
                    │       infer=True)     │
                    └───────────┬───────────┘
                                │
                    ┌───────────┴───────────┐
                    │  Clubhouse Qdrant     │
                    │  192.168.101.42:6333  │
                    └───────────────────────┘
```

**Key design decisions:**

1. **Fire-and-forget** — `_record_to_mem0()` never blocks the tool response. Runs on a daemon thread.
2. **No new MCP tools** — Agents read via existing Hermes `mem0_search` / `mem0_profile`.
3. **Agent-scoped** — Parousia agent IDs are prefixed (`parousia-hermes`) to avoid name collisions with Hermes user_ids.
4. **Circuit breaker** — Same pattern as the Hermes plugin: 5 consecutive failures → 120s cooldown.
5. **Config file** — `/etc/parousia/mem0.yaml` (same format as `~/.hermes/mem0.json`).

---

## 2. File-by-File Implementation

### 2.1 `src/parousia/memory/__init__.py` (NEW)

Package init. Exports `MemoryRecorder`.

```python
"""Parousia memory layer — Mem0-backed presence memory."""

from parousia.memory.recorder import MemoryRecorder

__all__ = ["MemoryRecorder"]
```

### 2.2 `src/parousia/memory/config.py` (NEW)

Loads Mem0 configuration from `/etc/parousia/mem0.yaml` with sensible defaults.

```python
"""Mem0 configuration loader for Parousia."""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = "/etc/parousia/mem0.yaml"

@dataclass
class Mem0Config:
    mode: str = "local"
    user_id_prefix: str = "parousia-"
    vector_store_host: str = "192.168.101.42"
    vector_store_port: int = 6333
    vector_store_provider: str = "qdrant"
    embedding_model_dims: int = 768
    llm_provider: str = "lmstudio"
    llm_model: str = "qwen2.5-coder-3b-instruct"
    llm_base_url: str = "http://192.168.101.23:1234/v1"
    llm_temperature: float = 0.1
    embedder_provider: str = "lmstudio"
    embedder_model: str = "text-embedding-nomic-embed-text-v1.5"
    embedder_base_url: str = "http://192.168.101.23:1234/v1"

    @classmethod
    def from_file(cls, path: str = DEFAULT_CONFIG_PATH) -> "Mem0Config":
        """Load from YAML config file, falling back to defaults."""
        if os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        # Filter to known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def to_mem0_dict(self) -> dict:
        """Convert to mem0.Memory.from_config() dict."""
        return {
            "vector_store": {
                "provider": self.vector_store_provider,
                "config": {
                    "host": self.vector_store_host,
                    "port": self.vector_store_port,
                    "embedding_model_dims": self.embedding_model_dims,
                },
            },
            "llm": {
                "provider": self.llm_provider,
                "config": {
                    "model": self.llm_model,
                    "lmstudio_base_url": self.llm_base_url,
                    "temperature": self.llm_temperature,
                },
            },
            "embedder": {
                "provider": self.embedder_provider,
                "config": {
                    "model": self.embedder_model,
                    "lmstudio_base_url": self.embedder_base_url,
                },
            },
            "history_db_path": "/var/lib/parousia/mem0_history.db",
        }
```

### 2.3 `src/parousia/memory/recorder.py` (NEW)

Core module — formats tool facts and writes to Mem0.

```python
"""MemoryRecorder — formats Parousia tool calls into Mem0 facts."""

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

from parousia.memory.config import Mem0Config

logger = logging.getLogger("parousia.memory")

# Circuit breaker
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120

# ── Fact formatters per tool ──────────────────────

_FACT_FORMATTERS: Dict[str, callable] = {}

def _register(*tool_names: str):
    """Decorator to register a fact formatter for one or more tools."""
    def decorator(fn):
        for name in tool_names:
            _FACT_FORMATTERS[name] = fn
        return fn
    return decorator


# ── Email tools ────────────────────────────────────

@_register("send_email")
def _fmt_send_email(args: dict, result: dict, agent_id: str) -> str:
    to = args.get("to", "unknown")
    subject = args.get("subject", "")
    sent = result.get("sent", False)
    if sent:
        return f'Sent email to {to}: "{subject}".'
    elif result.get("queued_for_approval"):
        return f'Queued email to {to}: "{subject}" for approval.'
    else:
        error = result.get("error", "unknown error")
        return f"Failed to send email to {to}: {error}."


@_register("check_inbox")
def _fmt_check_inbox(args: dict, result: dict, agent_id: str) -> Optional[str]:
    """Only record when new messages are found."""
    messages = result.get("messages", [])
    if not messages:
        return None  # Skip — no new information
    unread = sum(1 for m in messages if not m.get("read", True))
    if unread == 0:
        return None  # Skip — nothing unread
    senders = list({m["sender"] for m in messages if not m.get("read", True)})[:3]
    sender_list = ", ".join(senders)
    return f"Checked inbox: {unread} unread message(s) from {sender_list}."


# ── Temporal tools ─────────────────────────────────

@_register("schedule_event")
def _fmt_schedule_event(args: dict, result: dict, agent_id: str) -> str:
    title = args.get("title", "")
    start = args.get("start_time", "")
    flexibility = args.get("flexibility", "high")
    conflicts = result.get("conflicts", [])
    n_conflicts = len(conflicts) if conflicts else 0
    base = f'Scheduled "{title}" for {start} ({flexibility} flexibility).'
    if n_conflicts:
        base += f" {n_conflicts} conflict(s) resolved."
    return base


@_register("cancel_event")
def _fmt_cancel_event(args: dict, result: dict, agent_id: str) -> str:
    title = result.get("title", args.get("event_id", "unknown"))
    return f'Cancelled event "{title}".'


@_register("set_timer_alarm")
def _fmt_set_timer_alarm(args: dict, result: dict, agent_id: str) -> str:
    title = args.get("title", "")
    timer_type = result.get("type", "timer")
    remaining = result.get("remaining_seconds", 0)
    mins = remaining // 60
    return f'Set {timer_type} "{title}" — fires in ~{mins} min.'


@_register("nominate_milestone")
def _fmt_nominate_milestone(args: dict, result: dict, agent_id: str) -> str:
    title = args.get("title", "")
    entry_type = args.get("entry_type", "milestone")
    occurred = args.get("occurred_at", "")
    desc = args.get("description", "")
    fact = f'Recorded {entry_type}: "{title}"'
    if occurred:
        fact += f" at {occurred}"
    fact += "."
    return fact


@_register("resolve_conflicts")
def _fmt_resolve_conflicts(args: dict, result: dict, agent_id: str) -> str:
    moved = result.get("moved", 0)
    skipped = result.get("skipped", 0)
    return f"Resolved temporal conflicts: {moved} moved, {skipped} skipped."


@_register("get_temporal_context")
def _fmt_get_temporal_context(args: dict, result: dict, agent_id: str) -> Optional[str]:
    """Skip — read-only informational tool."""
    return None


# ── Spatial tools ──────────────────────────────────

@_register("browse_to")
def _fmt_browse_to(args: dict, result: dict, agent_id: str) -> Optional[str]:
    url = args.get("url", "")
    if result.get("error"):
        return None  # Skip failures
    return f"Browsed to {url}."


@_register("interact")
def _fmt_interact(args: dict, result: dict, agent_id: str) -> Optional[str]:
    action = args.get("action", "")
    element_id = args.get("id", "")
    text = args.get("text", "")
    if result.get("error"):
        return None
    if action == "type" and text:
        return f'Typed "{text[:80]}" into {element_id}.'
    elif action == "click":
        return f"Clicked {element_id}."
    else:
        return f"Performed {action} on {element_id}."


@_register("extract_page_state")
def _fmt_extract_page_state(args: dict, result: dict, agent_id: str) -> Optional[str]:
    """Skip — read-only extraction, too verbose to summarize usefully."""
    return None


# ── Recorder class ──────────────────────────────────

class MemoryRecorder:
    """Records Parousia tool calls as Mem0 facts.

    Fire-and-forget: writes happen on a background daemon thread.
    Circuit breaker prevents hammering a down Mem0 backend.
    """

    def __init__(self, config: Optional[Mem0Config] = None):
        self._config = config or Mem0Config.from_file()
        self._memory = None
        self._memory_lock = threading.Lock()
        self._sync_thread = None
        # Circuit breaker
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    def _get_memory(self):
        """Lazy-init the mem0.Memory client."""
        with self._memory_lock:
            if self._memory is not None:
                return self._memory
            from mem0 import Memory
            self._memory = Memory.from_config(self._config.to_mem0_dict())
            return self._memory

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning(
                "Parousia Mem0 circuit breaker tripped after %d failures. "
                "Pausing for %ds.",
                self._consecutive_failures, _BREAKER_COOLDOWN_SECS,
            )

    def record_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        result: dict,
        agent_id: str,
    ) -> None:
        """Record a tool call as a Mem0 fact (fire-and-forget).

        Args:
            tool_name: MCP tool name (e.g. 'send_email')
            arguments: Tool call arguments
            result: Parsed JSON result from the tool handler
            agent_id: Parousia agent ID (e.g. 'hermes')
        """
        if self._is_breaker_open():
            return

        formatter = _FACT_FORMATTERS.get(tool_name)
        if formatter is None:
            return  # Unknown tool — skip

        try:
            fact = formatter(arguments, result, agent_id)
        except Exception as e:
            logger.debug("Fact formatter failed for %s: %s", tool_name, e)
            return

        if fact is None:
            return  # Formatter chose to skip (read-only tool, empty result, etc.)

        # Fire and forget on background thread
        mem0_user_id = f"{self._config.user_id_prefix}{agent_id}"

        def _write():
            try:
                memory = self._get_memory()
                messages = [{"role": "user", "content": fact}]
                memory.add(
                    messages,
                    user_id=mem0_user_id,
                    agent_id="parousia",
                    infer=True,
                )
                self._record_success()
                logger.debug("Mem0 recorded: %s → %s", tool_name, fact[:100])
            except Exception as e:
                self._record_failure()
                logger.warning("Mem0 write failed for %s: %s", tool_name, e)

        # Wait for any previous write to complete (avoids pileup)
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_write, daemon=True, name="parousia-mem0-write"
        )
        self._sync_thread.start()

    def shutdown(self):
        """Wait for pending writes, then close."""
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        with self._memory_lock:
            self._memory = None
```

### 2.4 `src/parousia/guard/mcp_server.py` (MODIFY)

Three changes needed:

**A. Import the recorder** (add to imports at top):

```python
from parousia.memory.recorder import MemoryRecorder
```

**B. Initialize the recorder** (in `_build_server()`, after `inbox_store`):

```python
    # Initialize Mem0 memory recorder
    memory_recorder = MemoryRecorder()
```

**C. Hook into `handle_call_tool()`** (after each tool dispatch, before return):

The dispatch already branches on tool name. Add `memory_recorder.record_tool_call()`
after each successful branch. Example for send_email:

```python
        if name == "send_email":
            result = await _handle_send_email(arguments, config, rate_limiter, redis_client)
            # Parse the JSON result for memory recording
            try:
                memory_recorder.record_tool_call(
                    "send_email", arguments,
                    json.loads(result[0].text), agent_id
                )
            except Exception:
                pass  # Memory recording is best-effort
            return result
```

Same pattern for temporal tools (parse the `result` JSON string before passing to recorder):

```python
        temporal_names = {s["name"] for s in ALL_TEMPORAL_SCHEMAS}
        if name in temporal_names:
            result_str = temporal_handlers.dispatch(name, arguments, agent_id)
            result = [TextContent(type="text", text=result_str)]
            try:
                memory_recorder.record_tool_call(
                    name, arguments, json.loads(result_str), agent_id
                )
            except Exception:
                pass
            return result
```

Same for spatial tools and check_inbox. The recorder call is always wrapped in
`try/except` so a memory failure never blocks the tool response.

### 2.5 Test files

**`tests/test_memory_recorder.py`** (NEW) — ~200 lines  
**`tests/test_mcp_memory_integration.py`** (NEW) — ~150 lines

Detailed in [Section 6](#6-qa-test-suite).

---

## 3. Integration Point: MCP Server

Exact locations in `handle_call_tool()` (line numbers from current `main`):

| Tool(s) | Line | Integration point |
|---------|------|-------------------|
| `send_email` | 154-155 | After `_handle_send_email()` returns, before `return result` |
| `check_inbox` | 160-184 | After building result list, before `return` |
| All temporal tools | 187-190 | After `temporal_handlers.dispatch()` returns, before `return` |
| All spatial tools | 193-196 | After `spatial_handlers.dispatch()` returns, before `return` |

**Pattern for each:**
```python
# [existing tool dispatch code]
# [NEW] Record to Mem0 (best-effort, never blocks)
try:
    memory_recorder.record_tool_call(tool_name, arguments, parsed_result, agent_id)
except Exception:
    pass  # Memory recording failure never surfaces to caller
# return result
```

### send_email — special handling

`_handle_send_email()` returns `list[TextContent]`, not a dict. Need to parse:

```python
if name == "send_email":
    result_content = await _handle_send_email(arguments, config, rate_limiter, redis_client)
    try:
        parsed = json.loads(result_content[0].text)
        memory_recorder.record_tool_call("send_email", arguments, parsed, agent_id)
    except Exception:
        pass
    return result_content
```

### check_inbox — special handling

check_inbox builds its result inline in `handle_call_tool()`. The result dict is already
constructed, just pass it:

```python
if name == "check_inbox":
    # [existing code builds result_data list]
    result_dict = {"messages": result_data, "count": len(result_data), "unread_only": unread_only}
    result = [TextContent(type="text", text=json.dumps(result_dict))]
    try:
        memory_recorder.record_tool_call("check_inbox", arguments, result_dict, agent_id)
    except Exception:
        pass
    return result
```

---

## 4. Fact Formatting Rules

### What gets recorded

| Criteria | Tools | Rationale |
|----------|-------|-----------|
| ✅ Write operations | send_email, schedule_event, cancel_event, set_timer_alarm, nominate_milestone | Agent performed an action with side effects |
| ✅ Action spatial tools | browse_to, interact | Agent navigated or interacted — a presence footprint |
| ⚠️ Conditional | check_inbox | Only when unread messages found |
| ⚠️ Conditional | resolve_conflicts | Only when conflicts were actually moved |
| ❌ Read-only | get_temporal_context, extract_page_state | No new information — would create noise |

### Fact format conventions

- **One sentence per fact** — Mem0 extraction works best with concise, declarative facts
- **Include key parameters** — to, subject, start_time, title, URL, action
- **Include outcome** — sent/cancelled/scheduled/failed
- **Skip errors** — failed tool calls are not recorded (noise)
- **Skip oversized data** — SDOM content, full email bodies, long descriptions are truncated or excluded

### Example facts

```
"Sent email to sarah@example.com: 'Q3 deployment timeline'."
"Scheduled 'Team standup' for 2026-06-23T09:00:00Z (low flexibility). 1 conflict(s) resolved."
"Set timer 'Check build status' — fires in ~30 min."
"Recorded decision: 'Migrate to PostgreSQL 17' at 2026-06-20."
"Browsed to https://github.com/nousresearch/hermes-agent."
"Typed 'deployment config' into search-box."
"Checked inbox: 2 unread message(s) from sarah@example.com, github-notifications."
```

---

## 5. Configuration

### Config file: `/etc/parousia/mem0.yaml`

```yaml
mode: local
user_id_prefix: parousia-
vector_store_host: 192.168.101.42
vector_store_port: 6333
llm_provider: lmstudio
llm_model: qwen2.5-coder-3b-instruct
llm_base_url: http://192.168.101.23:1234/v1
llm_temperature: 0.1
embedder_provider: lmstudio
embedder_model: text-embedding-nomic-embed-text-v1.5
embedder_base_url: http://192.168.101.23:1234/v1
```

### AWS deployment note

On the AWS instance (32.197.57.145), the config file lives at the same path.
The Clubhouse IP (192.168.101.42) must be reachable from the AWS instance.
If not, Qdrant would need to run on the AWS instance itself (single binary,
~74MB RAM).

### Dependency

`mem0ai` and `qdrant-client` must be installed in the Parousia Python environment:

```bash
pip install mem0ai qdrant-client pyyaml
```

---

## 6. QA Test Suite

### 6.1 Unit Tests — `tests/test_memory_recorder.py`

Test the fact formatters and recorder in isolation with mocked Mem0.

| # | Test | What it verifies |
|---|------|------------------|
| 1 | `test_fmt_send_email_success` | Formats sent email fact correctly |
| 2 | `test_fmt_send_email_failure` | Formats failed email fact |
| 3 | `test_fmt_send_email_approval` | Formats queued-for-approval fact |
| 4 | `test_fmt_check_inbox_with_unread` | Returns fact when unread messages exist |
| 5 | `test_fmt_check_inbox_empty` | Returns None when inbox is empty |
| 6 | `test_fmt_check_inbox_all_read` | Returns None when all messages read |
| 7 | `test_fmt_schedule_event` | Formats scheduled event with conflict count |
| 8 | `test_fmt_schedule_event_no_conflicts` | Formats event without conflicts |
| 9 | `test_fmt_cancel_event` | Formats cancelled event fact |
| 10 | `test_fmt_set_timer_alarm` | Formats timer fact with minutes remaining |
| 11 | `test_fmt_nominate_milestone` | Formats milestone fact with occurred_at |
| 12 | `test_fmt_resolve_conflicts` | Formats conflict resolution fact |
| 13 | `test_fmt_get_temporal_context_skipped` | Returns None (read-only tool) |
| 14 | `test_fmt_browse_to` | Formats browse fact with URL |
| 15 | `test_fmt_browse_to_error_skipped` | Returns None on browse error |
| 16 | `test_fmt_interact_click` | Formats click interaction fact |
| 17 | `test_fmt_interact_type` | Formats type interaction fact |
| 18 | `test_fmt_interact_error_skipped` | Returns None on interaction error |
| 19 | `test_fmt_extract_page_state_skipped` | Returns None (read-only) |
| 20 | `test_unknown_tool_skipped` | recorder.record_tool_call() exits cleanly for unknown tool |
| 21 | `test_recorder_uses_prefixed_user_id` | Mem0 user_id includes `parousia-` prefix |
| 22 | `test_recorder_background_thread` | Writes happen on daemon thread, not caller thread |
| 23 | `test_recorder_fire_and_forget` | record_tool_call() returns immediately |
| 24 | `test_circuit_breaker_opens` | After 5 failures, writes are skipped |
| 25 | `test_circuit_breaker_resets` | After cooldown, writes resume |
| 26 | `test_recorder_shutdown` | Pending writes complete before shutdown |

### 6.2 Integration Tests — `tests/test_mcp_memory_integration.py`

Test the MCP server integration with a mock Mem0 recorder.

| # | Test | What it verifies |
|---|------|------------------|
| 1 | `test_send_email_records_fact` | send_email → fact recorded |
| 2 | `test_schedule_event_records_fact` | schedule_event → fact recorded |
| 3 | `test_check_inbox_records_fact` | check_inbox with unread → fact recorded |
| 4 | `test_check_inbox_empty_no_record` | check_inbox empty → no fact recorded |
| 5 | `test_get_temporal_context_no_record` | Read-only tool → no fact recorded |
| 6 | `test_memory_failure_doesnt_block_tool` | Mem0 down → tool still returns success |
| 7 | `test_all_11_tools_dispatch` | Every tool in the registry still works after integration |
| 8 | `test_agent_id_prefixed` | Facts use `parousia-{agent_id}` user_id |

### 6.3 Functional Verification Checklist

Run these against either the AWS deployment or a local Parousia instance
with real Clubhouse Qdrant.

#### Phase 1: Prerequisites

- [ ] `pip install mem0ai qdrant-client pyyaml` succeeds
- [ ] `/etc/parousia/mem0.yaml` exists and is valid YAML
- [ ] `curl http://192.168.101.42:6333/health` returns 200
- [ ] `curl http://192.168.101.23:1234/v1/embeddings -d '{"model":"text-embedding-nomic-embed-text-v1.5","input":"test"}'` returns embeddings

#### Phase 2: Smoke test — record a fact

```bash
# Start Parousia MCP server and send a single tool call
echo '{"method":"tools/call","params":{"name":"send_email","arguments":{"to":"test@example.com","subject":"Mem0 integration test","body":"Testing."}}}' | parousia-guard serve --mcp
```

- [ ] Tool call succeeds (email sent or queued)
- [ ] No errors in Parousia logs about Mem0
- [ ] Check Clubhouse Qdrant: fact is searchable

#### Phase 3: Cross-tool synthesis

Via Hermes with Parousia MCP tools:
1. `send_email(to="sarah@example.com", subject="Deploy Friday", body="...")`
2. `schedule_event(title="Deploy Friday", start_time="2026-06-27T14:00:00Z")`
3. `nominate_milestone(title="v0.3.0 shipped", occurred_at="2026-06-20")`
4. `mem0_search(query="deployment timeline")`

- [ ] Search returns synthesized fact mentioning both email and event
- [ ] Search returns milestone fact with correct date
- [ ] `mem0_profile` shows all 3 facts

#### Phase 4: Resilience

- [ ] Stop Qdrant → tool calls still succeed → circuit breaker opens after 5 failures
- [ ] Start Qdrant → circuit breaker resets after cooldown → new facts recorded
- [ ] Parousia restart → facts from before restart still searchable
- [ ] Multiple agents (hermes, claude) → facts are namespaced correctly

#### Phase 5: Existing test regression

- [ ] `python -m pytest tests/ -x -q` — all 32 existing test files pass
- [ ] `python -m pytest tests/test_memory_recorder.py tests/test_mcp_memory_integration.py -v` — all new tests pass

---

## 7. Functional Verification

### 7.1 Automated verification script

```bash
#!/bin/bash
# verify_mem0_integration.sh — run after deployment
set -e

echo "=== 1. Prerequisites ==="
python3 -c "from mem0 import Memory; print('mem0ai OK')"
python3 -c "import yaml; print('pyyaml OK')"
curl -sf http://192.168.101.42:6333/health > /dev/null && echo "Qdrant OK"

echo "=== 2. Config ==="
python3 -c "
from parousia.memory.config import Mem0Config
cfg = Mem0Config.from_file()
print(f'mode={cfg.mode} qdrant={cfg.vector_store_host}:{cfg.vector_store_port}')
assert cfg.mode == 'local'
print('Config OK')
"

echo "=== 3. Fact formatting ==="
python3 -c "
from parousia.memory.recorder import _FACT_FORMATTERS
tools = sorted(_FACT_FORMATTERS.keys())
print(f'{len(tools)} tools with formatters: {tools}')
assert 'send_email' in tools
assert 'schedule_event' in tools
assert 'browse_to' in tools
assert 'get_temporal_context' in tools  # registered, but returns None
print('Formatters OK')
"

echo "=== 4. Write + search a fact ==="
python3 -c "
from parousia.memory.recorder import MemoryRecorder
import time, json

recorder = MemoryRecorder()
recorder.record_tool_call(
    'nominate_milestone',
    {'title': 'QA verification milestone', 'entry_type': 'milestone', 'occurred_at': '2026-06-22'},
    {'recorded': True, 'journal_id': 'qa:j1', 'title': 'QA verification milestone'},
    'qa-test'
)
time.sleep(3)  # Wait for extraction
recorder.shutdown()

# Now search via the Hermes plugin
import os, sys
os.environ['HERMES_HOME'] = '/tmp/parousia-mem0-verify'
sys.path.insert(0, '/home/sblanken/.hermes/hermes-agent')
from plugins.memory.mem0 import Mem0MemoryProvider
provider = Mem0MemoryProvider()
provider._mode = 'local'
provider._config = {
    'mode': 'local', 'user_id': 'parousia-qa-test', 'agent_id': 'parousia',
    'vector_store_provider': 'qdrant', 'vector_store_host': '192.168.101.42',
    'vector_store_port': 6333, 'embedding_model_dims': 768,
    'llm_provider': 'lmstudio', 'llm_model': 'qwen2.5-coder-3b-instruct',
    'llm_base_url': 'http://192.168.101.23:1234/v1', 'llm_temperature': 0.1,
    'embedder_provider': 'lmstudio', 'embedder_model': 'text-embedding-nomic-embed-text-v1.5',
    'embedder_base_url': 'http://192.168.101.23:1234/v1',
}
provider.initialize('verify-session', user_id='parousia-qa-test')
result = json.loads(provider.handle_tool_call('mem0_search', {'query': 'QA verification'}))
print(f'Search results: {result.get(\"count\", 0)}')
assert result.get('count', 0) > 0, 'QA milestone not found in search!'
print('Write + search: PASSED')
provider.shutdown()
"

echo "=== 5. Fire-and-forget ==="
python3 -c "
from parousia.memory.recorder import MemoryRecorder
import time

recorder = MemoryRecorder()
start = time.monotonic()
recorder.record_tool_call(
    'send_email',
    {'to': 'test@example.com', 'subject': 'Perf test'},
    {'sent': True, 'message_id': 'test-123'},
    'perf-test'
)
elapsed = time.monotonic() - start
print(f'record_tool_call() returned in {elapsed*1000:.1f}ms')
assert elapsed < 0.5, f'Too slow: {elapsed*1000:.0f}ms (must be <500ms)'
print('Fire-and-forget: PASSED')
recorder.shutdown()
"

echo "=== ALL VERIFICATIONS PASSED ==="
```

### 7.2 Manual QA checklist

| # | Check | Command / Action | Expected |
|---|-------|-----------------|----------|
| 1 | MCP tools still work | Call each of the 11 tools via MCP | All return success (except rate-limited email) |
| 2 | Mem0 writes silently on failure | Stop Qdrant, call a tool 6 times | Tool succeeds each time, no errors surfaced |
| 3 | Facts are agent-scoped | Call tools as hermes and claude, search each | Each agent only sees their own facts |
| 4 | Existing test suite passes | `pytest tests/ -x -q` | 0 failures |
| 5 | Parousia version bumped | `grep version src/parousia/__init__.py` | `0.3.0` |

---

## 8. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Mem0 extraction LLM doesn't support structured outputs | High | Low | Facts are already natural language — extraction is nice-to-have. Search still works via embeddings. |
| Clubhouse unreachable from AWS Parousia instance | Medium | Medium | Qdrant is 74MB — can run on the AWS instance as fallback. Or use file-backed Qdrant at `/var/lib/parousia/qdrant/`. |
| Memory growth over time (unbounded facts) | Medium | Low | Qdrant handles millions of vectors. If needed, add `max_facts_per_agent` config with LRU eviction in v0.4. |
| Thread safety with concurrent MCP tool calls | Low | Medium | Single daemon thread serializes writes. If concurrent calls arrive, `join(timeout=5.0)` on previous thread prevents pileup. |
| mem0ai version incompatibility | Low | Low | Pin `mem0ai>=2.0.7,<3.0` in requirements. Tested with 2.0.7. |
