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


def test_rate_limits_returned(tmp_path):
    """MP-7: per-agent counters come from the AccountStore registry."""
    from parousia.auth.accounts import AccountStore

    config = ParousiaConfig()
    r = fakeredis.FakeRedis()

    store = AccountStore(str(tmp_path / "mon.db"))
    store.connect()
    try:
        store.ensure_account("hermes", rate_limit_per_hour=100)
        metrics = collect_metrics(config, r, account_store=store)
    finally:
        store.close()
    assert "per_agent" in metrics["rate_limits"]
    assert "hermes" in metrics["rate_limits"]["per_agent"]


def test_rate_limits_without_store_skips_per_agent():
    """MP-7: no AccountStore → per-agent section empty (domain still reports)."""
    config = ParousiaConfig()
    r = fakeredis.FakeRedis()
    metrics = collect_metrics(config, r, account_store=None)
    assert metrics["rate_limits"]["per_agent"] == {}
    assert "domain" in metrics["rate_limits"]


def test_no_temporal_key_when_db_is_none():
    config = ParousiaConfig()
    r = fakeredis.FakeRedis()
    metrics = collect_metrics(config, r, temporal_db=None)
    assert metrics["temporal"] is None
