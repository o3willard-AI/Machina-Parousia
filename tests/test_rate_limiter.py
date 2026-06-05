"""Tests for Redis-backed rate limiter."""

import time

import pytest
import redis

from parousia.guard.rate_limiter import RateLimiter


@pytest.fixture
def redis_client():
    """Real Redis client — tests require redis-server running or fakeredis."""
    try:
        import fakeredis

        return fakeredis.FakeRedis()
    except ImportError:
        pytest.skip("fakeredis not installed")


@pytest.fixture
def limiter(redis_client):
    return RateLimiter(redis_client, per_agent_per_hour=10, domain_per_day=50)


def test_check_allows_first_call(limiter):
    allowed, remaining, reset = limiter.check("hermes")
    assert allowed is True
    assert remaining >= 0


def test_check_blocks_after_limit(limiter):
    # Exhaust the limit (10/hour)
    for _ in range(10):
        allowed, _, _ = limiter.check("hermes")
        assert allowed is True

    # 11th call should be blocked
    allowed, remaining, reset = limiter.check("hermes")
    assert allowed is False
    assert remaining == 0


def test_check_per_agent_isolation(limiter):
    # Hermes uses 5
    for _ in range(5):
        limiter.check("hermes")

    # Openclaw should be unaffected
    allowed, remaining, _ = limiter.check("openclaw")
    assert allowed is True
    assert remaining >= 5  # hasn't used any


def test_check_domain_cap(limiter):
    # Use a tiny domain cap for testing
    limiter.domain_per_day = 3

    for _ in range(3):
        allowed, _, _ = limiter.check("agent_a")
        assert allowed is True

    # 4th should be blocked by domain cap
    allowed, remaining, _ = limiter.check("agent_b")
    assert allowed is False
    assert remaining == 0


def test_check_graceful_degradation():
    """When Redis is down, rate limiter fails open."""
    # Use a broken Redis connection
    broken_redis = redis.Redis(host="255.255.255.255", port=9999, socket_connect_timeout=0.1)
    limiter = RateLimiter(broken_redis)

    allowed, remaining, _ = limiter.check("hermes")
    assert allowed is True
    assert remaining == 999


def test_get_counts(limiter, redis_client):
    redis_client.flushdb()
    limiter2 = RateLimiter(redis_client, per_agent_per_hour=10, domain_per_day=50)

    limiter2.check("hermes")
    limiter2.check("hermes")
    limiter2.check("openclaw")

    assert limiter2.get_agent_count("hermes") == 2
    assert limiter2.get_agent_count("openclaw") == 1
    assert limiter2.get_domain_count() == 3
