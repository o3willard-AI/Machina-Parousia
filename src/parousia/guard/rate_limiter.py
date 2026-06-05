"""Redis-backed token-bucket rate limiter for Parousia Guard.

Per-agent limit: 100 emails/hour. Domain-wide limit: 500/day.
Fails open (ALLOW) when Redis is unreachable.
"""

import logging
from typing import Tuple

logger = logging.getLogger("parousia.rate_limiter")


class RateLimiter:
    """Token-bucket rate limiter backed by Redis.

    Uses INCR + EXPIRE pattern for efficient counting with auto-expiry.
    """

    def __init__(self, redis_client, per_agent_per_hour: int = 100, domain_per_day: int = 500):
        self._redis = redis_client
        self.per_agent_per_hour = per_agent_per_hour
        self.domain_per_day = domain_per_day

    def check(self, agent_id: str) -> Tuple[bool, int, int]:
        """Check if an agent is allowed to send email.

        Args:
            agent_id: The agent identifier (e.g., "hermes").

        Returns:
            (allowed, remaining, reset_seconds) tuple.
        """
        agent_key = f"rate:agent:{agent_id}"
        domain_key = "rate:domain"

        try:
            # Per-agent check
            agent_count = self._redis.incr(agent_key)
            if agent_count == 1:
                self._redis.expire(agent_key, 3600)  # 1-hour window

            agent_remaining = max(0, self.per_agent_per_hour - agent_count)
            agent_reset = self._redis.ttl(agent_key)

            # Domain-wide check
            domain_count = self._redis.incr(domain_key)
            if domain_count == 1:
                self._redis.expire(domain_key, 86400)  # 24-hour window

            domain_remaining = max(0, self.domain_per_day - domain_count)
            domain_reset = self._redis.ttl(domain_key)

            if agent_count > self.per_agent_per_hour:
                logger.warning(
                    "agent rate limit exceeded",
                    extra={"agent_id": agent_id, "count": agent_count},
                )
                return (False, 0, agent_reset)

            if domain_count > self.domain_per_day:
                logger.warning(
                    "domain rate limit exceeded",
                    extra={"domain_count": domain_count},
                )
                return (False, 0, domain_reset)

            effective_remaining = min(agent_remaining, domain_remaining)
            return (True, effective_remaining, max(agent_reset, domain_reset))

        except Exception as e:
            # Redis down — fail open (allow), log warning
            logger.warning(
                "redis unavailable — rate limiting disabled",
                extra={"error": str(e)},
            )
            return (True, 999, 0)

    def get_agent_count(self, agent_id: str) -> int:
        """Get current count for an agent (for status display)."""
        try:
            val = self._redis.get(f"rate:agent:{agent_id}")
            return int(val) if val else 0
        except Exception:
            return 0

    def get_domain_count(self) -> int:
        """Get current domain-wide count (for status display)."""
        try:
            val = self._redis.get("rate:domain")
            return int(val) if val else 0
        except Exception:
            return 0
