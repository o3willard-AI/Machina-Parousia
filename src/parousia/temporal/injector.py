"""Opportunistic temporal injection.

Lightweight keyword classifier that gates whether temporal context should
be injected into the agent's next LLM pass, plus consideration hints on
select MCP tool returns.
"""

from typing import Optional, List, Dict, Any

DEFAULT_TEMPORAL_KEYWORDS = [
    "schedule", "calendar", "meeting", "appointment", "event", "next week", 
    "tomorrow", "yesterday", "remind", "deadline", "due date", "upcoming", 
    "this month", "timeline", "agenda", "invite", "rsvp"
]

def contains_temporal_keywords(text: str, keywords: Optional[List[str]] = None) -> bool:
    """Case-insensitive substring match against keyword list.
    
    Args:
        text: Input text to check for temporal keywords
        keywords: Optional custom keyword list (uses default if None)
        
    Returns:
        True if any keyword matches, False otherwise
    """
    if not text:
        return False
    
    if keywords is None:
        keywords = DEFAULT_TEMPORAL_KEYWORDS
    
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)


def generate_consideration_hint(tool_name: str, result: Dict[str, Any]) -> Optional[str]:
    """Context-aware hint generation based on tool usage.
    
    Args:
        tool_name: Name of the MCP tool that was called
        result: Result dictionary from the tool execution
        
    Returns:
        Hint string (max 120 chars) or None if no hint applicable
    """
    if tool_name == "send_email" and "body" in result:
        if contains_temporal_keywords(result["body"]):
            return "This email mentions a deadline. Your temporal context may be relevant."
    
    elif tool_name == "get_temporal_context" and "count" in result:
        count = result["count"]
        return f"You have {count} upcoming events in the next 24 hours."
    
    elif tool_name == "schedule_event" and result.get("scheduled") is True:
        return "Event scheduled. Check your temporal context for remaining slots."
    
    elif tool_name == "nominate_milestone" and result.get("recorded") is True:
        count = result.get("count", 0)
        return f"Milestone recorded. Your journal now has {count} entries."
    
    return None


__all__ = ["contains_temporal_keywords", "generate_consideration_hint"]
