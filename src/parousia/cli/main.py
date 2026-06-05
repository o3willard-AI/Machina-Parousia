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
def setup():
    """Configure Postfix, DKIM, and guard components."""
    click.echo("Not yet implemented — Story 5 (Postfix) + Story 6 (DKIM)")


@cli.command()
def validate():
    """Validate the Parousia installation."""
    from parousia.config import load_config

    try:
        config = load_config()
        click.echo(f"✓ Config loaded: domain={config.domain}")
    except Exception as e:
        click.echo(f"✗ Config error: {e}", err=True)
        raise SystemExit(1)

    click.echo("Not yet implemented — Story 5")


@cli.command()
def test():
    """Send a test email and verify delivery."""
    click.echo("Not yet implemented — Story 5")


@cli.command()
def status():
    """Show rate limits, queue health, and recent activity."""
    click.echo("Not yet implemented — Story 5")


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

    if mode_rest:
        import uvicorn

        click.echo("Starting REST ingress server on 127.0.0.1:8080")
        uvicorn.run(
            "parousia.guard.rest_server:app",
            host="127.0.0.1",
            port=8080,
            log_level="info",
        )
        return  # uvicorn.run blocks

    if mode_mcp:
        from parousia.guard.mcp_server import main as mcp_main

        click.echo("Starting MCP outbound server (stdio transport)")
        mcp_main()
        return  # mcp_main blocks


if __name__ == "__main__":
    cli()
