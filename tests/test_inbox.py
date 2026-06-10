import os
import tempfile
import pytest
from datetime import datetime
from unittest.mock import patch

from parousia.inbox.inbox_store import InboxStore, InboxMessage


def test_store_and_retrieve_message():
    """Test storing and retrieving a message."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        store = InboxStore(db_path)
        
        # Create a test message
        message = InboxMessage(
            id="test-message-id",
            agent_id="test-agent",
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Test Subject",
            body_text="Test body text",
            body_html="<p>Test body html</p>",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        # Store the message
        stored_id = store.store_message(message)
        
        # Retrieve the message
        retrieved = store.get_message(stored_id)
        
        assert retrieved is not None
        assert retrieved.id == "test-message-id"
        assert retrieved.agent_id == "test-agent"
        assert retrieved.sender == "sender@example.com"
        assert retrieved.recipient == "recipient@example.com"
        assert retrieved.subject == "Test Subject"
        assert retrieved.body_text == "Test body text"
        assert retrieved.body_html == "<p>Test body html</p>"
        assert retrieved.received_at == message.received_at
        assert retrieved.read == False
        assert retrieved.archived == False
        
    finally:
        os.unlink(db_path)


def test_list_messages_by_agent():
    """Test listing messages for an agent."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        store = InboxStore(db_path)
        
        # Create and store multiple messages
        message1 = InboxMessage(
            id="msg1",
            agent_id="agent1",
            sender="sender1@example.com",
            recipient="recipient1@example.com",
            subject="Subject 1",
            body_text="Body 1",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        message2 = InboxMessage(
            id="msg2",
            agent_id="agent1",
            sender="sender2@example.com",
            recipient="recipient2@example.com",
            subject="Subject 2",
            body_text="Body 2",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        message3 = InboxMessage(
            id="msg3",
            agent_id="agent2",
            sender="sender3@example.com",
            recipient="recipient3@example.com",
            subject="Subject 3",
            body_text="Body 3",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        store.store_message(message1)
        store.store_message(message2)
        store.store_message(message3)
        
        # List messages for agent1
        messages = store.list_messages("agent1")
        
        assert len(messages) == 2
        assert messages[0].id == "msg2"  # Should be sorted by received_at DESC
        assert messages[1].id == "msg1"
        
    finally:
        os.unlink(db_path)


def test_list_messages_pagination():
    """Test pagination of messages."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        store = InboxStore(db_path)
        
        # Create and store multiple messages
        for i in range(10):
            message = InboxMessage(
                id=f"msg{i}",
                agent_id="agent1",
                sender=f"sender{i}@example.com",
                recipient=f"recipient{i}@example.com",
                subject=f"Subject {i}",
                body_text=f"Body {i}",
                received_at=datetime.utcnow().isoformat() + 'Z',
                read=False,
                archived=False
            )
            store.store_message(message)
        
        # Test limit and offset
        messages = store.list_messages("agent1", limit=5, offset=3)
        
        assert len(messages) == 5
        assert messages[0].id == "msg6"  # Should be sorted by received_at DESC
        assert messages[4].id == "msg2"
        
    finally:
        os.unlink(db_path)


def test_unread_filter():
    """Test filtering unread messages."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        store = InboxStore(db_path)
        
        # Create and store messages
        message1 = InboxMessage(
            id="msg1",
            agent_id="agent1",
            sender="sender1@example.com",
            recipient="recipient1@example.com",
            subject="Subject 1",
            body_text="Body 1",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=True,
            archived=False
        )
        
        message2 = InboxMessage(
            id="msg2",
            agent_id="agent1",
            sender="sender2@example.com",
            recipient="recipient2@example.com",
            subject="Subject 2",
            body_text="Body 2",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        store.store_message(message1)
        store.store_message(message2)
        
        # List unread messages only
        unread_messages = store.list_messages("agent1", unread_only=True)
        
        assert len(unread_messages) == 1
        assert unread_messages[0].id == "msg2"
        assert unread_messages[0].read == False
        
    finally:
        os.unlink(db_path)


def test_mark_read():
    """Test marking a message as read."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        store = InboxStore(db_path)
        
        # Create and store a message
        message = InboxMessage(
            id="msg1",
            agent_id="agent1",
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Subject 1",
            body_text="Body 1",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        store.store_message(message)
        
        # Verify it's unread
        retrieved = store.get_message("msg1")
        assert retrieved.read == False
        
        # Mark as read
        result = store.mark_read("msg1")
        assert result == True
        
        # Verify it's now read
        retrieved = store.get_message("msg1")
        assert retrieved.read == True
        
        # Try to mark non-existent message (should return False)
        result = store.mark_read("non-existent")
        assert result == False
        
    finally:
        os.unlink(db_path)


def test_archive():
    """Test archiving a message."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        store = InboxStore(db_path)
        
        # Create and store a message
        message = InboxMessage(
            id="msg1",
            agent_id="agent1",
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Subject 1",
            body_text="Body 1",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        store.store_message(message)
        
        # Verify it's not archived
        retrieved = store.get_message("msg1")
        assert retrieved.archived == False
        
        # Archive the message
        result = store.archive("msg1")
        assert result == True
        
        # Verify it's now archived
        retrieved = store.get_message("msg1")
        assert retrieved.archived == True
        
        # Try to archive non-existent message (should return False)
        result = store.archive("non-existent")
        assert result == False
        
    finally:
        os.unlink(db_path)


def test_count_unread():
    """Test counting unread messages."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        store = InboxStore(db_path)
        
        # Create and store messages
        message1 = InboxMessage(
            id="msg1",
            agent_id="agent1",
            sender="sender1@example.com",
            recipient="recipient1@example.com",
            subject="Subject 1",
            body_text="Body 1",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=True,
            archived=False
        )
        
        message2 = InboxMessage(
            id="msg2",
            agent_id="agent1",
            sender="sender2@example.com",
            recipient="recipient2@example.com",
            subject="Subject 2",
            body_text="Body 2",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        message3 = InboxMessage(
            id="msg3",
            agent_id="agent1",
            sender="sender3@example.com",
            recipient="recipient3@example.com",
            subject="Subject 3",
            body_text="Body 3",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=True
        )
        
        store.store_message(message1)
        store.store_message(message2)
        store.store_message(message3)
        
        # Count unread messages for agent1
        count = store.count_unread("agent1")
        assert count == 2  # msg2 is unread, msg3 is archived (should not count)
        
        # Count unread for non-existent agent
        count = store.count_unread("non-existent-agent")
        assert count == 0
        
    finally:
        os.unlink(db_path)


def test_ingest_endpoint_stores_inbox():
    """Integration test: Test that ingest endpoint stores messages in inbox."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Mock the config and agent lookup
        from parousia.config import Config, AgentConfig
        
        # Create a mock config with agent
        config = Config()
        config.agents = {
            "test-agent": AgentConfig(
                webhook_url="http://localhost:8000/webhook",
                max_instances=1,
                rate_limit_per_hour=1000
            )
        }
        
        # Patch the load_config function to return our mock config
        with patch("parousia.guard.rest_server.load_config", return_value=config):
            from parousia.guard.rest_server import ingest, _inbox_store
            from fastapi.testclient import TestClient
            from parousia.guard.rest_server import app
            
            client = TestClient(app)
            
            # Create an ingest request
            ingest_data = {
                "sender": "sender@example.com",
                "recipient": "test-agent@example.com",
                "subject": "Test Subject",
                "body": "Test body text",
                "raw_mime": "From: sender@example.com\nTo: test-agent@example.com\nSubject: Test Subject\n\nTest body text"
            }
            
            # Call the ingest endpoint
            response = client.post("/ingest", json=ingest_data)
            
            assert response.status_code == 200
            
            # Verify that a message was stored in the inbox
            response_data = response.json()
            message_id = response_data["task_id"]  # This is the inbox message ID
            
            # Check if we can retrieve the message
            retrieved_message = _inbox_store.get_message(message_id)
            assert retrieved_message is not None
            assert retrieved_message.agent_id == "test-agent"
            assert retrieved_message.sender == "sender@example.com"
            
    finally:
        os.unlink(db_path)


def test_inbox_endpoint_returns_messages():
    """Integration test: Test that inbox endpoints return messages correctly."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Set up the inbox store
        store = InboxStore(db_path)
        
        # Create and store a message
        message = InboxMessage(
            id="test-message-id",
            agent_id="test-agent",
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Test Subject",
            body_text="Test body text",
            received_at=datetime.utcnow().isoformat() + 'Z',
            read=False,
            archived=False
        )
        
        store.store_message(message)
        
        # Test the inbox endpoint
        from fastapi.testclient import TestClient
        from parousia.guard.rest_server import app
        
        client = TestClient(app)
        
        # Test listing messages
        response = client.get("/inbox", params={"agent_id": "test-agent"})
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-message-id"
        assert data[0]["subject"] == "Test Subject"
        
        # Test getting a specific message
        response = client.get("/inbox/test-message-id")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-message-id"
        assert data["subject"] == "Test Subject"
        
        # Test getting a non-existent message
        response = client.get("/inbox/non-existent")
        assert response.status_code == 404
        
    finally:
        os.unlink(db_path)