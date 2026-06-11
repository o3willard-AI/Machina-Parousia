"""CLI entry point for parousia-guard."""

import click

from parousia import __version__


@click.group()
@click.version_option(version=__version__, prog_name="parousia-guard")
def cli():
    """Parousia Agentic Mail Server Guard.

    Postfix pipe-to-agent gateway with push-based ingest,
    MCP outbound tool, and Redis-backed rate limiting.
    """
    pass


@cli.command()
@click.option("--postfix", is_flag=True, help="Configure Postfix aliases for pipe-to-agent delivery.")
@click.option("--dkim", is_flag=True, help="Generate DKIM keys and DNS records (Story 6).")
@click.option("--tls", is_flag=True, help="Set up TLS certificates for Postfix via Let's Encrypt.")
@click.option("--postfwd", is_flag=True, help="Configure postfwd Tier 2 SMTP rate limiting.")
@click.option("--config", "gen_config", is_flag=True, help="Generate /etc/parousia/config.yaml with defaults.")
@click.option("--domain", default=None, help="Domain for TLS certificate.")
@click.option("--email", default=None, help="Email for Let's Encrypt notifications (for --tls).")
@click.option("--staging", is_flag=True, help="Use Let's Encrypt staging (for --tls).")
@click.option("--dry-run", is_flag=True, help="Preview changes without applying (for --tls).")
def setup(postfix, dkim, tls, postfwd, gen_config, domain, email, staging, dry_run):
    """Configure Parousia components: Postfix aliases or DKIM keys."""
    if gen_config:
        import os
        import yaml
        from parousia.config import ParousiaConfig

        config_path = "/etc/parousia/config.yaml"
        user_path = os.path.expanduser("~/.parousia/config.yaml")

        # Pick writable path
        if os.access(os.path.dirname(config_path) or "/etc", os.W_OK):
            target = config_path
        else:
            target = user_path

        os.makedirs(os.path.dirname(target), exist_ok=True)

        if os.path.exists(target):
            click.secho(f"⚠ Config already exists: {target}", fg="yellow")
            if not click.confirm("Overwrite?"):
                return

        defaults = ParousiaConfig()
        config_data = {
            "domain": defaults.domain,
            "hostname": defaults.hostname,
            "agents": {},
            "redis": {"host": "localhost", "port": 6379, "db": 0},
            "rate_limits": {"per_agent_per_hour": 100, "domain_per_day": 500},
            "postfix": {"aliases_file": "/etc/aliases", "guard_script": "/usr/local/bin/parousia-guard"},
            "dkim": {"key_dir": "/etc/parousia/dkim", "selector": "default"},
            "server": {"rest_host": "127.0.0.1", "rest_port": 8080, "mcp_host": "0.0.0.0", "mcp_port": 8081},
            "logging": {"level": "info", "format": "json", "output": "syslog"},
        }

        with open(target, "w") as f:
            yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False)

        click.secho(f"✓ Config written to {target}", fg="green")
        click.echo(f"  Edit {target} to set your domain, agent webhooks, and rate limits.")
        return

    if dkim:
        import os
        from parousia.cli.dkim import generate_dkim_keys
        from parousia.cli.dns_records import format_dns_records
        from parousia.config import load_config

        config = load_config()
        click.echo(f"Generating DKIM keys for {config.domain}...")

        public_pem = generate_dkim_keys(
            config.domain, config.dkim.key_dir, config.dkim.selector
        )
        if public_pem is None:
            key_path = os.path.join(config.dkim.key_dir, f"{config.domain}.key")
            click.secho(
                f"⚠ Key already exists: {key_path}\n"
                f"  Use --rotate to rotate keys or delete the existing key.",
                fg="yellow",
            )
            return

        records = format_dns_records(config.domain, config.dkim.selector, public_pem)
        click.secho("✓ DKIM keypair generated", fg="green")
        click.echo(records)
        return

    if not postfix and not tls and not postfwd:
        click.echo("Usage: parousia-guard setup [--postfix | --dkim | --tls | --postfwd | --config]")
        click.echo("  --postfix    Configure Postfix aliases for pipe-to-agent delivery")
        click.echo("  --dkim       Generate DKIM keys and DNS records")
        click.echo("  --tls        Set up TLS certificates via Let's Encrypt")
        click.echo("  --postfwd    Set up postfwd Tier 2 SMTP rate limiting")
        click.echo("  --config     Generate config file with defaults")
        return

    # ── TLS setup ───────────────────────────────────────────────
    if tls:
        from parousia.cli.tls import setup_tls
        setup_tls(domain=domain, email=email, staging=staging, dry_run=dry_run)
        return

    # ── postfwd setup ───────────────────────────────────────────
    if postfwd:
        from pathlib import Path
        import subprocess as _sub
        script = Path(__file__).parent.parent.parent.parent / "scripts" / "postfwd-setup.sh"
        if script.exists():
            click.echo("Configuring postfwd Tier 2 rate limiting...")
            _sub.run(["sudo", "bash", str(script)], check=True)
            click.secho("✓ postfwd configured", fg="green")
        else:
            click.secho(f"✗ postfwd-setup.sh not found at {script}", fg="red")
        return

    # ── Postfix alias setup ──────────────────────────────────────
    import subprocess

    alias_line = 'agent-alias: "|/usr/local/bin/parousia-guard ingest"\n'

    try:
        with open("/etc/aliases", "a") as f:
            f.write(alias_line)
        click.echo("✓ Wrote agent alias to /etc/aliases")
    except PermissionError:
        click.secho("✗ Permission denied: cannot write /etc/aliases. Run with sudo.", fg="red")
        raise SystemExit(1)
    except OSError as e:
        click.secho(f"✗ Failed to write /etc/aliases: {e}", fg="red")
        raise SystemExit(1)

    try:
        subprocess.run(["newaliases"], check=True, capture_output=True, text=True)
        click.echo("✓ Ran newaliases successfully")
    except FileNotFoundError:
        click.secho("✗ newaliases command not found. Is Postfix installed?", fg="red")
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        click.secho(f"✗ newaliases failed: {e.stderr.strip() if e.stderr else e}", fg="red")
        raise SystemExit(1)

    click.secho("Postfix aliases configured. Restart Postfix to apply.", fg="green")


@cli.command()
def validate():
    """Validate the Parousia installation — Postfix, Redis, aliases, config."""
    import subprocess
    import sys

    from parousia.config import load_config

    errors = 0

    # 1. Config validation
    try:
        config = load_config()
        if config.domain == "agents.yourdomain.com":
            click.secho(f"⚠ Config loaded but domain is default: {config.domain}", fg="yellow")
        else:
            click.echo(f"✓ Config: domain={config.domain}")
    except Exception as e:
        click.secho(f"✗ Config error: {e}", fg="red")
        errors += 1

    # 2. Postfix running
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "postfix"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and "active" in result.stdout:
            click.echo("✓ Postfix is running")
        else:
            click.secho(f"✗ Postfix is not running (status: {result.stdout.strip()})", fg="red")
            errors += 1
    except FileNotFoundError:
        click.secho("✗ systemctl not found — cannot check Postfix status", fg="red")
        errors += 1
    except subprocess.TimeoutExpired:
        click.secho("✗ systemctl timed out", fg="red")
        errors += 1

    # 3. Aliases file
    aliases_path = "/etc/aliases"
    import os
    if os.path.exists(aliases_path):
        click.echo(f"✓ Aliases file exists: {aliases_path}")
    else:
        click.secho(f"✗ Aliases file not found: {aliases_path}", fg="yellow")
        # Not a hard error — may not be configured yet

    # 4. Redis reachable
    try:
        import redis
        r = redis.Redis(host=config.redis.host, port=config.redis.port,
                        db=config.redis.db, socket_connect_timeout=3)
        if r.ping():
            click.echo(f"✓ Redis reachable at {config.redis.host}:{config.redis.port}")
        else:
            click.secho(f"✗ Redis ping failed at {config.redis.host}:{config.redis.port}", fg="red")
            errors += 1
    except ImportError:
        click.secho("⚠ redis package not installed — skipping Redis check", fg="yellow")
    except Exception as e:
        click.secho(f"✗ Redis unreachable: {e}", fg="red")
        errors += 1

    if errors:
        click.secho(f"\n✗ Validation failed with {errors} error(s)", fg="red")
        sys.exit(1)
    else:
        click.secho("\n✓ All checks passed", fg="green")


@cli.command()
@click.option("--to", "recipient", required=True, help="Recipient email address for test message.")
def test(recipient):
    """Send a test email via localhost:25 and print SMTP response."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "parousia-guard@localhost"
    msg["To"] = recipient
    msg["Subject"] = "Parousia Test Email"
    msg.set_content("This is a test message from parousia-guard.\n\nIf you receive this, the mail pipeline is working.")

    try:
        with smtplib.SMTP("localhost", 25, timeout=10) as smtp:
            smtp.set_debuglevel(0)
            response = smtp.send_message(msg)
            click.echo(f"✓ Test email sent to {recipient}")
            if response:
                for addr, (code, msg_text) in response.recipients.items():
                    click.echo(f"  {addr}: {code} {msg_text.decode() if isinstance(msg_text, bytes) else msg_text}")
    except ConnectionRefusedError:
        click.secho("✗ Connection refused — is Postfix running on localhost:25?", fg="red")
        raise SystemExit(1)
    except smtplib.SMTPException as e:
        click.secho(f"✗ SMTP error: {e}", fg="red")
        raise SystemExit(1)
    except OSError as e:
        click.secho(f"✗ Network error: {e}", fg="red")
        raise SystemExit(1)


@cli.command()
def status():
    """Show operational status: rate limits, mail queue, recent logs."""
    import subprocess

    from parousia.config import load_config

    config = load_config()

    # 1. Rate limit counters
    click.echo("═══ Rate Limits ═══")
    try:
        from parousia.guard.rate_limiter import RateLimiter
        import redis as redis_lib
        r = redis_lib.Redis(host=config.redis.host, port=config.redis.port,
                            db=config.redis.db, socket_connect_timeout=3)
        r.ping()
        limiter = RateLimiter(r)
        allowed, remaining, reset_secs = limiter.check("default")
        click.echo(f"  Agent 'default': {'OK' if allowed else 'BLOCKED'}, "
                    f"remaining={remaining}, reset in {reset_secs}s")
    except Exception:
        click.secho("  Redis: unavailable", fg="yellow")

    # 2. Mail queue health
    click.echo("\n═══ Mail Queue ═══")
    try:
        result = subprocess.run(["mailq"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            output = result.stdout.strip()
            if not output or "Mail queue is empty" in output:
                click.echo("  Queue is empty")
            else:
                # Count messages
                lines = output.split("\n")
                click.echo(f"  {len(lines)} queue entries")
                for line in lines[:5]:
                    click.echo(f"  {line[:100]}")
        else:
            click.secho(f"  mailq error: {result.stderr.strip()}", fg="yellow")
    except FileNotFoundError:
        click.secho("  mailq not found — is Postfix installed?", fg="yellow")
    except subprocess.TimeoutExpired:
        click.secho("  mailq timed out", fg="yellow")

    # 3. Recent log tail
    click.echo("\n═══ Recent Logs ═══")
    log_files = ["/var/log/mail.log", "/var/log/syslog"]
    for log_path in log_files:
        try:
            result = subprocess.run(
                ["tail", "-n", "20", log_path],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                click.echo(f"  --- {log_path} (last 20 lines) ---")
                for line in result.stdout.strip().split("\n")[-10:]:
                    click.echo(f"  {line[:120]}")
                break
        except Exception:
            continue
    else:
        click.secho("  No readable log files found", fg="yellow")


@cli.command()
def ingest():
    """Read raw email from stdin, parse, and forward to agent webhook."""
    from parousia.guard.ingest import main as ingest_main

    ingest_main()


@cli.command()
@click.option("--rest", "mode_rest", is_flag=True, help="Start REST ingress server")
@click.option("--mcp", "mode_mcp", is_flag=True, help="Start MCP outbound server")
@click.option("--all", "mode_all", is_flag=True, help="Start both servers")
def serve(mode_rest, mode_mcp, mode_all):
    """Start Parousia guard servers."""
    if mode_all or (not mode_rest and not mode_mcp):
        mode_rest = True
        mode_mcp = True

    if mode_rest and mode_mcp:
        # Start both: REST in daemon thread, MCP in main thread
        import threading
        import uvicorn

        click.echo("Starting REST ingress server on 127.0.0.1:8080")
        rest_thread = threading.Thread(
            target=uvicorn.run,
            kwargs={
                "app": "parousia.guard.rest_server:app",
                "host": "127.0.0.1",
                "port": 8080,
                "log_level": "info",
            },
            daemon=True,
        )
        rest_thread.start()

        from parousia.guard.mcp_server import main as mcp_main

        click.echo("Starting MCP outbound server (stdio transport)")
        mcp_main()
        return

    if mode_rest:
        import uvicorn

        click.echo("Starting REST ingress server on 127.0.0.1:8080")
        uvicorn.run(
            "parousia.guard.rest_server:app",
            host="127.0.0.1",
            port=8080,
            log_level="info",
        )
        return

    if mode_mcp:
        from parousia.guard.mcp_server import main as mcp_main

        click.echo("Starting MCP outbound server (stdio transport)")
        mcp_main()
        return


# ── Temporal group (Phase 2) ─────────────────────────────────────────
from parousia.cli.temporal import temporal_group
cli.add_command(temporal_group)

# ── Approval group (Phase 1 backlog Item 3) ──────────────────────────
from parousia.cli.approval import approval_group
cli.add_command(approval_group)

# ── Monitor command (Phase 1 backlog Item 7) ─────────────────────────
from parousia.cli.monitor import monitor
cli.add_command(monitor)

# ── Spatial group (Story 19) ─────────────────────────────────────────
from parousia.cli.spatial import spatial_group
cli.add_command(spatial_group)


if __name__ == "__main__":
    cli()
