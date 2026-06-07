"""Terminal-based health monitor for Parousia."""

import time as _time

import click
import redis as redis_lib

from parousia.config import load_config
from parousia.monitoring.dashboard import collect_metrics


@click.command("monitor")
@click.option("--interval", default=5, help="Refresh interval in seconds")
@click.option("--once", is_flag=True, help="Print metrics once and exit")
def monitor(interval, once):
    """Monitor Parousia health metrics in real-time."""
    config = load_config()
    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )

    while True:
        metrics_data = collect_metrics(config, r)
        click.clear()
        click.secho("=== Parousia Health Monitor ===\n", bold=True)
        click.echo(f"Timestamp: {metrics_data['timestamp']}")
        click.echo(f"Host:      {metrics_data['server']['hostname']}")
        click.echo(f"Uptime:    {metrics_data['server']['uptime_seconds']:.0f}s")

        redis_s = metrics_data["redis"]
        color = "green" if redis_s["status"] == "ok" else "red"
        click.secho(f"Redis:     {redis_s['status']}  ({redis_s.get('used_memory_human', '?')})", fg=color)

        pf = metrics_data["postfix"]
        color = "green" if pf["status"] == "ok" else "yellow" if pf["status"] == "error" else "red"
        click.secho(f"Postfix:   {pf['status']}", fg=color)

        mq = metrics_data["mail_queue"]
        color = "green" if mq.get("size", 0) == 0 else "yellow"
        click.secho(f"Mail Q:    {mq.get('size', '?')} items", fg=color)

        rl = metrics_data["rate_limits"]
        click.echo(f"Rate:      agent={rl['per_agent']}  domain={rl['domain']}/{rl['limits']['domain_per_day']}")

        if metrics_data.get("temporal"):
            t = metrics_data["temporal"]
            click.echo(f"Temporal:  {t.get('events', '?')} events, {t.get('journal_entries', '?')} journal")

        if once:
            break
        _time.sleep(interval)
