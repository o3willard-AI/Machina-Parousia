"""Redis-backed human-in-the-loop approval queue for outbound email.

Agents can be configured to require human approval before sending.
Emails are held in a Redis queue, reviewed via REST or CLI, then
approved (sent) or rejected (discarded).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("parousia.approval")


class ApprovalQueue:
    """Holds outbound emails pending human approval.

    Uses Redis lists for the pending queue and Redis strings
    (with TTL) for individual item storage.
    """

    QUEUE_KEY = "approval:pending"
    ITEM_PREFIX = "approval:item:"

    def __init__(self, redis_client):
        self._redis = redis_client

    def enqueue(
        self,
        agent_id: str,
        to: str,
        subject: str,
        body: str,
        from_addr: str,
        reply_to: Optional[str] = None,
        ttl_hours: int = 72,
    ) -> str:
        """Place an email in the approval queue.

        Returns the approval_id used to reference the item later.
        """
        approval_id = str(uuid.uuid4())[:12]
        item = {
            "approval_id": approval_id,
            "agent_id": agent_id,
            "to": to,
            "subject": subject,
            "body": body,
            "from_addr": from_addr,
            "reply_to": reply_to,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "approved_at": None,
            "rejected_at": None,
            "reject_reason": None,
        }
        self._redis.setex(
            f"{self.ITEM_PREFIX}{approval_id}",
            ttl_hours * 3600,
            json.dumps(item),
        )
        self._redis.lpush(self.QUEUE_KEY, approval_id)
        logger.info(
            "email queued for approval",
            extra={"approval_id": approval_id, "agent_id": agent_id},
        )
        return approval_id

    def list_pending(self, limit: int = 50) -> list[dict]:
        """List all pending (unreviewed) approval items."""
        ids = self._redis.lrange(self.QUEUE_KEY, 0, limit - 1)
        items = []
        for aid in ids:
            aid_str = aid.decode() if isinstance(aid, bytes) else aid
            raw = self._redis.get(f"{self.ITEM_PREFIX}{aid_str}")
            if raw:
                items.append(json.loads(raw))
        return items

    def get_item(self, approval_id: str) -> Optional[dict]:
        """Retrieve a single approval item by ID."""
        raw = self._redis.get(f"{self.ITEM_PREFIX}{approval_id}")
        return json.loads(raw) if raw else None

    def approve(self, approval_id: str) -> Optional[dict]:
        """Approve a pending email. Returns the item if found and pending."""
        item = self.get_item(approval_id)
        if not item or item["status"] != "pending":
            return None
        item["status"] = "approved"
        item["approved_at"] = datetime.now(timezone.utc).isoformat()
        self._redis.setex(
            f"{self.ITEM_PREFIX}{approval_id}",
            3600,  # Keep approved items for 1h traceability
            json.dumps(item),
        )
        self._redis.lrem(self.QUEUE_KEY, 0, approval_id)
        logger.info("email approved", extra={"approval_id": approval_id})
        return item

    def reject(self, approval_id: str, reason: str = "") -> Optional[dict]:
        """Reject a pending email. Returns the item if found and pending."""
        item = self.get_item(approval_id)
        if not item or item["status"] != "pending":
            return None
        item["status"] = "rejected"
        item["rejected_at"] = datetime.now(timezone.utc).isoformat()
        item["reject_reason"] = reason
        self._redis.setex(
            f"{self.ITEM_PREFIX}{approval_id}",
            3600,
            json.dumps(item),
        )
        self._redis.lrem(self.QUEUE_KEY, 0, approval_id)
        logger.info("email rejected", extra={"approval_id": approval_id, "reason": reason})
        return item
