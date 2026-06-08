"""MCP tool handler implementations for temporal tools.

get_temporal_context, schedule_event, cancel_event, set_timer_alarm,
nominate_milestone — registered on the existing MCP server (port 8081).
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from parousia.config import ParousiaConfig
from parousia.temporal.db import TemporalDB
from parousia.temporal.serializer import TemporalSerializer

logger = logging.getLogger("parousia.temporal.tools")


# ── Tool schemas ──────────────────────────────────────


def get_temporal_context_schema() -> dict:
    return {
        "name": "get_temporal_context",
        "description": (
            "Return your current temporal context in a token-lean DSL format. "
            "Modes: 'standard' (past 24h + next 3d), 'planning' (next 14d), "
            "'retrospective' (past 7d), 'full' (past/future 30d)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["standard", "planning", "retrospective", "full"],
                    "description": "Temporal window mode. Default: 'standard'.",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID. Auto-detected from session if omitted.",
                },
            },
        },
    }


def schedule_event_schema() -> dict:
    return {
        "name": "schedule_event",
        "description": (
            "Schedule a new calendar event. Returns .ics, Google Calendar API, "
            "and MS Graph API payloads for external sync. When auto_resolve=True "
            "(default), automatically resolves time conflicts by moving flexible events."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "ISO 8601 start datetime"},
                "end_time": {"type": "string", "description": "ISO 8601 end datetime (default: start + 1h)"},
                "flexibility": {"type": "string", "enum": ["high", "low", "none"], "description": "How movable this event is. Default: 'high'."},
                "auto_resolve": {"type": "boolean", "description": "Automatically resolve time conflicts by moving flexible events. Default: true."},
                "stakeholders": {"type": "string", "description": "Comma-separated stakeholder list"},
                "metadata": {"type": "object", "description": "Arbitrary key-value metadata"},
            },
            "required": ["title", "start_time"],
        },
    }


def cancel_event_schema() -> dict:
    return {
        "name": "cancel_event",
        "description": "Cancel an event by its short ID (e.g., 'e3'). Soft-deletes — sets status to 'cancelled'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Short event ID (e.g., 'e3' from your temporal context)"},
            },
            "required": ["event_id"],
        },
    }


def set_timer_alarm_schema() -> dict:
    return {
        "name": "set_timer_alarm",
        "description": (
            "Set a relative timer or an absolute alarm. Must provide exactly one of "
            "'duration_minutes' (timer) or 'trigger_at' (alarm)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What the timer/alarm is for"},
                "duration_minutes": {"type": "integer", "description": "Timer duration in minutes (mutually exclusive with trigger_at)"},
                "trigger_at": {"type": "string", "description": "Absolute alarm trigger time (ISO 8601, mutually exclusive with duration_minutes)"},
            },
            "required": ["title"],
        },
    }


def nominate_milestone_schema() -> dict:
    return {
        "name": "nominate_milestone",
        "description": "Record a research, decision, or shipped milestone in your temporal journal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Milestone title"},
                "description": {"type": "string", "description": "Free-text summary"},
                "entry_type": {"type": "string", "enum": ["milestone", "research", "decision", "shipped"], "description": "Category. Default: 'milestone'."},
                "occurred_at": {"type": "string", "description": "ISO 8601 date or datetime when it happened"},
                "tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": ["title", "occurred_at"],
        },
    }


def resolve_conflicts_schema() -> dict:
    return {
        "name": "resolve_conflicts",
        "description": (
            "Detect and auto-resolve scheduling conflicts. Moves flexible events "
            "to avoid overlaps — never moves flexibility='none' events. "
            "Use this before planning to ensure a clean calendar."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent ID. Auto-detected from session if omitted.",
                },
            },
        },
    }


ALL_TEMPORAL_SCHEMAS = [
    get_temporal_context_schema(),
    schedule_event_schema(),
    cancel_event_schema(),
    set_timer_alarm_schema(),
    nominate_milestone_schema(),
    resolve_conflicts_schema(),
]


# ── Tool handler registry ─────────────────────────────


class TemporalToolHandlers:
    """Handle MCP tool invocations for temporal tools."""

    def __init__(self, config: ParousiaConfig, db: TemporalDB):
        self.config = config
        self.db = db
        self.serializer = TemporalSerializer(db)

    def dispatch(self, name: str, arguments: dict[str, Any], agent_id: str) -> str:
        """Route tool call to the correct handler and return JSON result."""
        handlers = {
            "get_temporal_context": self._handle_get_temporal_context,
            "schedule_event": self._handle_schedule_event,
            "cancel_event": self._handle_cancel_event,
            "set_timer_alarm": self._handle_set_timer_alarm,
            "nominate_milestone": self._handle_nominate_milestone,
            "resolve_conflicts": self._handle_resolve_conflicts,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown temporal tool: {name}"})
        try:
            result = handler(arguments, agent_id)
        except Exception as e:
            logger.error("temporal tool error", extra={"tool": name, "error": str(e)})
            result = {"error": str(e)}
        return json.dumps(result)

    # ── get_temporal_context ───────────────────────────

    def _handle_get_temporal_context(self, args: dict, agent_id: str) -> dict:
        mode = args.get("mode", "standard")
        dsl = self.serializer.to_dsl(agent_id, mode)
        conflicts = self.serializer.get_conflicts(agent_id)
        event_count = self.db.count_events(agent_id)

        result = {
            "context": dsl,
            "mode": mode,
            "event_count": event_count,
            "conflicts": conflicts,
        }
        # Opportunistic consideration hint
        if conflicts:
            result["consideration"] = f"You have {len(conflicts)} scheduling conflict(s). Review before planning."
        else:
            upcoming = self.db.get_events(agent_id, status="confirmed", limit=10)
            now = datetime.now(timezone.utc)
            soon = [e for e in upcoming if e.get("start_time", "") > now.isoformat()]
            if soon:
                result["consideration"] = f"You have {len(soon)} upcoming event(s). Temporal context loaded."
        return result

    # ── schedule_event ─────────────────────────────────

    def _handle_schedule_event(self, args: dict, agent_id: str) -> dict:
        title = args["title"]
        start_time = args["start_time"]

        # Default: start + 1 hour if no end_time
        end_time = args.get("end_time")
        if not end_time:
            try:
                st = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end_time = (st + timedelta(hours=1)).isoformat()
            except (ValueError, TypeError):
                pass

        event = {
            "agent_id": agent_id,
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "flexibility": args.get("flexibility", "high"),
            "stakeholders": args.get("stakeholders"),
            "metadata": args.get("metadata"),
        }

        event_id = self.db.insert_event(event)

        # Check conflicts
        conflicts = self.serializer.get_conflicts(agent_id)

        # Auto-resolve if requested (default: True)
        resolution = None
        auto_resolve = args.get("auto_resolve", True)
        if auto_resolve and conflicts:
            resolution = self.serializer.resolve_conflicts(agent_id)

        result = {
            "scheduled": True,
            "event_id": event_id.split(":", 1)[1] if ":" in event_id else event_id,
            "title": title,
            "conflicts": conflicts,
        }
        if resolution:
            result["resolution"] = resolution
        return result

    # ── cancel_event ───────────────────────────────────

    def _handle_cancel_event(self, args: dict, agent_id: str) -> dict:
        raw_id = args["event_id"]
        # Reconstruct full DB ID if short ID provided (e.g., 'e3' → 'hermes:e3')
        if ":" not in raw_id:
            full_id = f"{agent_id}:{raw_id}"
        else:
            full_id = raw_id

        # Fetch the event to get current title
        events = self.db.get_events(agent_id, limit=200)
        event = next((e for e in events if e["id"] == full_id), None)
        if not event:
            return {"cancelled": False, "error": f"Event '{raw_id}' not found"}

        self.db.update_event(full_id, {"status": "cancelled"})

        return {
            "cancelled": True,
            "event_id": raw_id.split(":", 1)[1] if ":" in raw_id else raw_id,
            "title": event["title"],
        }

    # ── set_timer_alarm ────────────────────────────────

    def _handle_set_timer_alarm(self, args: dict, agent_id: str) -> dict:
        title = args["title"]
        duration = args.get("duration_minutes")
        trigger = args.get("trigger_at")

        # Exactly one must be provided
        if duration and trigger:
            return {"set": False, "error": "Provide exactly one of 'duration_minutes' or 'trigger_at', not both."}
        if not duration and not trigger:
            return {"set": False, "error": "Provide either 'duration_minutes' (timer) or 'trigger_at' (alarm)."}

        now = datetime.now(timezone.utc)
        if duration:
            event_type = "timer"
            start_time = now.isoformat()
            trigger_time = (now + timedelta(minutes=duration)).isoformat()
            remaining = duration * 60
        else:
            event_type = "alarm"
            start_time = trigger
            trigger_time = trigger
            try:
                trigger_dt = datetime.fromisoformat(trigger.replace("Z", "+00:00"))
                remaining = max(0, int((trigger_dt - now).total_seconds()))
            except (ValueError, TypeError):
                remaining = 0

        event = {
            "agent_id": agent_id,
            "title": title,
            "start_time": start_time,
            "end_time": trigger_time,
            "event_type": event_type,
            "status": "confirmed",
            "metadata": {
                "duration_minutes": duration,
                "trigger_at": trigger,
            },
        }

        alarm_id = self.db.insert_event(event)

        return {
            "set": True,
            "alarm_id": alarm_id.split(":", 1)[1] if ":" in alarm_id else alarm_id,
            "type": event_type,
            "remaining_seconds": remaining,
        }

    # ── nominate_milestone ─────────────────────────────

    def _handle_nominate_milestone(self, args: dict, agent_id: str) -> dict:
        entry = {
            "agent_id": agent_id,
            "title": args["title"],
            "description": args.get("description"),
            "entry_type": args.get("entry_type", "milestone"),
            "occurred_at": args["occurred_at"],
            "tags": args.get("tags"),
        }

        journal_id = self.db.insert_journal(entry)

        return {
            "recorded": True,
            "journal_id": journal_id.split(":", 1)[1] if ":" in journal_id else journal_id,
            "title": args["title"],
        }

    # ── resolve_conflicts ───────────────────────────────

    def _handle_resolve_conflicts(self, args: dict, agent_id: str) -> dict:
        resolution = self.serializer.resolve_conflicts(agent_id)
        return resolution
