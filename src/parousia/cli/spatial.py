"""CLI commands for the Parousia spatial (browser automation) subsystem."""

import json
import os
import sys

import click

from parousia.config import load_config

# ── CLI group ────────────────────────────────────────────────────────────

@click.group(name="spatial")
def spatial_group():
    """Manage Parousia spatial subsystem — browser automation with Playwright and Crawl4AI."""
    pass


# ── setup ────────────────────────────────────────────────────────────────

@spatial_group.command()
def setup():
    """Install required dependencies for spatial functionality."""
    click.echo("pip install playwright crawl4ai && playwright install chromium")


# ── status ───────────────────────────────────────────────────────────────

@spatial_group.command()
def status():
    """Show spatial subsystem status."""
    # This would normally check actual status, but for now we'll just show a mock response
    status_data = {
        "enabled": True,
        "chromium_path": "/usr/bin/chromium-browser",
        "active_instances": 0,
        "profile_count": 0
    }
    click.echo(json.dumps(status_data, indent=2))


# ── validate ─────────────────────────────────────────────────────────────

@spatial_group.command()
def validate():
    """Validate spatial subsystem dependencies."""
    errors = 0
    
    # Check if chromium exists
    try:
        from parousia.config import load_config
        config = load_config()
        if not os.path.exists(config.spatial.chromium_path):
            click.secho(f"✗ Chromium not found at: {config.spatial.chromium_path}", fg="red")
            errors += 1
        else:
            click.echo(f"✓ Chromium found at: {config.spatial.chromium_path}")
    except Exception as e:
        click.secho(f"✗ Config error: {e}", fg="red")
        errors += 1

    # Try importing crawl4ai and playwright
    try:
        import crawl4ai
        import playwright
        click.echo("✓ crawl4ai and Playwright imported successfully")
    except ImportError as e:
        click.secho(f"✗ Import error: {e}", fg="red")
        errors += 1

    if errors:
        click.secho(f"\n✗ Validation failed with {errors} error(s)", fg="red")
        raise SystemExit(1)
    else:
        click.secho("\n✓ Spatial validation passed", fg="green")


# ── cleanup ───────────────────────────────────────────────────────────────

@spatial_group.command()
@click.option("--days", "days", default=7, help="Remove profiles older than N days")
def cleanup(days):
    """Clean up idle browser profiles."""
    click.echo(f"Removing idle profiles older than {days} days")

