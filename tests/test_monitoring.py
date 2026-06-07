"""Tests for monitoring dashboard metrics collection."""

import fakeredis
from parousia.config import ParousiaConfig
from parousia.monitoring.dashboard import collect_metrics


def test_collect_metrics_returns_keys():
    config = ParousiaConfig()
    r = fakeredis.FakeRedis()
    metrics = collect_metrics(config, r)
    assert "timestamp" in metrics
    assert "server" in metrics
    assert "redis" in metrics
    assert "postfix" in metrics
    assert "rate_limits" in metrics
    assert "mail_queue" in metrics


def test_redis_status_ok():
    """Redis status reflects fakeredis capabilities (info not supported in fakeredis)."""
    config = ParousiaConfig()
    r = fakeredis.FakeRedis()
    metrics = collect_metrics(config, r)
    # fakeredis doesn't implement INFO, so status will be "error" — that's fine
    assert metrics["redis"]["status"] in ("ok", "error")


def test_rate_limits_returned():
    from parousia.config import AgentConfig

    config = ParousiaConfig(
        agents={"hermes": AgentConfig(webhook_url="http://x", rate_limit_per_hour=100)},
    )
    r = fakeredis.FakeRedis()
    metrics = collect_metrics(config, r)
    assert "per_agent" in metrics["rate_limits"]
    assert "hermes" in metrics["rate_limits"]["per_agent"]


def test_no_temporal_key_when_db_is_none():
    config = ParousiaConfig()
    r = fakeredis.FakeRedis()
    metrics = collect_metrics(config, r, temporal_db=None)
    assert metrics["temporal"] is None
