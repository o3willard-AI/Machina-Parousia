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
        end_range = (now + timedelta(days=30)).replace(
            hour=23, minute=59, second=59, microsecond=0
        ).isoformat()
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
                e1_end = _parse_dt(e1.get("end_time") or e1["start_time"])
                s2 = _parse_dt(e2["start_time"])
                e2_end = _parse_dt(e2.get("end_time") or e2["start_time"])
                if s1 < e2_end and s2 < e1_end:
                    conflicts.append({
                        "event_a": _strip_agent_id(e1["id"]),
                        "event_b": _strip_agent_id(e2["id"]),
                        "title_a": e1["title"],
                        "title_b": e2["title"],
                    })
        return conflicts

    def resolve_conflicts(self, agent_id: str, lookahead_days: int = 30) -> dict:
        """Detect and auto-resolve scheduling conflicts.

        Resolution rules (priority order):
          1. flexibility='none' events are NEVER moved
          2. 'high' gives way to 'low' and 'none'
          3. 'low' gives way to 'none'
          4. Between equal-flex events: move the shorter-duration one
          5. Between equal-duration events: move the newer one (spare existing commitments)

        Returns:
          dict with 'resolved' (list of moved events), 'unresolved' (list of
          conflicts that couldn't be auto-resolved), and 'actions_taken' (str summary).
        """
        now = datetime.now(timezone.utc)
        # Use end-of-day for the lookahead boundary so events on the final day are included
        end_range = (now + timedelta(days=lookahead_days)).replace(
            hour=23, minute=59, second=59, microsecond=0
        ).isoformat()

        events = self.db.get_events(
            agent_id,
            start_range=now.isoformat(),
            end_range=end_range,
            status="confirmed",
            limit=200,
        )
        if len(events) < 2:
            return {"resolved": [], "unresolved": [], "actions_taken": "No conflicts to resolve."}

        # Build full event dicts indexed by short ID
        event_map: dict[str, dict] = {}
        for e in events:
            sid = _strip_agent_id(e["id"])
            event_map[sid] = dict(e)

        # Collect all conflict pairs
        pairs: list[tuple[str, str]] = []
        for i in range(len(events)):
            for j in range(i + 1, len(events)):
                e1, e2 = events[i], events[j]
                s1 = _parse_dt(e1["start_time"])
                e1_end = _parse_dt(e1.get("end_time") or e1["start_time"])
                s2 = _parse_dt(e2["start_time"])
                e2_end = _parse_dt(e2.get("end_time") or e2["start_time"])
                if s1 < e2_end and s2 < e1_end:
                    pairs.append((_strip_agent_id(e1["id"]), _strip_agent_id(e2["id"])))

        if not pairs:
            return {"resolved": [], "unresolved": [], "actions_taken": "No conflicts to resolve."}

        # Map flexibility to a priority score (lower = more privileged, never moved)
        flex_priority = {"none": 0, "low": 1, "high": 2}

        resolved: list[dict] = []
        unresolved: list[dict] = []
        # Track which events have been moved (their start/end times shift through resolution)
        moved: dict[str, datetime] = {}  # short_id -> new_start

        for (id_a, id_b) in pairs:
            ea = event_map.get(id_a)
            eb = event_map.get(id_b)
            if not ea or not eb:
                continue
            # Skip if either has been cancelled/moved by a prior resolution
            if ea.get("status") == "cancelled" or eb.get("status") == "cancelled":
                continue

            # Use updated times if one was already moved
            sa = moved.get(id_a) or _parse_dt(ea["start_time"])
            ea_end = sa + self._event_duration(ea)
            sb = moved.get(id_b) or _parse_dt(eb["start_time"])
            eb_end = sb + self._event_duration(eb)

            # Check if still overlapping after prior moves
            if sa >= eb_end or sb >= ea_end:
                continue

            fa = flex_priority.get(ea.get("flexibility", "high"), 2)
            fb = flex_priority.get(eb.get("flexibility", "high"), 2)

            # Higher flex_priority = more flexible = should move
            # Rules: 'high'(2) gives way to 'low'(1), 'low'(1) gives way to 'none'(0)
            if fa > fb:
                # a is more flexible → a moves
                mover_id, stayer_id = id_a, id_b
                mover, stayer = ea, eb
                stayer_end = eb_end
            elif fb > fa:
                # b is more flexible → b moves
                mover_id, stayer_id = id_b, id_a
                mover, stayer = eb, ea
                stayer_end = ea_end
            else:
                # Equal flexibility — move the shorter event
                dur_a = self._event_duration(ea)
                dur_b = self._event_duration(eb)
                if dur_a < dur_b:
                    mover_id, stayer_id = id_a, id_b
                    mover, stayer = ea, eb
                    stayer_end = eb_end
                elif dur_b < dur_a:
                    mover_id, stayer_id = id_b, id_a
                    mover, stayer = eb, ea
                    stayer_end = ea_end
                else:
                    # Equal duration — move the newer event (higher ID number)
                    a_num = int(id_a[1:]) if id_a[1:].isdigit() else 0
                    b_num = int(id_b[1:]) if id_b[1:].isdigit() else 0
                    if a_num > b_num:
                        mover_id, stayer_id = id_a, id_b
                        mover, stayer = ea, eb
                        stayer_end = eb_end
                    else:
                        mover_id, stayer_id = id_b, id_a
                        mover, stayer = eb, ea
                        stayer_end = ea_end

            mover_duration = self._event_duration(mover)
            mover_flex = mover.get("flexibility", "high")

            # If the mover is immovable, this conflict can't be auto-resolved
            if mover_flex == "none":
                unresolved.append({
                    "event_a": id_a,
                    "event_b": id_b,
                    "title_a": ea["title"],
                    "title_b": eb["title"],
                    "reason": "Both events marked flexibility='none' — cannot auto-resolve.",
                })
                continue

            # Place mover immediately after the stayer ends
            new_start = stayer_end
            # Ensure we don't push into another collision — simple forward scan
            new_end = new_start + mover_duration
            # Look for other events in the 30-day window that might collide with new slot
            collides = self._find_collision(agent_id, new_start, new_end, exclude_ids={mover_id, stayer_id})
            attempts = 0
            while collides and attempts < 100:
                # Push past this collision
                coll_end = _parse_dt(collides.get("end_time", collides.get("start_time", "")))
                new_start = coll_end
                new_end = new_start + mover_duration
                collides = self._find_collision(agent_id, new_start, new_end, exclude_ids={mover_id, stayer_id})
                attempts += 1

            if attempts >= 100:
                unresolved.append({
                    "event_a": id_a,
                    "event_b": id_b,
                    "title_a": ea["title"],
                    "title_b": eb["title"],
                    "reason": "Could not find a free slot for the moved event within the lookahead window.",
                })
                continue

            # Apply the resolution
            self.db.update_event(mover["id"], {
                "start_time": new_start.isoformat(),
                "end_time": new_end.isoformat(),
            })
            moved[mover_id] = new_start
            # Update our local map so subsequent conflict pairs see the new time
            event_map[mover_id]["start_time"] = new_start.isoformat()
            event_map[mover_id]["end_time"] = new_end.isoformat()

            resolved.append({
                "moved_event_id": mover_id,
                "moved_title": mover["title"],
                "original_start": ea["start_time"] if mover_id == id_a else eb["start_time"],
                "new_start": new_start.isoformat(),
                "new_end": new_end.isoformat(),
                "reason": (
                    f"Moved '{mover['title']}' ({mover_flex} flexibility) "
                    f"to resolve conflict with '{stayer['title']}'."
                ),
            })

        actions = []
        if resolved:
            actions.append(f"Resolved {len(resolved)} conflict(s): {', '.join(r['moved_title'] for r in resolved)}.")
        if unresolved:
            actions.append(f"{len(unresolved)} conflict(s) could not be auto-resolved (flexibility='none' clashes).")
        if not actions:
            actions.append("No conflicts to resolve.")

        return {
            "resolved": resolved,
            "unresolved": unresolved,
            "actions_taken": " ".join(actions),
        }

    def _event_duration(self, event: dict) -> timedelta:
        """Return the duration of an event as a timedelta. Default 1 hour."""
        s = _parse_dt(event.get("start_time", ""))
        e_str = event.get("end_time")
        if e_str:
            e = _parse_dt(e_str)
        else:
            e = s + timedelta(hours=1)
        duration = e - s
        if duration.total_seconds() <= 0:
            return timedelta(hours=1)
        return duration

    def _find_collision(
        self, agent_id: str, start: datetime, end: datetime,
        exclude_ids: Optional[set] = None,
    ) -> Optional[dict]:
        """Return the first event that overlaps with [start, end), or None."""
        if exclude_ids is None:
            exclude_ids = set()
        now = datetime.now(timezone.utc)
        end_range = (now + timedelta(days=30)).replace(
            hour=23, minute=59, second=59, microsecond=0
        ).isoformat()
        events = self.db.get_events(
            agent_id,
            start_range=now.isoformat(),
            end_range=end_range,
            status="confirmed",
            limit=200,
        )
        for e in events:
            sid = _strip_agent_id(e["id"])
            if sid in exclude_ids:
                continue
            es = _parse_dt(e["start_time"])
            ee = _parse_dt(e.get("end_time") or e["start_time"])
            if start < ee and es < end:
                return e
        return None


__all__ = ["TemporalSerializer"]
