"""MP-3 (part B): `parousia-guard vault` — administer the browser credential store.

Admin/human-populated ONLY. The agent-facing surface is the
`fill_credentials` MCP tool, which fills from this store and returns a
summary — it can never add or read a secret value.
"""

import os

import click


def _open_store(db_path: str, key_path: str):
    """Open the credential store at the given paths (defaults from config)."""
    from parousia.config import load_config
    from parousia.vault.credential_store import CredentialStore

    if not db_path or not key_path:
        config = load_config()
        db_path = db_path or config.credential_store.db_path
        key_path = key_path or config.credential_store.key_path

    # Never create the real /var/lib store from a non-writable context:
    # CredentialStore.connect() mkdirs the parent. Let PermissionError
    # surface as a clear CLI error instead.
    return CredentialStore(db_path, key_path)


@click.group()
@click.option("--db", default=None, help="Credential store DB path (default: config credential_store.db_path).")
@click.option("--key", default=None, help="Fernet key file path (default: config credential_store.key_path).")
@click.pass_context
def vault_group(ctx, db, key):
    """Manage the browser credential store (admin/human use only).

    Credentials are encrypted at rest (Fernet) and keyed by
    (origin, handle). Agents never add or read secrets — they request
    origin-bound fills via the fill_credentials MCP tool, which returns a
    fill summary only.
    """
    ctx.ensure_object(dict)
    ctx.obj["db"] = db
    ctx.obj["key"] = key


@vault_group.command()
@click.option("--origin", required=True, help="Origin the credential belongs to (e.g. https://site.example).")
@click.option("--handle", required=True, help="Credential handle (name agents request the fill by).")
@click.option("--username", default=None, help="Username (omit to be prompted).")
@click.option("--password", default=None, help="Password (omit to be prompted hidden).")
@click.pass_context
def add(ctx, origin, handle, username, password):
    """Store (or update) a credential for an origin."""
    store = _open_store(ctx.obj.get("db"), ctx.obj.get("key"))
    store.connect()
    try:
        if username is None:
            username = click.prompt("Username", type=str)
        if password is None:
            password = click.prompt("Password", hide_input=True, type=str)
        cred = store.add(origin, handle, username, password)
        click.secho("✓ Credential stored", fg="green")
        click.echo(f"  Origin:  {cred.origin}")
        click.echo(f"  Handle:  {cred.handle}")
    finally:
        store.close()


@vault_group.command(name="list")
@click.pass_context
def list_credentials(ctx):
    """List stored credentials — metadata only (never usernames/passwords)."""
    store = _open_store(ctx.obj.get("db"), ctx.obj.get("key"))
    store.connect()
    try:
        rows = store.list()
        if not rows:
            click.echo("No credentials stored.")
            return
        click.echo(f"{'ORIGIN':<40s} {'HANDLE':<20s} {'UPDATED'}")
        click.echo("-" * 90)
        for row in rows:
            click.echo(f"{row['origin']:<40s} {row['handle']:<20s} {row['updated_at']}")
    finally:
        store.close()


@vault_group.command()
@click.option("--origin", required=True, help="Origin the credential is stored for.")
@click.option("--handle", required=True, help="Credential handle to remove.")
@click.pass_context
def remove(ctx, origin, handle):
    """Delete a stored credential."""
    store = _open_store(ctx.obj.get("db"), ctx.obj.get("key"))
    store.connect()
    try:
        if store.delete(origin, handle):
            click.secho(f"✓ Removed credential for {origin} handle {handle}", fg="green")
        else:
            click.secho(f"✗ No credential stored for {origin} handle {handle}", fg="red")
    finally:
        store.close()