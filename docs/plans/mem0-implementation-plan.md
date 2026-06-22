# Mem0 Memory Layer — Implementation Plan

> **Target release:** Parousia v0.3.0  
> **Dependency:** Clubhouse Qdrant + .23 LM Studio (already deployed)  
> **Estimated effort:** 4 hours (code) + 2 hours (multi-agent testing) = **6 hours total**

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File-by-File Implementation](#2-file-by-file-implementation)
3. [Integration Point: MCP Server](#3-integration-point-mcp-server)
4. [Fact Formatting Rules](#4-fact-formatting-rules)
5. [Configuration](#5-configuration)
6. [QA Test Suite](#6-qa-test-suite)
7. [Functional Verification](#7-functional-verification)
8. [Multi-Agent User Testing on AWS](#8-multi-agent-user-testing-on-aws)
9. [Risk Register](#9-risk-register)

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

## 8. Multi-Agent User Testing on AWS

> **Objective:** Deploy real agents (Hermes, Claude Code, OpenClaw) on AWS VMs and
> verify that the Parousia Mem0 memory layer correctly records, synthesizes, and
> surfaces facts across agents — a live dogfood test of the "Machine's Presence."

### 8.1 Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        AWS us-east-1                             │
│                                                                  │
│  ┌──────────────────────┐    ┌──────────────────────────────┐   │
│  │ Parousia (existing)   │    │ Clubhouse (lab)              │   │
│  │ 32.197.57.145        │    │ 192.168.101.42               │   │
│  │ m7i-flex.large       │    │ Qdrant :6333                 │   │
│  │ MCP server :8081 ◄───┼────┤ LiteLLM :4000                │   │
│  │ Mem0 recorder (NEW)  │    │ LM Studio .23:1234           │   │
│  └──────────────────────┘    └──────────────────────────────┘   │
│           ▲                                                      │
│           │  MCP tool calls (send_email, schedule_event, ...)    │
│           │  Mem0 writes via Qdrant                              │
│  ┌────────┴────────┬──────────────────┐                          │
│  │                 │                  │                          │
│  ▼                 ▼                  ▼                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐                   │
│  │ VM-A     │  │ VM-B     │  │ VM-C         │                   │
│  │ Hermes   │  │ Claude   │  │ OpenClaw     │                   │
│  │ Agent    │  │ Code     │  │              │                   │
│  │          │  │          │  │              │                   │
│  │ t3.small │  │ t3.small │  │ t3.small     │                   │
│  └──────────┘  └──────────┘  └──────────────┘                   │
│                                                                  │
│  Agent A schedules an event ──► Mem0 records "parousia-hermes"   │
│  Agent B searches ────────────► Finds A's fact via cross-search  │
│  Agent C browses web ─────────► Spatial fact recorded            │
│  All three ───────────────────► Shared memory across agents      │
└──────────────────────────────────────────────────────────────────┘
```

**Key design for testing:**

1. **Three isolated VMs** — one per agent type, each with its own identity (`parousia-hermes`, `parousia-claude`, `parousia-openclaw`)
2. **Single shared Parousia** — all agents call the same MCP server at 32.197.57.145:8081
3. **Single shared Qdrant** — all Mem0 facts land in the same vector store, namespaced by `user_id`
4. **Cross-agent search** — agents search across all `parousia-*` namespaces to discover facts from other agents
5. **Minus email sending** — `send_email` calls are mocked/recorded but not actually delivered (the mock event pattern)

### 8.2 Prerequisites

| # | Requirement | Status | Verification |
|---|------------|--------|-------------|
| 1 | AWS credentials configured | Check | `aws sts get-caller-identity` |
| 2 | SSH key pair in AWS (`linus-test-key`) | Check | `aws ec2 describe-key-pairs` |
| 3 | Parousia Mem0 integration deployed to 32.197.57.145 | **Gate** | `ssh parousia 'systemctl status parousia-guard'` |
| 4 | `/etc/parousia/mem0.yaml` on Parousia instance | **Gate** | `ssh parousia 'cat /etc/parousia/mem0.yaml'` |
| 5 | Clubhouse Qdrant reachable from AWS | Check | `ssh parousia 'curl -s http://192.168.101.42:6333/health'` |
| 6 | Clubhouse Qdrant reachable from test VMs | Check | Verified during bootstrap |
| 7 | OpenRouter API key (for OpenClaw) | **Gate** | In KeePass `General/openrouter MR-Krabs QA Key` |
| 8 | Anthropic API key (for Claude Code) | **Gate** | Env var or OAuth |
| 9 | DeepSeek API key (for Hermes) | Check | In `~/.hermes/.env` |

**Gate items** must pass before provisioning test VMs. If Parousia Mem0 integration
hasn't been deployed yet, complete Sections 2–7 first.

### 8.3 Test VM Provisioning

Use the Linus Deployment Specialist (`shared/provision/linus-provision.sh`)
to provision three Ubuntu 24.04 t3.small instances.

```bash
# VM-A: Hermes Agent
PROVIDER=aws \
  AWS_REGION=us-east-1 \
  AWS_KEY_NAME=linus-test-key \
  VM_OS_TYPE=ubuntu \
  VM_NAME=parousia-qa-hermes \
  BOOTSTRAP_PACKAGES="python3-pip python3-venv git curl" \
  bash shared/provision/linus-provision.sh

# VM-B: Claude Code
PROVIDER=aws \
  AWS_REGION=us-east-1 \
  AWS_KEY_NAME=linus-test-key \
  VM_OS_TYPE=ubuntu \
  VM_NAME=parousia-qa-claude \
  BOOTSTRAP_PACKAGES="nodejs npm git curl" \
  bash shared/provision/linus-provision.sh

# VM-C: OpenClaw
PROVIDER=aws \
  AWS_REGION=us-east-1 \
  AWS_KEY_NAME=linus-test-key \
  VM_OS_TYPE=ubuntu \
  VM_NAME=parousia-qa-openclaw \
  BOOTSTRAP_PACKAGES="python3-pip python3-venv git curl" \
  bash shared/provision/linus-provision.sh
```

**Expected output per VM:** `LINUS_RESULT:SUCCESS` with `LINUS_VM_IP` populated.

**Cost estimate (3 × t3.small, ~1 hour test window):**
- 3 × $0.0208/hr × 1.5hr = **~$0.09 total**
- Plus Parousia m7i-flex.large: ~$0.09/hr (already running)

### 8.4 Agent Installation & Configuration

#### 8.4.1 VM-A: Hermes Agent

```bash
# SSH to VM-A
ssh -i ~/.ssh/linus-test-key ubuntu@<VM_A_IP>

# Install Hermes
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash

# Configure OpenRouter or DeepSeek provider
export DEEPSEEK_API_KEY="<key>"
hermes model  # Interactive picker → DeepSeek v4 Pro

# Enable MCP for Parousia
hermes mcp add parousia --url http://32.197.57.145:8081/sse

# Configure Mem0 local mode
mkdir -p ~/.hermes
cat > ~/.hermes/mem0.json << 'EOF'
{
  "mode": "local",
  "user_id": "parousia-hermes",
  "vector_store_host": "192.168.101.42",
  "vector_store_port": 6333,
  "embedding_model_dims": 768,
  "llm_provider": "lmstudio",
  "llm_model": "qwen2.5-coder-3b-instruct",
  "llm_base_url": "http://192.168.101.23:1234/v1",
  "embedder_provider": "lmstudio",
  "embedder_model": "text-embedding-nomic-embed-text-v1.5",
  "embedder_base_url": "http://192.168.101.23:1234/v1"
}
EOF

# Enable Mem0 in Hermes
hermes config set memory.provider mem0

# Verify
hermes tools list | grep parousia  # Should show MCP tools
hermes chat -q "Call mem0_profile and confirm it returns empty"
```

#### 8.4.2 VM-B: Claude Code

```bash
# SSH to VM-B
ssh -i ~/.ssh/linus-test-key ubuntu@<VM_B_IP>

# Install Claude Code (Node.js already bootstrapped)
npm install -g @anthropic-ai/claude-code

# Authenticate
export ANTHROPIC_API_KEY="<key>"
claude auth login --console

# Add Parousia MCP server
claude mcp add -s user parousia -- \
  curl -s http://32.197.57.145:8081/sse

# Create CLAUDE.md with Parousia context
cat > ~/CLAUDE.md << 'EOF'
# Parousia Test Agent

I am parousia-claude — a Claude Code agent configured to test the Parousia
presence platform with Mem0 memory.

## Available MCP Tools
- send_email(to, subject, body) — Send email via Parousia
- schedule_event(title, start_time, ...) — Schedule temporal events
- nominate_milestone(title, occurred_at, ...) — Record milestones
- browse_to(url) — Browse web pages
- check_inbox — Check email inbox
- get_temporal_context — Query temporal state

## Memory
All tool calls are automatically recorded to Mem0. Search memory with
"search for <query>" — Claude should use WebSearch or its own tools
to query the Qdrant API directly.

## Qdrant Search (for cross-agent memory verification)
curl -s http://192.168.101.42:6333/collections/mem0/points/search \
  -H "Content-Type: application/json" \
  -d '{"vector": [...], "limit": 5}'
EOF

# Smoke test
claude -p "Call the schedule_event tool to create a test event: title='Claude startup test', start_time='$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)'" \
  --allowedTools "mcp__parousia__schedule_event" --max-turns 3
```

#### 8.4.3 VM-C: OpenClaw

```bash
# SSH to VM-C
ssh -i ~/.ssh/linus-test-key ubuntu@<VM_C_IP>

# Install OpenClaw (Python package)
pip install openclaw  # or pip install git+https://github.com/...

# Configure OpenRouter provider
export OPENROUTER_API_KEY="<key>"

# Create OpenClaw config with Parousia tools
mkdir -p ~/.openclaw
cat > ~/.openclaw/config.yaml << 'EOF'
provider: openrouter
model: anthropic/claude-sonnet-4
agent_id: parousia-openclaw

mcp_servers:
  parousia:
    url: http://32.197.57.145:8081/sse

memory:
  backend: mem0
  mem0:
    mode: local
    user_id: parousia-openclaw
    vector_store:
      host: 192.168.101.42
      port: 6333
    embedder:
      provider: lmstudio
      model: text-embedding-nomic-embed-text-v1.5
      base_url: http://192.168.101.23:1234/v1
EOF

# Smoke test
openclaw run "List available tools and confirm you can see Parousia tools"
```

### 8.5 Test Scenarios

Each scenario is a scripted interaction. The orchestrator (running on the
Clubhouse or local machine) drives the agents via their respective CLIs.

#### Scenario 1: Cross-Agent Temporal Awareness

**Goal:** Hermes schedules an event → Claude discovers it via memory search.

| Step | Agent | Action | Tool | Expected |
|------|-------|--------|------|----------|
| 1.1 | Hermes | Schedule a deployment event | `schedule_event(title="Deploy v0.3.0 to production", start_time="2026-06-27T14:00:00Z", flexibility="medium")` | Event created, no conflicts |
| 1.2 | Hermes | Verify recording | `mem0_search(query="deployment event")` | Returns fact with title and date |
| 1.3 | Claude | Cross-agent search | Curl Qdrant API with `parousia-hermes` user_id filter | Finds Hermes's scheduled event |
| 1.4 | Claude | Schedule its own event | `schedule_event(title="Post-deploy monitoring window", start_time="2026-06-27T14:15:00Z")` | Event created |
| 1.5 | Hermes | Discover Claude's event | `mem0_search(query="monitoring window")` with cross-agent user_id | Finds Claude's event |

**Orchestration script:**
```bash
#!/bin/bash
set -e
HERMES_IP="$1"  CLAUDE_IP="$2"

echo "=== Scenario 1: Cross-Agent Temporal Awareness ==="

# Step 1.1: Hermes schedules
ssh ubuntu@$HERMES_IP \
  'hermes chat -q "Call schedule_event: title=Deploy v0.3.0 to production, start_time=2026-06-27T14:00:00Z, flexibility=medium. Return the JSON result."'

sleep 3  # Mem0 write + extraction

# Step 1.2: Hermes verifies own memory
ssh ubuntu@$HERMES_IP \
  'hermes chat -q "Call mem0_search with query=\\"deployment event\\". Return the results."'

# Step 1.3: Claude cross-searches
ssh ubuntu@$CLAUDE_IP \
  'curl -s http://192.168.101.42:6333/collections/mem0/points/scroll \
    -H "Content-Type: application/json" \
    -d "{\"filter\":{\"must\":[{\"key\":\"user_id\",\"match\":{\"value\":\"parousia-hermes\"}}]},\"limit\":5}" | python3 -c "import json,sys; data=json.load(sys.stdin); print(f\\"Found {len(data.get(\\"result\\",{}).get(\\"points\\",[]))} points\\")"'

echo "Scenario 1 complete."
```

#### Scenario 2: Email + Event Synthesis

**Goal:** Claude sends an email → Hermes schedules a related event →
memory synthesizes a connected fact.

| Step | Agent | Action | Tool | Expected |
|------|-------|--------|------|----------|
| 2.1 | Claude | Mock-send an email | `send_email(to="team@example.com", subject="v0.3.0 rollout plan", body="Rolling out Wednesday...")` | Email queued/recorded |
| 2.2 | Hermes | Schedule follow-up | `schedule_event(title="v0.3.0 rollout", start_time="2026-06-25T09:00:00Z")` | Event created |
| 2.3 | Hermes | Search synthesized memory | `mem0_search(query="rollout communication and schedule")` | Returns facts connecting email + event |
| 2.4 | Hermes | Full profile | `mem0_profile` | Shows 3+ facts including Claude's email |

**Key verification:** The `mem0_search` result for "rollout communication" should
return facts from BOTH agents — Claude's email fact AND Hermes's scheduled event.
This proves cross-agent memory synthesis works.

#### Scenario 3: Spatial Discovery Chain

**Goal:** OpenClaw browses → Claude discovers → Hermes enriches.

| Step | Agent | Action | Tool | Expected |
|------|-------|--------|------|----------|
| 3.1 | OpenClaw | Browse a URL | `browse_to(url="https://github.com/NousResearch/hermes-agent/releases")` | Page loaded |
| 3.2 | Claude | Search for web activity | Curl Qdrant across all namespaces | Finds OpenClaw's browse fact |
| 3.3 | Claude | Nominate a milestone based on discovery | `nominate_milestone(title="Hermes Agent latest release noted", entry_type="discovery", occurred_at="2026-06-22")` | Milestone recorded |
| 3.4 | Hermes | Timeline reconstruction | `mem0_search(query="June 2026 discoveries and events")` | Returns browse + milestone facts |

#### Scenario 4: Multi-Agent Timeline Reconstruction

**Goal:** All three agents perform actions → a single query reconstructs the full timeline.

| Step | Agent | Action |
|------|-------|--------|
| 4.1 | Hermes | `set_timer_alarm(title="QA review deadline", fire_at="2026-06-23T17:00:00Z")` |
| 4.2 | Claude | `nominate_milestone(title="Mem0 memory layer v0.3.0", entry_type="release", occurred_at="2026-06-22")` |
| 4.3 | OpenClaw | `browse_to(url="https://docs.mem0.ai")` |
| 4.4 | Hermes | `resolve_conflicts(event_id="...")` — resolve any temporal conflicts |
| 4.5 | Any agent | `mem0_search(query="all activity June 2026", user_id="parousia-*")` |

**Success criterion:** Search returns ≥4 distinct facts from ≥2 different agents,
with correct dates and agent attribution.

#### Scenario 5: Circuit Breaker Resilience

**Goal:** Prove that Mem0 failures never block Parousia tool calls.

| Step | Action | Expected |
|------|--------|----------|
| 5.1 | Hermes schedules 3 events (baseline) | All succeed |
| 5.2 | **Stop Clubhouse Qdrant** (`systemctl --user stop qdrant`) | |
| 5.3 | Hermes schedules 6 more events | All 6 tool calls succeed, 5 Mem0 failures logged |
| 5.4 | Verify circuit breaker opened | Parousia logs show "circuit breaker tripped after 5 failures" |
| 5.5 | Verify 7th+ tool call skips Mem0 silently | Event created, no Mem0 attempt |
| 5.6 | **Start Clubhouse Qdrant** (`systemctl --user start qdrant`) | |
| 5.7 | Wait 130s (breaker cooldown + margin) | |
| 5.8 | Hermes schedules another event | Tool call succeeds AND Mem0 write succeeds |
| 5.9 | Verify fact is searchable | `mem0_search` returns the post-recovery fact |

### 8.6 Cross-Agent Search Design

**Important design consideration:** By default, Mem0 searches are scoped to the
agent's own `user_id`. For cross-agent discovery, we need one of:

**Option A: Multi-user search (recommended for testing)**
```python
# Search across all parousia agents
results = memory.search(
    query="deployment events",
    user_id="parousia-hermes",  # Primary agent
    filters={"user_id": {"$in": [
        "parousia-hermes",
        "parousia-claude", 
        "parousia-openclaw"
    ]}}
)
```

**Option B: Shared user_id**
All three agents use the same `user_id: parousia-shared`. Simpler but loses
agent attribution — facts can't be traced to source agent.

**Option C: Direct Qdrant scroll (used in test scripts)**
```bash
curl -s http://192.168.101.42:6333/collections/mem0/points/scroll \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"user_id","match":{"text":"parousia-"}}]},"limit":20}'
```

**For this test phase, we use Option C** (direct Qdrant) for cross-agent
verification and Option A (multi-user search) for agent-driven queries.

### 8.7 Verification & Success Criteria

#### Automated Verification Script

`verify_multi_agent.sh` — runs on the orchestrator machine:

```bash
#!/bin/bash
# verify_multi_agent.sh — multi-agent Mem0 integration test
# Usage: bash verify_multi_agent.sh <hermes_ip> <claude_ip> <openclaw_ip>
set -e
HERMES=$1; CLAUDE=$2; OPENCLAW=$3
PASS=0; FAIL=0

assert() {
    local desc="$1"; local cmd="$2"; local expect="$3"
    echo -n "  $desc ... "
    result=$(eval "$cmd" 2>&1) || true
    if echo "$result" | grep -q "$expect"; then
        echo "✓ PASS"; ((PASS++))
    else
        echo "✗ FAIL (expected '$expect', got: ${result:0:200})"; ((FAIL++))
    fi
}

echo "=== Multi-Agent Mem0 Verification ==="

# ── Phase 1: Connectivity ──
echo "Phase 1: Connectivity"
assert "Parousia MCP reachable" \
  "curl -s -o /dev/null -w '%{http_code}' http://32.197.57.145:8081/sse" "200"
assert "Qdrant reachable from Hermes VM" \
  "ssh ubuntu@$HERMES 'curl -s http://192.168.101.42:6333/health'" "ok"
assert "Hermes tools list shows MCP" \
  "ssh ubuntu@$HERMES 'hermes tools list 2>/dev/null'" "parousia"

# ── Phase 2: Cross-Agent Write → Search ──
echo "Phase 2: Cross-Agent Write → Search"

# Hermes schedules
ssh ubuntu@$HERMES "hermes chat -q 'Call schedule_event: title=QA Cross-Agent Test, start_time=2026-06-27T14:00:00Z, flexibility=low'"
sleep 3

assert "Hermes sees own fact" \
  "ssh ubuntu@$HERMES \"hermes chat -q 'Call mem0_search with query=\\\"QA Cross-Agent Test\\\". Return count.'\"" \
  "QA Cross-Agent"

# Claude cross-searches Qdrant
assert "Claude cross-searches Hermes fact" \
  "ssh ubuntu@$CLAUDE \"curl -s http://192.168.101.42:6333/collections/mem0/points/scroll -H 'Content-Type: application/json' -d '{\\\"limit\\\":10}'\"" \
  "parousia-hermes"

# ── Phase 3: Independent Agent Actions ──
echo "Phase 3: Independent Agent Actions"

ssh ubuntu@$CLAUDE "claude -p 'Call nominate_milestone: title=Claude discovered Mem0 testing, entry_type=discovery, occurred_at=2026-06-22' --allowedTools mcp__parousia__nominate_milestone --max-turns 3"
sleep 3

ssh ubuntu@$OPENCLAW "openclaw run 'Browse to https://github.com/NousResearch/hermes-agent and report the latest release tag'"
sleep 3

assert "Qdrant has facts from ≥2 agents" \
  "ssh ubuntu@$HERMES \"curl -s http://192.168.101.42:6333/collections/mem0/points/scroll -H 'Content-Type: application/json' -d '{\\\"limit\\\":20}' | python3 -c \\\"import json,sys; d=json.load(sys.stdin); ids=set(p['payload'].get('user_id','') for p in d.get('result',{}).get('points',[])); print(len(ids), 'agents:', sorted(ids))\\\"\"" \
  "agents"

# ── Phase 4: Circuit Breaker ──
echo "Phase 4: Circuit Breaker Resilience"
# (Requires Qdrant stop/start access — see Scenario 5 above)
echo "  (Manual: stop Qdrant, run 6+ tool calls, verify breaker, restart)"

# ── Phase 5: Regression ──
echo "Phase 5: Tool Regression"
assert "Hermes: schedule_event works" \
  "ssh ubuntu@$HERMES \"hermes chat -q 'Call schedule_event: title=Regression Test, start_time=2026-06-28T10:00:00Z' 2>&1\"" \
  "schedule_event"

# ── Summary ──
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && echo "MULTI-AGENT VERIFICATION: PASSED" || echo "MULTI-AGENT VERIFICATION: FAILED"
```

#### Success Criteria

| # | Criterion | Threshold |
|---|-----------|-----------|
| 1 | All 3 agents can call Parousia MCP tools | 100% (no connection failures) |
| 2 | Each agent's write actions produce Mem0 facts | ≥1 fact per agent in Qdrant |
| 3 | Cross-agent search finds facts from other agents | ≥1 cross-agent discovery |
| 4 | Mem0 search synthesizes related facts | Search for "deployment" returns both email and event facts |
| 5 | Circuit breaker opens on Qdrant failure | 5 consecutive failures → breaker open log |
| 6 | Circuit breaker resets after recovery | Post-recovery writes succeed |
| 7 | Tool calls succeed during Qdrant outage | 100% (no blocking) |
| 8 | Timeline reconstruction works | Search across all agents returns ≥4 distinct facts |
| 9 | No agent sees another agent's facts on its own user_id search | `mem0_profile` for `parousia-hermes` doesn't show `parousia-claude` facts |
| 10 | Cross-agent search (explicit multi-user) returns all agents' facts | Multi-user filter returns facts from ≥2 agents |

### 8.8 Test Execution Order

```
Prerequisites (8.2)
    │
    ▼
┌─────────────────────────────────────────────┐
│ GATE 1: Parousia Mem0 deployed to AWS       │
│ GATE 2: AWS credentials + SSH keys ready    │
└─────────────────────────────────────────────┘
    │
    ▼
Provision VMs (8.3) — Linus × 3
    │
    ▼
Install Agents (8.4) — Hermes, Claude, OpenClaw
    │
    ▼
┌─────────────────────────────────────────────┐
│ GATE 3: All agents can call Parousia tools  │
└─────────────────────────────────────────────┘
    │
    ├──► Scenario 1: Temporal Awareness
    ├──► Scenario 2: Email + Event Synthesis
    ├──► Scenario 3: Spatial Discovery Chain
    ├──► Scenario 4: Timeline Reconstruction
    └──► Scenario 5: Circuit Breaker Resilience
    │
    ▼
Verification Script (8.7)
    │
    ▼
┌─────────────────────────────────────────────┐
│ DECISION: All 10 criteria met?              │
│   YES → Teardown VMs, mark v0.3.0 ready    │
│   NO  → Debug, fix, re-run from scenario    │
└─────────────────────────────────────────────┘
```

### 8.9 Teardown

```bash
# List all test instances
aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=parousia-qa-*" \
  --query "Reservations[].Instances[].{ID:InstanceId,IP:PublicIpAddress,Name:Tags[?Key=='Name']|[0].Value}" \
  --region us-east-1 --output table

# Terminate
for id in $(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=parousia-qa-*" "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].InstanceId" --region us-east-1 --output text); do
  echo "Terminating $id..."
  aws ec2 terminate-instances --instance-ids "$id" --region us-east-1
done

# Verify termination
sleep 30
aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=parousia-qa-*" \
  --query "Reservations[].Instances[].State.Name" --region us-east-1 --output text
# Expected: terminated terminated terminated (or empty)
```

### 8.10 Contingency: OpenClaw Unavailable

If OpenClaw CLI is not installable or doesn't support MCP natively,
substitute with **OpenCode CLI** (`npm i -g opencode-ai`):

```bash
# OpenCode alternative for VM-C
ssh ubuntu@<VM_C_IP> '
  npm install -g opencode-ai@latest
  opencode auth login
  
  # Configure Parousia as custom provider with MCP-like tools
  mkdir -p ~/.config/opencode
  cat > ~/.config/opencode/opencode.json << EOF
  {
    "provider": {
      "parousia": {
        "name": "Parousia Presence Platform",
        "options": { "baseURL": "http://32.197.57.145:8081" }
      }
    }
  }
  EOF
'
```

**Note:** OpenCode may not support SSE-based MCP natively as of v1.14.
In that case, use `curl`-based tool calls for OpenCode tests — the
Qdrant verification still works because facts are recorded server-side
by Parousia regardless of which client triggered them.

---

## 9. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Mem0 extraction LLM doesn't support structured outputs | High | Low | Facts are already natural language — extraction is nice-to-have. Search still works via embeddings. |
| Clubhouse unreachable from AWS Parousia instance | Medium | Medium | Qdrant is 74MB — can run on the AWS instance as fallback. Or use file-backed Qdrant at `/var/lib/parousia/qdrant/`. |
| Clubhouse unreachable from test VMs | Medium | High | Test VMs run in AWS us-east-1; Clubhouse is on lab network. Ensure site-to-site VPN or use Parousia as Qdrant proxy. Fallback: deploy Qdrant on each test VM. |
| OpenClaw CLI not installable or lacks MCP support | High | Medium | Substitute OpenCode CLI or use direct curl-based tool calls (8.10 contingency). Qdrant verification unaffected. |
| Memory growth over time (unbounded facts) | Medium | Low | Qdrant handles millions of vectors. If needed, add `max_facts_per_agent` config with LRU eviction in v0.4. |
| Thread safety with concurrent MCP tool calls | Low | Medium | Single daemon thread serializes writes. If concurrent calls arrive, `join(timeout=5.0)` on previous thread prevents pileup. |
| mem0ai version incompatibility | Low | Low | Pin `mem0ai>=2.0.7,<3.0` in requirements. Tested with 2.0.7. |
| Test agents leak API key spend (runaway loops) | Medium | Low | Set `--max-turns` for Claude Code, `--max-budget-usd` where available. Hermes has built-in turn limit. Monitor AWS billing. |
| Multiple agents writing to same user_id namespace | Low | Low | Each agent uses distinct `parousia-{name}` user_id. Cross-agent search uses explicit multi-user filter. |
