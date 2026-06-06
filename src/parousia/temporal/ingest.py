"""Temporal ingest pipeline.

Parses .ics files (RFC 5545), structured JSON, and unstructured text
into temporal events. Bridges email .ics attachments to temporal storage.
"""

import json
import logging
from typing import Optional

from parousia.temporal.db import TemporalDB

logger = logging.getLogger("parousia.ingest")


class TemporalIngest:
    """Parse and ingest calendar data into the temporal database."""

    def __init__(self, db: TemporalDB):
        self.db = db

    # ── .ics parser ─────────────────────────────────────

    def parse_ics(self, ics_text: str, agent_id: str) -> dict:
        """Parse RFC 5545 iCalendar text into temporal_events.

        Returns:
            {"event_ids": [...], "errors": [...]}
        """
        from icalendar import Calendar

        event_ids = []
        errors = []

        try:
            cal = Calendar.from_ical(ics_text)
        except Exception as e:
            logger.warning("ICS parse error", extra={"error": str(e)})
            return {"event_ids": [], "errors": [f"ICS parse error: {e}"]}

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            try:
                eid = self._parse_vevent(component, agent_id)
                if eid:
                    event_ids.append(eid)
            except Exception as e:
                summary = str(component.get("summary", "Untitled"))
                errors.append(f"Event '{summary}': {e}")
                logger.warning("VEVENT parse error", extra={"summary": summary, "error": str(e)})

        return {"event_ids": event_ids, "errors": errors}

    def _parse_vevent(self, component, agent_id: str) -> Optional[str]:
        """Parse a single VEVENT component. Returns event_id or None if skipped."""
        from datetime import datetime, timezone

        summary = str(component.get("summary", "Untitled"))
        uid = str(component.get("uid", ""))

        # Extract times
        dtstart = component.get("dtstart")
        dtend = component.get("dtend")

        if not dtstart:
            logger.warning("VEVENT without DTSTART", extra={"uid": uid})
            return None

        start_time = dtstart.dt.isoformat() if hasattr(dtstart, 'dt') else str(dtstart)
        end_time = dtend.dt.isoformat() if dtend and hasattr(dtend, 'dt') else (str(dtend) if dtend else None)

        # Timezone info
        tzid = None
        for dt_attr in (dtstart, dtend):
            if dt_attr and hasattr(dt_attr, 'dt'):
                tz = getattr(dt_attr.dt, 'tzinfo', None)
                if tz:
                    tzid = str(tz)
                    break

        # Stakeholders
        stakeholders = self._extract_stakeholders(component)

        # Recurrence
        rrule = component.get("rrule")
        rrule_str = None
        if rrule:
            try:
                rrule_str = rrule.to_ical().decode("utf-8")
            except Exception:
                rrule_str = str(rrule)

        # Build metadata
        metadata = {}
        if uid:
            metadata["uid"] = uid
        if tzid:
            metadata["tzid"] = tzid
        if rrule_str:
            metadata["rrule"] = rrule_str

        # Idempotent check by UID
        if uid:
            existing = self.db.get_events(agent_id, limit=200)
            for ev in existing:
                ev_meta = json.loads(ev.get("metadata") or "{}")
                if ev_meta.get("uid") == uid:
                    logger.debug("Skipping duplicate ICS event", extra={"uid": uid, "existing_id": ev["id"]})
                    return ev["id"]  # return existing event_id

        event = {
            "agent_id": agent_id,
            "title": summary,
            "start_time": start_time,
            "end_time": end_time,
            "source": "ics_import",
            "stakeholders": stakeholders,
            "metadata": metadata,
        }
        return self.db.insert_event(event)

    def _extract_stakeholders(self, component) -> Optional[str]:
        """Extract organizer and attendees from a VEVENT component."""
        parts = []

        def _extract_email(raw) -> Optional[str]:
            """Extract email from organizer/attendee value."""
            s = str(raw)
            # Strip common prefixes
            for prefix in ("ORG:", "ORGANIZER:", "ATTENDEE:", "MAILTO:", "mailto:"):
                s = s.replace(prefix, "")
            if "mailto:" in s:
                s = s.split("mailto:")[1]
            s = s.strip()
            if "@" in s and not s.startswith("vCalAddress"):
                return s
            return None

        organizer = component.get("organizer")
        if organizer:
            email = _extract_email(organizer)
            if email:
                parts.append(email)

        attendees = component.get("attendee", [])
        if not isinstance(attendees, list):
            attendees = [attendees]
        for att in attendees:
            email = _extract_email(att)
            if email and email not in parts:
                parts.append(email)

        return ", ".join(parts) if parts else None

    # ── JSON parser ─────────────────────────────────────

    def parse_json(self, payload: dict, agent_id: str) -> str:
        """Validate and insert a structured JSON event payload.

        Required fields: title, start_time.
        Returns: event_id.
        """
        if "agent_id" not in payload:
            payload["agent_id"] = agent_id

        required = ["title", "start_time"]
        missing = [f for f in required if f not in payload]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

        event = {
            "agent_id": agent_id,
            "title": payload["title"],
            "start_time": payload["start_time"],
            "end_time": payload.get("end_time"),
            "flexibility": payload.get("flexibility", "high"),
            "stakeholders": payload.get("stakeholders"),
            "metadata": payload.get("metadata"),
            "source": "json_import",
        }
        return self.db.insert_event(event)

    # ── NL parser ───────────────────────────────────────

    def parse_nl(
        self, text: str, agent_id: str, llm_endpoint: Optional[str] = None
    ) -> dict:
        """Parse unstructured natural-language text into an event.

        If llm_endpoint is configured, POST the text to that endpoint.
        Otherwise, return a passthrough dict for the agent to handle.

        Returns: {"passthrough": True, "text": ...} or {"parsed": True, "event_id": ...}
        """
        if not llm_endpoint:
            return {"passthrough": True, "text": text, "agent_id": agent_id}

        try:
            import requests

            resp = requests.post(
                llm_endpoint,
                json={"text": text},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            event_id = self.parse_json(data, agent_id)
            return {"parsed": True, "event_id": event_id}
        except Exception as e:
            logger.warning("NL parse failed, falling back to passthrough", extra={"error": str(e)})
            return {"passthrough": True, "text": text, "agent_id": agent_id}
