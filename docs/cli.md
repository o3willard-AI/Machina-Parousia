# CLI Reference — `parousia-guard`

Every command, flag, and behavior below is transcribed from real `parousia-guard --help` / `parousia-guard <command> --help` output. `parousia-guard` is the console script installed with the package: Postfix pipes inbound mail to `/usr/local/bin/parousia-guard ingest`, agents spawn `parousia-guard serve --mcp` for stdio MCP sessions, and operators use the rest for setup and administration.

## Top level

```
Usage: parousia-guard [OPTIONS] COMMAND [ARGS]...

  Parousia Agentic Mail Server Guard.

  Postfix pipe-to-agent gateway with push-based ingest, MCP outbound tool, and
  Redis-backed rate limiting.

Options:
  --version  Show the version and exit.

Commands:
  approval        Manage the email approval queue.
  ingest          Read raw email from stdin, parse, and forward to agent...
  invite          Manage invite keys for gated onboarding.
  migrate-agents  MP-7: migrate the legacy `agents:` block into accounts.db.
  monitor         Monitor Parousia health metrics in real-time.
  serve           Start Parousia guard servers.
  setup           Configure Parousia components: Postfix aliases or DKIM...
  spatial         Manage Parousia spatial subsystem — browser automation...
  status          Show operational status: rate limits, mail queue,...
  temporal        Manage Parousia temporal subsystem — calendar events,...
  test            Send a test email via localhost:25 and print SMTP...
  validate        Validate the Parousia installation — Postfix, Redis,...
  vault           Manage the browser credential store (admin/human use...
```

---

## `ingest`

```
Usage: parousia-guard ingest [OPTIONS]

  Read raw email from stdin, parse, and forward to the REST ingest endpoint.

Options:
  --help  Show this message and exit.
```

The Postfix pipe command. Takes **no flags** — no `--config`, no `--db`: it reads the raw RFC 822 message from stdin, parses it with the stdlib `email` module, derives `agent_id` from the local part of the recipient address, and POSTs to `http://127.0.0.1:8080/ingest` via stdlib `urllib` (`src/parousia/guard/ingest.py`).

Exit codes: `0` on success (and on empty stdin — nothing to bounce), `75` (EX_TEMPFAIL) on MIME parse failure or an unreachable REST server — Postfix queues and retries on non-zero. See [getting-started.md → 5b](getting-started.md#5b-the-pipe-command--parousia-guard-ingest) for the canonical `/ingest` payload.

## `serve`

```
Usage: parousia-guard serve [OPTIONS]

  Start Parousia guard servers.

Options:
  --rest              Start REST ingress server
  --mcp               Start MCP outbound server (stdio transport)
  --mcp-sse           Start MCP outbound server (SSE transport)
  --mcp-host TEXT     MCP SSE bind address (default: 127.0.0.1, loopback —
                      pass 0.0.0.0 explicitly to expose)
  --mcp-port INTEGER  MCP SSE port (default: 8081)
  --all               Start REST + MCP SSE servers
  --help              Show this message and exit.
```

MP-13 fail-closed defaults:

- **bare `serve`** → REST only, hardcoded to loopback `127.0.0.1:8080`
- `--rest` → REST only (same bind)
- `--mcp` → MCP over stdio only — spawned per-agent as a subprocess, no port
- `--mcp-sse` → MCP over SSE only, on `--mcp-host`:`--mcp-port` (default `127.0.0.1:8081`)
- `--all` → REST (daemon thread) + MCP SSE

## `setup`

```
Usage: parousia-guard setup [OPTIONS]

  Configure Parousia components: Postfix aliases or DKIM keys.

Options:
  --postfix      Configure Postfix aliases for pipe-to-agent delivery.
  --dkim         Generate DKIM keys and DNS records (Story 6).
  --tls          Set up TLS certificates for Postfix via Let's Encrypt.
  --postfwd      Configure postfwd Tier 2 SMTP rate limiting.
  --config       Generate /etc/parousia/config.yaml with defaults.
  --domain TEXT  Domain for TLS certificate.
  --email TEXT   Email for Let's Encrypt notifications (for --tls).
  --staging      Use Let's Encrypt staging (for --tls).
  --dry-run      Preview changes without applying (for --tls).
  --rotate       Rotate DKIM keys: save with a timestamp suffix and keep the
                 old key (for --dkim).
  --help         Show this message and exit.
```

- `setup --config` generates `/etc/parousia/config.yaml` with defaults (falls back to `~/.parousia/config.yaml` when `/etc` is not writable). There is no webhook configuration — set your domain and rate limits.
- `setup --postfix` appends `agent-alias: "|/usr/local/bin/parousia-guard ingest"` to `/etc/aliases` and runs `newaliases`.
- `setup --dkim` generates the keypair under `dkim.key_dir` (default `/etc/parousia/dkim`) and prints the DNS records to add. `--rotate` keeps the old key and saves the new one with a timestamp suffix.
- `setup --postfwd` runs `scripts/postfwd-setup.sh` via sudo.
- `setup --tls` drives certbot; `--domain`, `--email`, `--staging`, `--dry-run` qualify it.

## `invite`

```
Commands:
  create  Generate a one-time invite key.
  list    List invite keys.
  revoke  Revoke an unused invite key.
```

```bash
parousia-guard invite create --sponsor stephen
# --sponsor TEXT (default: stephen), --contact TEXT (sponsor contact info),
# --note TEXT, --max-uses INTEGER (default: 1), --expires-in-days INTEGER (0 = never)

parousia-guard invite list --status unused|used|revoked
parousia-guard invite revoke <CODE>
```

Onboarding is invite-gated by default (`invites.require_invite: true`), so a fresh host can onboard nobody until an invite exists — see [ONBOARDING.md](../ONBOARDING.md).

## `migrate-agents`

```
Usage: parousia-guard migrate-agents [OPTIONS]

  MP-7: migrate the legacy `agents:` block into accounts.db.

Options:
  --db TEXT      AccountStore DB path (default: config account_store.db_path).
  --config TEXT  Legacy config YAML path (default: /etc/parousia/config.yaml
                 then ~/.parousia/config.yaml).
```

Idempotent — safe to run repeatedly. Config-only agents are created with **no API key** (`api_key_hash=''`; they authenticate via ingest/stdio, never the API path). Existing rows get rate limit/status refreshed; key/tier/email are never touched. After it succeeds, the `agents:` block can be deleted from the config file. Running it with no legacy block reports the accounts.db account count and changes nothing.

## `approval`

```
Commands:
  approve  Approve and send a pending email.   args: APPROVAL_ID
  list     List pending approval items.
  reject   Reject a pending email.            args: APPROVAL_ID, --reason TEXT
```

## `status`

Show operational status: rate limits, mail queue, recent logs. No flags.

## `monitor`

```
Options:
  --interval INTEGER  Refresh interval in seconds
  --once              Print metrics once and exit
```

## `validate`

Validate the Parousia installation — Postfix, Redis, aliases, config. No flags. Prints a ✓/✗ per check and exits `1` if any check failed, `0` after "All checks passed".

## `test`

```
Options:
  --to TEXT  Recipient email address for test message.  [required]
```

Sends a test email via `localhost:25` and prints the SMTP response.

## `temporal`

```
Commands:
  db        Database management — stats and maintenance.
  export    Export temporal event(s) to .ics, Google Calendar, or MS Graph format.
  ingest    Ingest calendar data from .ics file or JSON.
  pulse     Generate and optionally send a monthly nomination pulse.
  setup     Initialize the temporal database and optional pulse cron.
  status    Show temporal event counts, DB size, last pulse, journal stats.
  validate  Check temporal DB connectivity, schema, and serializer output.
```

Key flags (verified via `--help`):

| Subcommand | Flags |
|------------|-------|
| `temporal db` | `--stats` (table sizes/row counts), `--vacuum` (VACUUM on SQLite / ANALYZE on PostgreSQL) |
| `temporal export` | `--format [ics\|google\|msgraph]`, `--event-id TEXT`, `-o/--output TEXT` (file; default stdout), `--agent-id TEXT` |
| `temporal ingest` | `--ics TEXT` (path), `--json TEXT` (single object), `--agent-id TEXT` |
| `temporal pulse` | `--agent-id TEXT`, `--dry-run` (print the prompt without sending) |
| `temporal setup` | `--pg TEXT` (PostgreSQL connection URL), `--pulse` (install monthly pulse cron), `--force` (overwrite existing DB) |
| `temporal status` | `--agent-id TEXT`, `--json` |
| `temporal validate` | `--agent-id TEXT` |

## `spatial`

```
Commands:
  cleanup   Clean up idle browser profiles.   --days INTEGER (remove profiles older than N days)
  setup     Install required dependencies for spatial functionality.
  status    Show spatial subsystem status.
  validate  Validate spatial subsystem dependencies.
```

## `vault`

```
Usage: parousia-guard vault [OPTIONS] COMMAND [ARGS]...

  Manage the browser credential store (admin/human use only).

  Credentials are encrypted at rest (Fernet) and keyed by (origin, handle).
  Agents never add or read secrets — they request origin-bound fills via the
  fill_credentials MCP tool, which returns a fill summary only.

Options:
  --db TEXT   Credential store DB path (default: config credential_store.db_path).
  --key TEXT  Fernet key file path (default: config credential_store.key_path).

Commands:
  add     Store (or update) a credential for an origin.
  list    List stored credentials — metadata only (never usernames/passwords).
  remove  Delete a stored credential.
```

```bash
parousia-guard vault add --origin https://site.example --handle personal
# --username / --password omitted → prompted (password hidden)

parousia-guard vault list
parousia-guard vault remove --origin https://site.example --handle personal
```

---

## REST `/admin` endpoints

The REST server (loopback `127.0.0.1:8080`) exposes admin endpoints guarded by the **`PAROUSIA_ADMIN_KEY`** environment variable (`src/parousia/guard/rest_server.py`). Every request must carry `Authorization: Bearer <PAROUSIA_ADMIN_KEY>`; with the env var unset they answer `501 Admin API not configured (set PAROUSIA_ADMIN_KEY)`, and a wrong key gets `403 Admin access required`.

| Endpoint | Body | Returns |
|----------|------|---------|
| `POST /admin/accounts` | `{account_id, tier?, email?, display_name?}` | `{account_id, api_key, tier}` — the API key is shown exactly once |
| `POST /admin/accounts/{account_id}/suspend` | — | `{account_id, status: "suspended"}`; `404` if the account is unknown |
| `POST /admin/accounts/{account_id}/reactivate` | — | `{account_id, status: "active"}`; `404` if the account is unknown |

---

Related: [getting-started.md](getting-started.md) · [architecture.md](architecture.md) · [ONBOARDING.md](../ONBOARDING.md)