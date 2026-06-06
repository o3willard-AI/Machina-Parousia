"""Tests for temporal injector module."""

import pytest
from parousia.temporal.injector import contains_temporal_keywords, generate_consideration_hint


class TestContainsTemporalKeywords:
    """Tests for contains_temporal_keywords function."""
    
    def test_keyword_match(self):
        """Test that temporal keywords are correctly identified."""
        text = "schedule a meeting tomorrow"
        assert contains_temporal_keywords(text) is True
        
    def test_no_match(self):
        """Test that non-temporal text returns False."""
        text = "whats the weather like"
        assert contains_temporal_keywords(text) is False
        
    def test_empty_string(self):
        """Test that empty string returns False."""
        text = ""
        assert contains_temporal_keywords(text) is False
        
    def test_custom_keywords(self):
        """Test that custom keyword lists are respected."""
        text = "let's go to the party"
        custom_keywords = ["party", "celebration"]
        assert contains_temporal_keywords(text, custom_keywords) is True
        
        # Same text should return False with default keywords
        assert contains_temporal_keywords(text) is False

    def test_case_insensitive_matching(self):
        """Test that keyword matching is case-insensitive."""
        text = "SCHEDULE A MEETING TOMORROW"
        assert contains_temporal_keywords(text) is True
        
    def test_partial_match(self):
        """Test substring matching works correctly."""
        text = "we need to reschedule this"
        assert contains_temporal_keywords(text) is True


class TestGenerateConsiderationHint:
    """Tests for generate_consideration_hint function."""
    
    def test_consideration_hint_send_email(self):
        """Test hint generation for send_email with temporal content."""
        result = {"body": "Don't forget about the meeting tomorrow"}
        hint = generate_consideration_hint("send_email", result)
        expected = "This email mentions a deadline. Your temporal context may be relevant."
        assert hint == expected
        
    def test_consideration_hint_send_email_no_temporal(self):
        """Test no hint when email body has no temporal keywords."""
        result = {"body": "Here is the report you requested"}
        hint = generate_consideration_hint("send_email", result)
        assert hint is None
        
    def test_consideration_hint_get_temporal_context(self):
        """Test hint generation for get_temporal_context."""
        result = {"count": 3}
        hint = generate_consideration_hint("get_temporal_context", result)
        expected = "You have 3 upcoming events in the next 24 hours."
        assert hint == expected
        
    def test_consideration_hint_schedule_event(self):
        """Test hint generation for successful event scheduling."""
        result = {"scheduled": True}
        hint = generate_consideration_hint("schedule_event", result)
        expected = "Event scheduled. Check your temporal context for remaining slots."
        assert hint == expected
        
    def test_consideration_hint_schedule_event_failed(self):
        """Test no hint when event scheduling fails."""
        result = {"scheduled": False}
        hint = generate_consideration_hint("schedule_event", result)
        assert hint is None
        
    def test_consideration_hint_nominate_milestone(self):
        """Test hint generation for successful milestone nomination."""
        result = {"recorded": True, "count": 5}
        hint = generate_consideration_hint("nominate_milestone", result)
        expected = "Milestone recorded. Your journal now has 5 entries."
        assert hint == expected
        
    def test_consideration_hint_nominate_milestone_no_count(self):
        """Test milestone hint with default count when count not provided."""
        result = {"recorded": True}
        hint = generate_consideration_hint("nominate_milestone", result)
        expected = "Milestone recorded. Your journal now has 0 entries."
        assert hint == expected
    
    def test_consideration_hint_none(self):
        """Test that cancel_event returns None (no hint applicable)."""
        result = {"cancelled": True}
        hint = generate_consideration_hint("cancel_event", result)
        assert hint is None
        
    def test_hint_length_limit(self):
        """Test that all hints are within 120 character limit."""
        test_cases = [
            ("send_email", {"body": "meeting tomorrow"}),
            ("get_temporal_context", {"count": 999}),
            ("schedule_event", {"scheduled": True}),
            ("nominate_milestone", {"recorded": True, "count": 999})
        ]
        
        for tool_name, result in test_cases:
            hint = generate_consideration_hint(tool_name, result)
            if hint is not None:
                assert len(hint) <= 120, f"Hint too long: '{hint}' ({len(hint)} chars)"