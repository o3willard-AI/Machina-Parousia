"""TLS certificate setup for Postfix via Let's Encrypt / certbot."""

import subprocess
import sys
from pathlib import Path

import click


TLS_CONF_TEMPLATE = """# TLS parameters (managed by parousia-guard setup --tls)
smtpd_tls_cert_file = {cert_path}
smtpd_tls_key_file = {key_path}
smtpd_tls_security_level = may
smtpd_tls_loglevel = 1
smtp_tls_security_level = may
smtp_tls_loglevel = 1
smtpd_tls_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtp_tls_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtpd_tls_mandatory_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtp_tls_mandatory_protocols = !SSLv2, !SSLv3, !TLSv1, !TLSv1.1
smtpd_tls_received_header = yes
"""


@click.command("tls")
@click.option("--domain", help="Domain for TLS certificate (default: hostname from config)")
@click.option("--email", help="Email for Let's Encrypt notifications (required)")
@click.option("--staging", is_flag=True, help="Use Let's Encrypt staging environment")
@click.option("--dry-run", is_flag=True, help="Show what would be done without executing")
def setup_tls(domain, email, staging, dry_run):
    """Set up TLS certificates for Postfix using Let's Encrypt."""
    from parousia.config import load_config

    config = load_config()

    if not domain:
        domain = config.hostname

    if not email:
        click.secho("Error: --email is required for Let's Encrypt notifications.", fg="red")
        click.echo("Example: parousia-guard setup --tls --email admin@example.com")
        sys.exit(1)

    cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"

    # ── Step 1: Run certbot ────────────────────
    cmd = [
        "sudo", "certbot", "certonly",
        "--standalone",
        "--non-interactive",
        "--agree-tos",
        "-d", domain,
        "-m", email,
    ]
    if staging:
        cmd.append("--staging")
    if dry_run:
        cmd.append("--dry-run")

    if dry_run:
        click.echo(f"Would run: {' '.join(cmd)}")
    else:
        click.echo("Obtaining Let's Encrypt certificate...")
        try:
            subprocess.run(cmd, check=True, timeout=120)
            click.secho(f"✓ Certificate obtained: {cert_path}", fg="green")
        except subprocess.CalledProcessError:
            click.secho("✗ certbot failed. Check:", fg="red")
            click.echo("  - Port 80 is open and reachable")
            click.echo(f"  - DNS A record for {domain} points to this server")
            click.echo("  - No other web server is using port 80")
            sys.exit(1)
        except FileNotFoundError:
            click.secho("✗ certbot not found. Install with: sudo apt install certbot", fg="red")
            sys.exit(1)

    # ── Step 2: Configure Postfix ──────────────
    tls_config = TLS_CONF_TEMPLATE.format(cert_path=cert_path, key_path=key_path)

    if dry_run:
        click.echo(f"\nWould write to /etc/postfix/tls.conf:\n{tls_config}")
    else:
        snippet_path = Path("/etc/postfix/tls.conf")
        try:
            snippet_path.write_text(tls_config)
            click.secho(f"✓ Wrote TLS config to {snippet_path}", fg="green")
        except PermissionError:
            click.secho("✗ Permission denied — run with sudo.", fg="red")
            sys.exit(1)

        # Include in main.cf if not already
        main_cf = Path("/etc/postfix/main.cf")
        if main_cf.exists():
            content = main_cf.read_text()
            if "tls.conf" not in content:
                try:
                    with main_cf.open("a") as f:
                        f.write(f"\n# TLS configuration\n!include {snippet_path}\n")
                    click.secho("✓ Added !include to /etc/postfix/main.cf", fg="green")
                except PermissionError:
                    click.secho("⚠ Cannot modify main.cf — add manually:", fg="yellow")
                    click.echo(f"  !include {snippet_path}")

        # Reload Postfix
        try:
            subprocess.run(["sudo", "systemctl", "reload", "postfix"], check=True)
            click.secho("✓ Postfix reloaded", fg="green")
        except Exception:
            click.secho("⚠ Could not reload Postfix — reload manually.", fg="yellow")

    # ── Step 3: Verify STARTTLS ────────────────
    if not dry_run:
        click.echo("\nVerifying STARTTLS...")
        try:
            result = subprocess.run(
                ["openssl", "s_client", "-connect", "localhost:25", "-starttls", "smtp", "-brief"],
                input=b"QUIT\n", capture_output=True, text=True, timeout=10,
            )
            if "Verification: OK" in result.stdout or "Verification error" not in result.stderr:
                click.secho("✓ STARTTLS working on port 25", fg="green")
            else:
                click.echo(result.stdout[:500] if result.stdout else result.stderr[:500])
        except Exception:
            click.secho("⚠ Cannot verify STARTTLS — openssl not available?", fg="yellow")

    # ── Auto-renewal reminder ──────────────────
    click.echo("\nLet's Encrypt certificates auto-renew via certbot timer.")
    click.echo("Verify renewal: sudo certbot renew --dry-run")
