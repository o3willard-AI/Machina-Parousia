"""Health metrics collection for Parousia monitoring dashboard."""

import os
import subprocess
import time
from datetime import datetime, timezone


def collect_metrics(config, redis_client, temporal_db=None, account_store=None) -> dict:
    """Collect all health metrics into a single dict.

    Returns a comprehensive metrics dict suitable for JSON serialization
    or dashboard rendering.

    MP-7: the per-agent rate counters iterate the AccountStore registry
    (``account_store``) — the legacy YAML agents block is gone. Without a
    store the per-agent section is empty (the rest of the metrics are
    unaffected).
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server": {
            "uptime_seconds": _get_uptime(),
            "hostname": os.uname().nodename,
        },
        "redis": _check_redis(redis_client),
        "postfix": _check_postfix(),
        "rate_limits": _get_rate_limits(redis_client, account_store, config),
        "mail_queue": _get_mail_queue(),
        "temporal": _get_temporal_stats(temporal_db) if temporal_db else None,
    }


def _get_uptime() -> float:
    """Read system uptime in seconds from /proc/uptime."""
    try:
        with open("/proc/uptime") as f:
            return float(f.readline().split()[0])
    except Exception:
        return 0.0


def _check_redis(redis_client) -> dict:
    """Check Redis connectivity and memory usage."""
    try:
        redis_client.ping()
        info = redis_client.info("memory")
        return {
            "status": "ok",
            "used_memory_human": info.get("used_memory_human", "unknown"),
            "connected_clients": info.get("connected_clients", 0),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_postfix() -> dict:
    """Check if Postfix is running via systemctl."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "postfix"],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip() == "active"
        return {"status": "ok" if active else "down", "active": active}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_rate_limits(redis_client, account_store, config) -> dict:
    """Read current rate limit counters.

    MP-7: the agent list comes from the AccountStore registry (the legacy
    YAML agents block is gone). Without a store, per-agent counters are
    skipped — the domain counter still reports.
    """
    agents = {}
    if account_store is not None:
        try:
            agent_ids = account_store.list_account_ids(active_only=True)
        except Exception:
            agent_ids = []
        for agent_id in agent_ids:
            try:
                count = redis_client.get(f"rate:agent:{agent_id}")
                agents[agent_id] = int(count) if count else 0
            except Exception:
                agents[agent_id] = -1
    try:
        domain_count = redis_client.get("rate:domain")
        domain = int(domain_count) if domain_count else 0
    except Exception:
        domain = -1
    return {
        "per_agent": agents,
        "domain": domain,
        "limits": {
            "per_agent_per_hour": config.rate_limits.per_agent_per_hour,
            "domain_per_day": config.rate_limits.domain_per_day,
        },
    }


def _get_mail_queue() -> dict:
    """Check Postfix mail queue size."""
    try:
        result = subprocess.run(
            ["mailq"], capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.strip()
        if not output or "Mail queue is empty" in output:
            return {"size": 0, "status": "ok"}
        # Parse last line: "-- N Kbytes in M Requests"
        queue_size = 0
        for line in output.split("\n"):
            if "Requests" in line:
                try:
                    queue_size = int(line.strip().split()[-2])
                except (ValueError, IndexError):
                    pass
        return {"size": queue_size, "status": "ok" if queue_size < 100 else "warning"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_temporal_stats(temporal_db) -> dict | None:
    """Get temporal DB statistics."""
    try:
        conn = temporal_db._conn if temporal_db._conn else None
        if not conn:
            return None
        events = conn.execute("SELECT COUNT(*) FROM temporal_events").fetchone()[0]
        journal = conn.execute("SELECT COUNT(*) FROM temporal_journal").fetchone()[0]
        return {"events": events, "journal_entries": journal, "status": "ok"}
    except Exception:
        return None
