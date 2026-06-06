"""CLI commands for the Parousia temporal (calendar/scheduling) subsystem."""

import json
import os
import sys

import click

from parousia.temporal.db import DEFAULT_DB_PATH


def _get_db(pg_url=None):
    """Connect to temporal database, creating tables if needed."""
    from parousia.temporal.db import TemporalDB
    if pg_url:
        db = TemporalDB(postgres_url=pg_url)
    else:
        db_path = os.environ.get("PAROUSIA_TEMPORAL_DB", DEFAULT_DB_PATH)
        db = TemporalDB(db_path=db_path)
    db.connect()
    return db


# ── CLI group ────────────────────────────────────────────────────────────

@click.group(name="temporal")
def temporal_group():
    """Manage Parousia temporal subsystem — calendar events, journal, pulse."""
    pass


# ── setup ────────────────────────────────────────────────────────────────

@temporal_group.command()
@click.option("--pg", "pg_url", default=None, help="PostgreSQL connection URL")
@click.option("--pulse", "enable_pulse", is_flag=True, help="Install monthly pulse cron script")
@click.option("--force", is_flag=True, help="Overwrite existing DB if present")
def setup(pg_url, enable_pulse, force):
    """Initialize the temporal database and optional pulse cron."""
    try:
        from parousia.temporal.db import TemporalDB
        db = _get_db(pg_url)

        if not force and not pg_url and os.path.exists(db.db_path):
            click.secho(f"⚠ Database already exists: {db.db_path}", fg="yellow")
            click.echo("  Use --force to overwrite or --pg for PostgreSQL.")
            return

        db.create_tables()
        label = "PostgreSQL" if pg_url else f"SQLite ({db.db_path})"
        click.secho(f"✓ Temporal database initialized — {label}", fg="green")
        db.close()
    except Exception as e:
        click.secho(f"✗ Database setup failed: {e}", fg="red")
        raise SystemExit(1)

    if enable_pulse:
        pulse_cron = os.path.expanduser("~/.parousia/pulse_cron.sh")
        os.makedirs(os.path.dirname(pulse_cron), exist_ok=True)
        with open(pulse_cron, "w") as f:
            f.write(
                "#!/bin/bash\n"
                "# Parousia monthly nomination pulse\n"
                "parousia-guard temporal pulse --agent-id hermes\n"
            )
        os.chmod(pulse_cron, 0o755)
        click.secho(f"✓ Pulse cron script written: {pulse_cron}", fg="green")
        click.echo("  Add to crontab: 0 9 1 * * " + pulse_cron)


# ── validate ─────────────────────────────────────────────────────────────

@temporal_group.command()
@click.option("--agent-id", default="hermes", help="Agent ID for serializer check")
def validate(agent_id):
    """Check temporal DB connectivity, schema, and serializer output."""
    errors = 0

    try:
        db = _get_db()
    except Exception as e:
        click.secho(f"✗ Database connection failed: {e}", fg="red")
        raise SystemExit(1)

    # Schema check
    try:
        events = db.get_events(agent_id)
        journal = db.get_journal(agent_id)
        click.echo("✓ Schema OK — events + journal tables accessible")
    except Exception as e:
        click.secho(f"✗ Schema check failed: {e}", fg="red")
        errors += 1

    # Serializer check
    try:
        from parousia.temporal.serializer import TemporalSerializer
        ser = TemporalSerializer(db)
        dsl = ser.to_dsl(agent_id)
        click.echo(f"✓ Serializer OK — {len(dsl)} chars output")
    except Exception as e:
        click.secho(f"✗ Serializer check failed: {e}", fg="red")
        errors += 1

    db.close()

    if errors:
        click.secho(f"\n✗ Validation failed with {errors} error(s)", fg="red")
        raise SystemExit(1)
    else:
        click.secho("\n✓ Temporal validation passed", fg="green")


# ── status ───────────────────────────────────────────────────────────────

@temporal_group.command()
@click.option("--agent-id", default="hermes", help="Agent ID for event count")
@click.option("--json", "json_output", is_flag=True, help="Output machine-readable JSON")
def status(agent_id, json_output):
    """Show temporal event counts, DB size, last pulse, journal stats."""
    try:
        db = _get_db()
    except Exception as e:
        click.secho(f"✗ Cannot connect to database: {e}", fg="red")
        raise SystemExit(1)

    event_count = db.count_events(agent_id)
    journal_count = db.count_journal(agent_id)
    days = db.days_since_last_journal(agent_id)

    db_size_bytes = 0
    if not db.postgres_url and os.path.exists(db.db_path):
        db_size_bytes = os.path.getsize(db.db_path)

    status_data = {
        "event_count": event_count,
        "journal_count": journal_count,
        "days_since_last_journal": days,
        "db_size_bytes": db_size_bytes,
        "db_path": db.db_path if not db.postgres_url else "PostgreSQL",
        "agent_id": agent_id,
    }

    if json_output:
        click.echo(json.dumps(status_data, indent=2))
    else:
        click.echo(f"Agent:                   {agent_id}")
        click.echo(f"Events:                  {event_count}")
        click.echo(f"Journal entries:         {journal_count}")
        click.echo(f"Days since last journal: {days if days is not None else 'N/A'}")
        click.echo(f"DB size:                 {db_size_bytes / 1024:.1f} KB")
        click.echo(f"DB path:                 {db.db_path if not db.postgres_url else 'PostgreSQL'}")

    db.close()


# ── export ───────────────────────────────────────────────────────────────

@temporal_group.command()
@click.option("--format", "fmt", type=click.Choice(["ics", "google", "msgraph"]),
              default="ics", help="Export format")
@click.option("--event-id", default=None, help="Export single event by ID")
@click.option("--output", "-o", "output_path", default=None, help="Write to file (default: stdout)")
@click.option("--agent-id", default="hermes", help="Agent ID (for multi-event export)")
def export(fmt, event_id, output_path, agent_id):
    """Export temporal event(s) to .ics, Google Calendar, or MS Graph format."""
    try:
        from parousia.temporal.export import TemporalExport
        db = _get_db()
    except Exception as e:
        click.secho(f"✗ Cannot connect to database: {e}", fg="red")
        raise SystemExit(1)

    try:
        if event_id:
            events = db.get_events(agent_id)
            event = next((e for e in events if e.get("id") == event_id), None)
            if event is None:
                click.secho(f"✗ Event not found: {event_id}", fg="red")
                raise SystemExit(1)
            events = [event]
        else:
            events = db.get_events(agent_id)

        if not events:
            click.secho("⚠ No events to export", fg="yellow")
            return

        output_parts = []
        for event in events:
            if fmt == "ics":
                output_parts.append(TemporalExport.to_ics(event))
            elif fmt == "google":
                output_parts.append(json.dumps(TemporalExport.to_google(event), indent=2))
            elif fmt == "msgraph":
                output_parts.append(json.dumps(TemporalExport.to_msgraph(event), indent=2))

        result = "\n".join(output_parts)

        if output_path:
            with open(output_path, "w") as f:
                f.write(result)
            click.secho(f"✓ Exported {len(events)} event(s) to {output_path}", fg="green")
        else:
            click.echo(result)

    finally:
        db.close()


# ── ingest ───────────────────────────────────────────────────────────────

@temporal_group.command()
@click.option("--ics", "ics_path", default=None, help="Path to .ics file")
@click.option("--json", "json_str", default=None, help="JSON event data (single object)")
@click.option("--agent-id", default="hermes", help="Agent ID to ingest for")
def ingest(ics_path, json_str, agent_id):
    """Ingest calendar data from .ics file or JSON."""
    if not ics_path and not json_str:
        click.secho("✗ Provide --ics PATH or --json JSON", fg="red")
        raise SystemExit(1)

    try:
        from parousia.temporal.ingest import TemporalIngest
        db = _get_db()
        ti = TemporalIngest(db)
    except Exception as e:
        click.secho(f"✗ Cannot connect to database: {e}", fg="red")
        raise SystemExit(1)

    try:
        if ics_path:
            if not os.path.exists(ics_path):
                click.secho(f"✗ File not found: {ics_path}", fg="red")
                raise SystemExit(1)
            with open(ics_path) as f:
                ics_text = f.read()
            result = ti.parse_ics(ics_text, agent_id)
            event_ids = result.get("event_ids", [])
            for eid in event_ids:
                click.echo(eid)
            click.secho(f"✓ Ingested {len(event_ids)} event(s) from {ics_path}", fg="green")
        elif json_str:
            payload = json.loads(json_str)
            eid = ti.parse_json(payload, agent_id)
            click.echo(eid)
            click.secho("✓ Event ingested", fg="green")
    finally:
        db.close()


# ── pulse ────────────────────────────────────────────────────────────────

@temporal_group.command()
@click.option("--agent-id", default="hermes", help="Agent ID to send pulse for")
@click.option("--dry-run", is_flag=True, help="Print the prompt without sending")
def pulse(agent_id, dry_run):
    """Generate and optionally send a monthly nomination pulse."""
    try:
        from parousia.temporal.journal import generate_pulse_prompt
        db = _get_db()
    except Exception as e:
        click.secho(f"✗ Cannot connect to database: {e}", fg="red")
        raise SystemExit(1)

    try:
        prompt = generate_pulse_prompt(db, agent_id)

        if dry_run:
            click.echo("=== PULSE PROMPT (dry run) ===")
            click.echo(prompt)
            click.secho("✓ Dry run — no message sent", fg="green")
        else:
            # Print prompt to stdout for agent to consume
            click.echo(prompt)
            click.secho("✓ Pulse prompt generated", fg="green")
    finally:
        db.close()


# ── db ───────────────────────────────────────────────────────────────────

@temporal_group.command()
@click.option("--stats", is_flag=True, help="Show table sizes and row counts")
@click.option("--vacuum", is_flag=True, help="Run VACUUM (SQLite) or ANALYZE (PostgreSQL)")
def db(stats, vacuum):
    """Database management — stats and maintenance."""
    try:
        db = _get_db()
    except Exception as e:
        click.secho(f"✗ Cannot connect to database: {e}", fg="red")
        raise SystemExit(1)

    try:
        if stats:
            events = db.count_events("__all__") if hasattr(db, "count_events") else "?"
            journal = db.count_journal("__all__") if hasattr(db, "count_journal") else "?"
            db_size = os.path.getsize(db.db_path) if not db.postgres_url and os.path.exists(db.db_path) else 0
            click.echo(f"temporal_events:  {events} rows")
            click.echo(f"temporal_journal: {journal} rows")
            click.echo(f"Database size:    {db_size / 1024:.1f} KB")
            click.echo(f"DB path:          {db.db_path if not db.postgres_url else 'PostgreSQL'}")

        if vacuum:
            if db.postgres_url:
                db._conn.execute("ANALYZE")
                click.secho("✓ ANALYZE complete", fg="green")
            else:
                db._conn.execute("VACUUM")
                click.secho("✓ VACUUM complete", fg="green")
    finally:
        db.close()
