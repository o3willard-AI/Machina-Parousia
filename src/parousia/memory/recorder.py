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
                # Use infer=False when no LLM is configured (embed-only mode)
                infer = bool(self._config.llm_provider)
                memory.add(
                    messages,
                    user_id=mem0_user_id,
                    agent_id="parousia",
                    infer=infer,
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
