"""Translation layer for outbound calendar formats.

Generates .ics (iCalendar RFC 5545), Google Calendar API v3 JSON payloads,
and Microsoft Graph API JSON payloads from temporal event rows.
"""

import uuid
from datetime import datetime
from typing import Dict, List, Any, Optional
from dateutil import parser as dateutil_parser
from icalendar import Calendar, Event

__all__ = ["TemporalExport"]


class TemporalExport:
    """Static methods to generate calendar payloads from temporal event rows."""
    
    @staticmethod
    def to_ics(event_row: Dict[str, Any]) -> str:
        """Generate RFC 5545 .ics VEVENT using icalendar library.
        
        Args:
            event_row: Dict containing event data with keys like title, start_time, end_time, etc.
            
        Returns:
            str: iCalendar format string
        """
        cal = Calendar()
        cal.add('prodid', '-//Parousia//Temporal Export//EN')
        cal.add('version', '2.0')
        
        event = Event()
        
        # Handle UID - use from metadata if present, else generate
        metadata = event_row.get('metadata', {})
        if isinstance(metadata, dict) and 'uid' in metadata:
            event_uid = metadata['uid']
        else:
            event_uid = str(uuid.uuid4())
        event.add('uid', event_uid)
        
        # Add basic fields
        event.add('summary', event_row.get('title', 'Untitled Event'))
        
        if event_row.get('description'):
            event.add('description', event_row['description'])
        
        # Handle dates - parse ISO 8601 strings
        start_time_str = event_row.get('start_time')
        if start_time_str:
            start_dt = dateutil_parser.isoparse(start_time_str)
            event.add('dtstart', start_dt)
        
        end_time_str = event_row.get('end_time')
        if end_time_str:
            end_dt = dateutil_parser.isoparse(end_time_str)
            event.add('dtend', end_dt)
        
        # Handle timezone - default to UTC, use metadata.tzid if present
        timezone = 'UTC'
        if isinstance(metadata, dict) and 'tzid' in metadata:
            timezone = metadata['tzid']
        
        cal.add_component(event)
        return cal.to_ical().decode()
    
    @staticmethod
    def to_google(event_row: Dict[str, Any]) -> Dict[str, Any]:
        """Generate Google Calendar API v3 payload.
        
        Args:
            event_row: Dict containing event data
            
        Returns:
            Dict: Google Calendar API v3 event payload
        """
        payload = {
            'summary': event_row.get('title', 'Untitled Event')
        }
        
        if event_row.get('description'):
            payload['description'] = event_row['description']
        
        # Handle dates
        metadata = event_row.get('metadata', {})
        timezone = 'UTC'
        if isinstance(metadata, dict) and 'tzid' in metadata:
            timezone = metadata['tzid']
        
        start_time_str = event_row.get('start_time')
        if start_time_str:
            payload['start'] = {
                'dateTime': start_time_str,
                'timeZone': timezone
            }
        
        end_time_str = event_row.get('end_time')
        if end_time_str:
            payload['end'] = {
                'dateTime': end_time_str,
                'timeZone': timezone
            }
        
        # Handle stakeholders - convert comma-split string to attendee array
        stakeholders = event_row.get('stakeholders', '')
        if stakeholders and isinstance(stakeholders, str):
            attendees = []
            for email in stakeholders.split(','):
                email = email.strip()
                if email:
                    attendees.append({'email': email})
            if attendees:
                payload['attendees'] = attendees
        
        # Handle event ID - strip 'agent_id:' prefix if present
        event_id = event_row.get('event_id', '')
        if event_id.startswith('agent_id:'):
            event_id = event_id[len('agent_id:'):]
        if event_id:
            payload['id'] = event_id
        
        return payload
    
    @staticmethod
    def to_msgraph(event_row: Dict[str, Any]) -> Dict[str, Any]:
        """Generate MS Graph API payload.
        
        Args:
            event_row: Dict containing event data
            
        Returns:
            Dict: Microsoft Graph API event payload
        """
        payload = {
            'subject': event_row.get('title', 'Untitled Event')
        }
        
        if event_row.get('description'):
            payload['body'] = {
                'contentType': 'text',
                'content': event_row['description']
            }
        
        # Handle dates
        metadata = event_row.get('metadata', {})
        timezone = 'UTC'
        if isinstance(metadata, dict) and 'tzid' in metadata:
            timezone = metadata['tzid']
        
        start_time_str = event_row.get('start_time')
        if start_time_str:
            payload['start'] = {
                'dateTime': start_time_str,
                'timeZone': timezone
            }
        
        end_time_str = event_row.get('end_time')
        if end_time_str:
            payload['end'] = {
                'dateTime': end_time_str,
                'timeZone': timezone
            }
        
        # Handle stakeholders - convert comma-split string to attendee array
        stakeholders = event_row.get('stakeholders', '')
        if stakeholders and isinstance(stakeholders, str):
            attendees = []
            for email in stakeholders.split(','):
                email = email.strip()
                if email:
                    attendees.append({
                        'emailAddress': {
                            'address': email
                        }
                    })
            if attendees:
                payload['attendees'] = attendees
        
        return payload
    
    @staticmethod
    def to_all_formats(event_row: Dict[str, Any]) -> Dict[str, Any]:
        """Generate all calendar formats for the event.
        
        Args:
            event_row: Dict containing event data
            
        Returns:
            Dict: Contains 'ics', 'google', and 'msgraph' keys with respective payloads
        """
        return {
            'ics': TemporalExport.to_ics(event_row),
            'google': TemporalExport.to_google(event_row),
            'msgraph': TemporalExport.to_msgraph(event_row)
        }