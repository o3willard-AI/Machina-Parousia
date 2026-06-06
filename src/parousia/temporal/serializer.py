"""Token-lean temporal DSL serializer.

Converts database rows into the compressed text format for LLM context windows.
Supports standard, planning, retrospective, and full modes.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from parousia.temporal.db import TemporalDB


# Mode configurations: (past_days, future_days, include_journal)
MODE_CONFIGS = {
    "standard":      (1, 3, True),
    "planning":      (0, 14, False),
    "retrospective": (7, 0, True),
    "full":          (30, 30, True),
}
DOMAIN = "GENERAL_CORP"


def _format_date(dt_str: str, now: datetime) -> str:
    """Format an ISO datetime string as MM-DD or YYYY-MM-DD."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return dt_str[:10] if dt_str else "?"

    if dt.year == now.year:
        return dt.strftime("%m-%d")
    return dt.strftime("%Y-%m-%d")


def _format_time(dt_str: str) -> str:
    """Extract HH:MM from ISO datetime string."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return "??:??"


def _strip_agent_id(db_id: str) -> str:
    """Convert 'hermes:e3' → 'e3' for display in DSL."""
    if ":" in db_id:
        return db_id.split(":", 1)[1]
    return db_id


def _now_header(now: datetime) -> str:
    """Generate the !NOW header line."""
    iso_week = now.isocalendar()
    return f"!NOW: {now.strftime('%Y-%m-%d')} W{iso_week.week:02d} {now.strftime('%a %H:%M')} | DOMAIN: {DOMAIN}"


def _past_window(events: list[dict], now: datetime, past_days: int) -> str:
    """Generate the #PAST_WINDOW section."""
    cutoff = now - timedelta(days=past_days)
    past_events = [
        e for e in events
        if e["status"] == "completed"
        and _parse_dt(e.get("start_time", "")) >= cutoff
        and _parse_dt(e.get("start_time", "")) <= now
    ]
    if not past_events:
        return ""

    lines = [f"#PAST_WINDOW ({past_days}d)"]
    for e in past_events:
        sid = _strip_agent_id(e["id"])
        date_part = _format_date(e["start_time"], now)
        start_t = _format_time(e["start_time"])
        end_t = _format_time(e["end_time"]) if e.get("end_time") else "??:??"
        lines.append(f"- {date_part} {start_t}|{end_t} [id:{sid}] {e['title']} *DONE")
    return "\n".join(lines)


def _planned_window(events: list[dict], now: datetime, future_days: int) -> str:
    """Generate the #PLANNED_WINDOW section."""
    end_range = now + timedelta(days=future_days)
    future_events = [
        e for e in events
        if e["status"] == "confirmed"
        and _parse_dt(e.get("start_time", "")) >= now
        and _parse_dt(e.get("start_time", "")) <= end_range
    ]
    if not future_events:
        return ""

    lines = [f"#PLANNED_WINDOW ({future_days}d)"]
    for e in future_events:
        sid = _strip_agent_id(e["id"])
        date_part = _format_date(e["start_time"], now)
        start_t = _format_time(e["start_time"])
        end_t = _format_time(e["end_time"]) if e.get("end_time") else "??:??"
        flex = e.get("flexibility", "high")
        lines.append(f"- {date_part} {start_t}|{end_t} [id:{sid}] [F:{flex}] {e['title']}")
    return "\n".join(lines)


def _timers_alarms(events: list[dict], now: datetime) -> str:
    """Generate the #TIMERS_ALARMS section."""
    ta_events = [
        e for e in events
        if e["event_type"] in ("timer", "alarm")
        and e["status"] != "cancelled"
    ]
    if not ta_events:
        return ""

    lines = ["#TIMERS_ALARMS"]
    for e in ta_events:
        sid = _strip_agent_id(e["id"])
        if e["event_type"] == "timer":
            lines.append(f"- T: {e.get('duration_display', '?')} [id:{sid}] {e['title']}")
        else:
            trigger = _format_date(e.get("trigger_at", e.get("start_time", "")), now)
            trigger_t = _format_time(e.get("trigger_at", e.get("start_time", "")))
            lines.append(f"- A: {trigger} {trigger_t} [id:{sid}] {e['title']}")
    return "\n".join(lines)


def _journal_section(journal: list[dict], now: datetime) -> str:
    """Generate the #JOURNAL section."""
    if not journal:
        return ""

    lines = ["#JOURNAL (recent)"]
    for j in journal:
        sid = _strip_agent_id(j["id"])
        date_part = _format_date(j["occurred_at"], now)
        lines.append(f"- {date_part} [id:{sid}] {j['title']}")
    return "\n".join(lines)


def _parse_dt(dt_str: str) -> datetime:
    """Parse an ISO datetime string safely."""
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _default_mode(mode: Optional[str]) -> str:
    """Normalize mode string to a valid config key."""
    if mode and mode in MODE_CONFIGS:
        return mode
    return "standard"


class TemporalSerializer:
    """Serialize temporal DB state into a token-lean DSL string for LLM context."""

    def __init__(self, db: TemporalDB):
        self.db = db

    def to_dsl(self, agent_id: str, mode: Optional[str] = None) -> str:
        """Generate the temporal DSL string for an agent."""
        mode = _default_mode(mode)
        past_days, future_days, include_journal = MODE_CONFIGS[mode]

        now = datetime.now(timezone.utc)
        start_range = (now - timedelta(days=past_days)).isoformat()
        end_range = (now + timedelta(days=future_days)).isoformat()

        events = self.db.get_events(
            agent_id,
            start_range=start_range,
            end_range=end_range,
            limit=200,
        )

        sections = [_now_header(now)]

        past = _past_window(events, now, past_days)
        if past:
            sections.append(past)

        planned = _planned_window(events, now, future_days)
        if planned:
            sections.append(planned)

        ta = _timers_alarms(events, now)
        if ta:
            sections.append(ta)

        if include_journal:
            journal = self.db.get_journal(agent_id, limit=5)
            j = _journal_section(journal, now)
            if j:
                sections.append(j)

        return "\n".join(sections)

    def measure_tokens(self, dsl: str) -> int:
        """Rough token count: characters / 4."""
        return len(dsl) // 4

    def get_conflicts(self, agent_id: str) -> list[dict]:
        """Detect overlapping events for an agent. Returns list of conflict pairs."""
        now = datetime.now(timezone.utc)
        end_range = (now + timedelta(days=30)).isoformat()
        events = self.db.get_events(
            agent_id,
            start_range=now.isoformat(),
            end_range=end_range,
            status="confirmed",
            limit=200,
        )
        conflicts = []
        for i in range(len(events)):
            for j in range(i + 1, len(events)):
                e1, e2 = events[i], events[j]
                s1 = _parse_dt(e1["start_time"])
                e1_end = _parse_dt(e1.get("end_time", e1["start_time"]))
                s2 = _parse_dt(e2["start_time"])
                e2_end = _parse_dt(e2.get("end_time", e2["start_time"]))
                if s1 < e2_end and s2 < e1_end:
                    conflicts.append({
                        "event_a": _strip_agent_id(e1["id"]),
                        "event_b": _strip_agent_id(e2["id"]),
                        "title_a": e1["title"],
                        "title_b": e2["title"],
                    })
        return conflicts


__all__ = ["TemporalSerializer"]
