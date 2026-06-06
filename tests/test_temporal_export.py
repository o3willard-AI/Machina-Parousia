"""Tests for temporal export functionality."""

import pytest
from datetime import datetime
import json
from icalendar import Calendar, Event
from parousia.temporal.export import TemporalExport


@pytest.fixture
def event_row():
    """Sample event row fixture for testing."""
    return {
        'event_id': 'agent_id:test-event-123',
        'title': 'Test Meeting',
        'description': 'This is a test meeting description',
        'start_time': '2024-12-15T10:00:00Z',
        'end_time': '2024-12-15T11:00:00Z',
        'stakeholders': 'alice@example.com, bob@example.com',
        'metadata': {
            'uid': 'test-uid-12345',
            'tzid': 'America/New_York'
        }
    }


def test_to_ics_roundtrip(event_row):
    """Test that ICS output can be parsed back with icalendar and is valid."""
    ics_output = TemporalExport.to_ics(event_row)
    
    # Parse the output back with icalendar
    cal = Calendar.from_ical(ics_output)
    
    # Verify it's a valid calendar
    assert cal is not None
    assert cal['version'] == '2.0'
    assert 'prodid' in cal
    
    # Check that we have at least one event
    events = [component for component in cal.walk() if component.name == 'VEVENT']
    assert len(events) >= 1
    
    event = events[0]
    assert event['uid'] == event_row['metadata']['uid']
    assert str(event['summary']) == event_row['title']


def test_to_ics_includes_fields(event_row):
    """Test that ICS output includes required fields: UID, SUMMARY, DTSTART."""
    ics_output = TemporalExport.to_ics(event_row)
    
    # Check that required fields are present in the string
    assert 'UID:test-uid-12345' in ics_output
    assert 'SUMMARY:Test Meeting' in ics_output
    assert 'DTSTART:' in ics_output
    assert 'DTEND:' in ics_output
    assert 'DESCRIPTION:This is a test meeting description' in ics_output


def test_to_google_json(event_row):
    """Test Google Calendar API v3 payload has correct schema shape."""
    google_payload = TemporalExport.to_google(event_row)
    
    # Verify required fields are present
    assert 'summary' in google_payload
    assert google_payload['summary'] == 'Test Meeting'
    
    # Check start time structure
    assert 'start' in google_payload
    assert 'dateTime' in google_payload['start']
    assert 'timeZone' in google_payload['start']
    assert google_payload['start']['dateTime'] == '2024-12-15T10:00:00Z'
    assert google_payload['start']['timeZone'] == 'America/New_York'
    
    # Check end time structure
    assert 'end' in google_payload
    assert 'dateTime' in google_payload['end']
    assert 'timeZone' in google_payload['end']
    
    # Check description
    assert google_payload['description'] == 'This is a test meeting description'
    
    # Check event ID (should strip agent_id: prefix)
    assert google_payload['id'] == 'test-event-123'
    
    # Verify payload can be JSON serialized
    json_str = json.dumps(google_payload)
    assert json_str is not None


def test_to_msgraph_json(event_row):
    """Test MS Graph API payload has correct schema shape."""
    msgraph_payload = TemporalExport.to_msgraph(event_row)
    
    # Verify required fields are present
    assert 'subject' in msgraph_payload
    assert msgraph_payload['subject'] == 'Test Meeting'
    
    # Check start time structure
    assert 'start' in msgraph_payload
    assert 'dateTime' in msgraph_payload['start']
    assert 'timeZone' in msgraph_payload['start']
    assert msgraph_payload['start']['dateTime'] == '2024-12-15T10:00:00Z'
    assert msgraph_payload['start']['timeZone'] == 'America/New_York'
    
    # Check end time structure
    assert 'end' in msgraph_payload
    assert 'dateTime' in msgraph_payload['end']
    assert 'timeZone' in msgraph_payload['end']
    
    # Check body structure
    assert 'body' in msgraph_payload
    assert 'contentType' in msgraph_payload['body']
    assert 'content' in msgraph_payload['body']
    assert msgraph_payload['body']['contentType'] == 'text'
    assert msgraph_payload['body']['content'] == 'This is a test meeting description'
    
    # Verify payload can be JSON serialized
    json_str = json.dumps(msgraph_payload)
    assert json_str is not None


def test_to_all_formats(event_row):
    """Test that to_all_formats returns dict with 'ics', 'google', 'msgraph' keys."""
    all_formats = TemporalExport.to_all_formats(event_row)
    
    # Verify all required keys are present
    assert 'ics' in all_formats
    assert 'google' in all_formats
    assert 'msgraph' in all_formats
    
    # Verify each format is correct type
    assert isinstance(all_formats['ics'], str)
    assert isinstance(all_formats['google'], dict)
    assert isinstance(all_formats['msgraph'], dict)
    
    # Verify ICS contains expected content
    assert 'SUMMARY:Test Meeting' in all_formats['ics']
    
    # Verify Google format has expected fields
    assert all_formats['google']['summary'] == 'Test Meeting'
    assert 'start' in all_formats['google']
    
    # Verify MS Graph format has expected fields
    assert all_formats['msgraph']['subject'] == 'Test Meeting'
    assert 'start' in all_formats['msgraph']


def test_stakeholders_to_attendees(event_row):
    """Test that stakeholders string is correctly converted to attendee arrays."""
    google_payload = TemporalExport.to_google(event_row)
    msgraph_payload = TemporalExport.to_msgraph(event_row)
    
    # Test Google format attendees
    assert 'attendees' in google_payload
    google_attendees = google_payload['attendees']
    assert len(google_attendees) == 2
    assert google_attendees[0]['email'] == 'alice@example.com'
    assert google_attendees[1]['email'] == 'bob@example.com'
    
    # Test MS Graph format attendees
    assert 'attendees' in msgraph_payload
    msgraph_attendees = msgraph_payload['attendees']
    assert len(msgraph_attendees) == 2
    assert msgraph_attendees[0]['emailAddress']['address'] == 'alice@example.com'
    assert msgraph_attendees[1]['emailAddress']['address'] == 'bob@example.com'


def test_minimal_event():
    """Test handling of minimal event data with missing fields."""
    minimal_event = {
        'title': 'Minimal Event',
        'start_time': '2024-12-15T10:00:00Z'
    }
    
    # Test ICS generation doesn't fail with minimal data
    ics_output = TemporalExport.to_ics(minimal_event)
    assert 'SUMMARY:Minimal Event' in ics_output
    assert 'UID:' in ics_output  # Should generate a UID
    
    # Test Google format
    google_payload = TemporalExport.to_google(minimal_event)
    assert google_payload['summary'] == 'Minimal Event'
    assert 'start' in google_payload
    
    # Test MS Graph format
    msgraph_payload = TemporalExport.to_msgraph(minimal_event)
    assert msgraph_payload['subject'] == 'Minimal Event'
    assert 'start' in msgraph_payload


def test_none_end_time():
    """Test graceful handling of None end_time."""
    event_with_none_end = {
        'title': 'Event with No End',
        'start_time': '2024-12-15T10:00:00Z',
        'end_time': None
    }
    
    # Should not crash with None end_time
    ics_output = TemporalExport.to_ics(event_with_none_end)
    google_payload = TemporalExport.to_google(event_with_none_end)
    msgraph_payload = TemporalExport.to_msgraph(event_with_none_end)
    
    # ICS should have start but may not have end
    assert 'SUMMARY:Event with No End' in ics_output
    assert 'DTSTART:' in ics_output
    
    # Google and MS Graph should have start
    assert 'start' in google_payload
    assert 'start' in msgraph_payload


def test_default_timezone():
    """Test that UTC is used as default timezone when metadata.tzid is not present."""
    event_no_tz = {
        'title': 'No Timezone Event',
        'start_time': '2024-12-15T10:00:00Z',
        'end_time': '2024-12-15T11:00:00Z'
    }
    
    google_payload = TemporalExport.to_google(event_no_tz)
    msgraph_payload = TemporalExport.to_msgraph(event_no_tz)
    
    assert google_payload['start']['timeZone'] == 'UTC'
    assert google_payload['end']['timeZone'] == 'UTC'
    assert msgraph_payload['start']['timeZone'] == 'UTC'
    assert msgraph_payload['end']['timeZone'] == 'UTC'