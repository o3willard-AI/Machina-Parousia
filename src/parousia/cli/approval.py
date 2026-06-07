"""CLI commands for human-in-the-loop email approval."""

import click
import redis as redis_lib

from parousia.config import load_config
from parousia.guard.approval_queue import ApprovalQueue
from parousia.guard.email_sender import send_email as _smtp_send


@click.group("approval")
def approval_group():
    """Manage the email approval queue."""


@approval_group.command("list")
def list_pending():
    """List pending approval items."""
    config = load_config()
    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )
    q = ApprovalQueue(r)
    items = q.list_pending()
    if not items:
        click.echo("No pending approval items.")
        return
    for item in items:
        click.echo(
            f"[{item['approval_id']}] {item['agent_id']} → {item['to']}: "
            f"{item['subject'][:80]}"
        )


@approval_group.command("approve")
@click.argument("approval_id")
def approve(approval_id):
    """Approve and send a pending email."""
    config = load_config()
    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )
    q = ApprovalQueue(r)
    item = q.approve(approval_id)
    if not item:
        click.echo(
            f"Error: approval item {approval_id} not found or already processed.",
            err=True,
        )
        raise SystemExit(1)
    try:
        msg_id = _smtp_send(
            to=item["to"], subject=item["subject"], body=item["body"],
            from_addr=item["from_addr"], reply_to=item.get("reply_to"),
        )
        click.echo(f"Approved and sent: {msg_id}")
    except Exception as e:
        click.echo(f"Approved but send failed: {e}", err=True)
        raise SystemExit(1)


@approval_group.command("reject")
@click.argument("approval_id")
@click.option("--reason", default="", help="Rejection reason")
def reject(approval_id, reason):
    """Reject a pending email."""
    config = load_config()
    r = redis_lib.Redis(
        host=config.redis.host, port=config.redis.port,
        db=config.redis.db, socket_connect_timeout=2,
    )
    q = ApprovalQueue(r)
    item = q.reject(approval_id, reason)
    if item:
        click.echo(f"Rejected: {approval_id}")
    else:
        click.echo(
            f"Error: approval item {approval_id} not found or already processed.",
            err=True,
        )
        raise SystemExit(1)
