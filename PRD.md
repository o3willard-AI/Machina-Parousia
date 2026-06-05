# Parousia — Phase 1 PRD

> **Goal**: Self-hosted Agentic Mail Server that gives AI agents sovereign, permanent
> email identity with push-based ingest, cryptographic authentication, and blast-shield
> rate limiting. No IMAP, no mailboxes, no cloud provider can evict your agents.

**Version**: 1.0 — Phase 1 MVP
**Target**: AWS EC2 Ubuntu 24.04, Python 3.12+, Postfix 3.x
**Domain**: TBD (Hostinger-registered, MX pointed at AWS)

---

## Architecture Overview

```
                          INTERNET
                             │
                    ┌────────┴────────┐
                    │   AWS EC2       │
                    │   Ubuntu 24.04  │
                    │                 │
   inbound mail ───→│ Postfix :25 ────→│ pipe ──→│ parousia-guard │──→ Agent webhook
                    │                 │         │   REST :8080   │    (Clubhouse)
                    │                 │
   Agent ──────────→│ parousia-guard ──→│ Postfix :25 ──→ outbound mail
                    │   MCP  :8081    │
                    │                 │
                    │  Redis :6379    │  rate-limit counters
                    └─────────────────┘
```

### Component Map

| Component | Role | Protocol | Port |
|-----------|------|----------|------|
| Postfix | MTA — accepts inbound, sends outbound | SMTP | 25 |
| parousia-guard REST | Inbound: Postfix pipe → parse → route to agent | HTTP | 8080 |
| parousia-guard MCP | Outbound: agent sends email via tool | MCP (JSON-RPC) | 8081 |
| Redis | Token-bucket rate limit counters | Redis protocol | 6379 |
| Agent (Hermes/MR-Krabs) | Consumes tasks via webhook, sends via MCP | HTTP / MCP | Clubhouse |

---

## Phase 1 Scope

### In Scope

1. **AWS EC2 provisioning** — Ubuntu 24.04, security groups, public IP
2. **Postfix installation + base config** — no Dovecot, no IMAP, no mailboxes
3. **Postfix pipe-to-script transport** — aliases file mapping agent addresses to guard script
4. **parousia-guard CLI** — pip-installable Python package with `setup`, `validate`, `test`, `status` commands
5. **REST ingress endpoint** — FastAPI, accepts parsed email, routes to agent webhook, returns 250 instantly
6. **MCP outbound endpoint** — MCP server exposing `send_email` tool with rate limiting
7. **Rate limiting (Tier 3)** — Redis-backed token bucket, 100/hr per agent, 500/day domain cap
8. **DKIM key generation** — CLI command generates keys, outputs DNS record for Hostinger
9. **Basic blast shields** — `default_destination_rate_delay`, `default_destination_concurrency_limit` in Postfix
10. **Test suite** — unit + integration tests simulating inbound pipe, outbound send, rate-limit exhaust

### Out of Scope (Phase 2+)

- Hostinger DNS/MX record configuration (needs Playwright or manual)
- postfwd policy server (Tier 2 rate limiting)
- Human-in-the-loop batch approval
- Multi-agent routing (Phase 1: single agent webhook)
- DKIM signature VALIDATION of incoming mail
- TLS certificates for Postfix (opportunistic STARTTLS only in Phase 1)
- Monitoring dashboard / alerting
- Docker / Ansible deployment (manual setup + CLI for Phase 1)

---

## User Stories

### US-1: AWS Server Provisioning

**As a** platform engineer
**I want** an Ubuntu 24.04 EC2 instance with a public IP, port 25 open, and SSH access
**So that** Postfix can receive and send mail on the public internet

**Acceptance Criteria:**
- t3.small or better, 20GB gp3, Ubuntu 24.04 LTS
- Security group: SSH (22) from trusted IPs, SMTP (25) from 0.0.0.0/0, HTTP (8080, 8081) from Clubhouse IP
- Elastic IP assigned and attached
- Reverse DNS (PTR) record set to match the MX hostname
- SSH key-based access configured
- `ufw` enabled allowing 22, 25, 8080, 8081
- Instance reachable from Clubhouse via `ssh`

**Technical Notes:**
- AWS blocks port 25 by default on new accounts. Must request removal of the port 25 restriction via AWS Support. This can take 24-48 hours. Phase 1 should include the request and a fallback plan (use submission port 587 with authentication for outbound, or use a relay as temporary measure).
- Elastic IP is required for reverse DNS — ephemeral public IPs cannot have PTR records.

---

### US-2: Postfix Installation & Base Configuration

**As a** platform engineer
**I want** Postfix installed with a minimal configuration focused on agent mail
**So that** the MTA is ready for pipe-to-script transport and outbound delivery

**Acceptance Criteria:**
- `postfix` package installed via apt
- `/etc/postfix/main.cf` configured with:
  - `myhostname = mx.agents.yourdomain.com`
  - `mydomain = agents.yourdomain.com`
  - `myorigin = $mydomain`
  - `inet_interfaces = all`
  - `mydestination = $myhostname, localhost.$mydomain, localhost`
  - `alias_maps = hash:/etc/aliases`
  - `alias_database = hash:/etc/aliases`
  - `default_destination_rate_delay = 3s`
  - `default_destination_concurrency_limit = 2`
  - `smtpd_recipient_limit = 50`
- No Dovecot, no virtual mailboxes, no `home_mailbox`
- `postfix check` exits clean
- Postfix starts and listens on port 25
- `swaks --to test@agents.yourdomain.com --server localhost` accepted (250 OK)

**Technical Notes:**
- The `mydestination` must NOT include `$mydomain` — if it does, Postfix tries local delivery instead of alias expansion. The correct value for pipe-only setup is `$myhostname, localhost.$mydomain, localhost`.
- `default_destination_rate_delay = 3s` ensures even if an agent floods the queue, delivery trickles at max 20 emails/minute per destination domain.
- `default_destination_concurrency_limit = 2` caps parallel connections to any single MX.

---

### US-3: Postfix Pipe-to-Script Transport

**As a** platform engineer
**I want** Postfix aliases that pipe incoming mail directly to the parousia-guard script
**So that** agents receive email tasks instantly via push instead of polling

**Acceptance Criteria:**
- `/etc/aliases` contains:
  ```
  agent: "|/usr/local/bin/parousia-guard ingest"
  ```
- After `newaliases`, email to `agent@agents.yourdomain.com` invokes the guard script
- The raw RFC 822 message is passed to the script's stdin
- Postfix returns 250 OK immediately after the pipe completes (script must return quickly)
- `postmap -q agent hash:/etc/aliases` returns the pipe command
- Script runs as `nobody` user (Postfix default for pipe transport) — requires world-readable config

**Technical Notes:**
- The `ingest` subcommand performs the REST handoff. It must:
  1. Read raw email from stdin
  2. Parse MIME (extract From, To, Subject, body)
  3. POST to the REST ingress endpoint (localhost:8080/ingest)
  4. Exit 0 on success, exit 75 (EX_TEMPFAIL) on transient failure
- Postfix retries on exit 75. Exit 0 = done, exit != 0 and != 75 = bounce.
- The guard REST server listens on localhost only — Postfix pipe hits it from the same host.

---

### US-4: parousia-guard CLI Package

**As a** DevOps engineer
**I want** a single pip-installable package that provides all Parousia functionality
**So that** I can set up, validate, and monitor the mail guard on any Ubuntu host

**Acceptance Criteria:**

Package structure:
```
parousia/
  pyproject.toml
  src/parousia/
    __init__.py
    cli/              # Click-based CLI
      __init__.py
      main.py         # entry point: parousia-guard
      setup.py        # setup command: Postfix aliases, config files
      validate.py     # validate command: check Postfix, DNS, connectivity
      test.py         # test command: send test email, verify receipt
      status.py       # status command: rate limits, queue health
    guard/
      __init__.py
      rest_server.py  # FastAPI REST ingress
      mcp_server.py   # MCP outbound server
      ingest.py       # stdin reader for Postfix pipe
      rate_limiter.py # Redis token bucket
      email_sender.py # SMTP outbound via localhost:25
    config.py         # Config loading from /etc/parousia/config.yaml
  tests/
    test_ingest.py
    test_rate_limiter.py
    test_rest_server.py
    test_mcp_server.py
    test_cli.py
```

CLI commands:
```
parousia-guard setup --postfix          # Write /etc/aliases, run newaliases, validate
parousia-guard setup --dkim             # Generate DKIM keys, output DNS record
parousia-guard validate                 # Check Postfix running, aliases resolve, Redis reachable
parousia-guard test --to agent@test     # Send test email via localhost:25, verify receipt
parousia-guard status                   # Show rate limits, queue size, recent activity
parousia-guard ingest                   # stdin→parse→POST (called by Postfix pipe)
parousia-guard serve --rest             # Start REST ingress server
parousia-guard serve --mcp              # Start MCP outbound server
parousia-guard serve --all              # Start both servers
```

**Acceptance Criteria:**
- `pip install .` from the repo root installs the `parousia-guard` command
- `parousia-guard --version` prints version
- `parousia-guard validate` exits 0 on healthy system, non-zero with diagnostics on failures
- All commands support `--config /path/to/config.yaml`
- Config file at `/etc/parousia/config.yaml` (or `~/.parousia/config.yaml`)

---

### US-5: REST Ingress Endpoint

**As an** agent framework (Hermes, MR-Krabs)
**I want** the guard to accept parsed email and forward it to my webhook
**So that** I receive tasks as structured JSON without parsing MIME or dealing with Postfix

**Acceptance Criteria:**

FastAPI server (`parousia-guard serve --rest`):
- `POST /ingest` — accepts JSON payload, routes to agent webhook
- `GET /health` — returns `{"status": "ok", "redis": true, "postfix": true}`
- Server binds to `127.0.0.1:8080` (security: Postfix pipe hits localhost, agents hit MCP port)

POST /ingest payload:
```json
{
  "sender": "human@example.com",
  "recipient": "agent@agents.yourdomain.com",
  "subject": "Review this PR",
  "body": "Please review https://github.com/...",
  "agent_id": "hermes",
  "raw_mime": "Return-Path: ...\nFrom: ...",
  "dkim_verified": false,
  "spf_verified": false,
  "timestamp": "2026-06-05T22:00:00Z"
}
```

Response (to Postfix pipe, within 2 seconds):
```json
{"status": "accepted", "agent_id": "hermes", "task_id": "abc123"}
```

Agent webhook POST:
```json
{
  "task_type": "email",
  "sender": "human@example.com",
  "subject": "Review this PR",
  "body": "Please review https://github.com/...",
  "task_id": "abc123",
  "raw_mime": "..."
}
```

**Acceptance Criteria:**
- Endpoint returns 200 within 2 seconds (Postfix pipe must not hang)
- Agent webhook timeout is 5 seconds — if unreachable, task is queued and retried
- Rate limit check PASSES before webhook is fired (if agent is rate-limited, reject with 429)
- `agent_id` is extracted from the `To:` header (recipient before @)
- Health endpoint returns Redis connectivity status
- Server logs to syslog with structured JSON

**Technical Notes:**
- The `raw_mime` field is preserved so agents can do their own DKIM verification in Phase 2.
- If the agent webhook is unreachable (Clubhouse down), the guard should queue the task in Redis with a TTL and retry up to 3 times with exponential backoff. This prevents data loss during Clubhouse restarts.
- The ingest subcommand (`parousia-guard ingest`) is the bridge between Postfix pipe and this REST endpoint. It reads stdin, parses MIME, constructs the JSON, and POSTs to localhost:8080/ingest.

---

### US-6: MCP Outbound Endpoint

**As an** AI agent (Hermes, MR-Krabs)
**I want** a discoverable MCP tool to send email with built-in rate limiting
**So that** I can communicate with humans and other agents without worrying about SMTP or rate limits

**Acceptance Criteria:**

MCP server (`parousia-guard serve --mcp`):
- Binds to `0.0.0.0:8081` (reachable from Clubhouse)
- Exposes one tool: `send_email`

Tool schema:
```json
{
  "name": "send_email",
  "description": "Send an email through the Parousia agent mail system. Rate-limited: 100/hr per agent, 500/day domain-wide.",
  "parameters": {
    "type": "object",
    "properties": {
      "to": {"type": "string", "description": "Recipient email address"},
      "subject": {"type": "string", "description": "Email subject line"},
      "body": {"type": "string", "description": "Plain-text email body. Markdown supported."},
      "reply_to": {"type": "string", "description": "Optional Reply-To address (defaults to agent's address)"}
    },
    "required": ["to", "subject", "body"]
  }
}
```

Tool response:
```json
{
  "sent": true,
  "message_id": "<abc123@agents.yourdomain.com>",
  "rate_limit_remaining": 87,
  "rate_limit_reset_seconds": 2340
}
```

**Acceptance Criteria:**
- Agent connects via MCP client (stdio or HTTP transport)
- Agent calls `send_email` → guard checks rate limit → sends via localhost:25 SMTP → returns result
- Rate limit exhausted → returns error with `rate_limit_remaining: 0` and `retry_after_seconds`
- Tool is idempotent: calling twice with same parameters does not send duplicate (configurable)
- Outbound mail uses `From: agent-id@agents.yourdomain.com`
- Redis counters for rate limits are per-agent (keyed on `agent_id` from MCP connection metadata)
- MCP server supports multiple concurrent agent connections

**Technical Notes:**
- The MCP server should use the official Python MCP SDK (`mcp` package). Config in `pyproject.toml`.
- The `agent_id` for rate limiting should come from either an `X-Agent-ID` header in HTTP transport or from the MCP session metadata. For Phase 1, hardcode to a config value or use the connecting IP.
- SMTP send uses `smtplib` to `localhost:25` with no authentication (trusted localhost relay).
- The `reply_to` parameter defaults to the agent's address so humans can reply directly.

---

### US-7: Redis Rate Limiting (Tier 3 — Architecture-Level Guard)

**As a** system operator
**I want** per-agent and domain-wide rate limits enforced at the guard layer
**So that** no single agent can destroy the domain's IP reputation through excessive outbound mail

**Acceptance Criteria:**
- Redis installed and running on `localhost:6379`
- Token bucket implementation:
  - 100 emails/hour per agent (key: `rate:agent:{agent_id}`)
  - 500 emails/day domain-wide (key: `rate:domain`)
  - Rolling window — key expires automatically
- Rate limit check is synchronous — happens before any SMTP send
- Rate limit headers in API responses: `X-RateLimit-Remaining`, `X-RateLimit-Reset`
- `parousia-guard status` shows current counters for all agents
- Redis is configured with `maxmemory 64mb` and `maxmemory-policy allkeys-lru`

**Technical Notes:**
- Use `redis-py` with connection pooling.
- The token bucket uses `INCR` + `EXPIRE` pattern. On first increment, set TTL. On subsequent, check count.
- Graceful degradation: if Redis is down, default to ALLOW (don't block mail, log warning). This prevents Redis from becoming a single point of failure for email delivery.
- Counters are ephemeral — Redis restart clears all rate limits. Acceptable for Phase 1.

---

### US-8: DKIM Key Generation & DNS Record Output

**As a** system operator
**I want** the CLI to generate DKIM keys and output the exact DNS record to configure
**So that** I can copy-paste into Hostinger's DNS interface without manual formatting

**Acceptance Criteria:**
- `parousia-guard setup --dkim` generates a 2048-bit RSA keypair
- Private key stored at `/etc/parousia/dkim/agents.yourdomain.com.key` (mode 600)
- Public key embedded in DNS TXT record output
- Output format:
  ```
  Add this TXT record to Hostinger DNS:
    Name:  default._domainkey.agents.yourdomain.com
    Value: v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQ...
  ```
- Postfix configured to sign outbound mail with this key:
  ```
  milter_default_action = accept
  milter_protocol = 6
  smtpd_milters = inet:localhost:8891
  non_smtpd_milters = inet:localhost:8891
  ```
- `opendkim` package installed and configured
- `parousia-guard validate` checks DKIM key exists and opendkim is running

**Technical Notes:**
- opendkim is the standard milter for DKIM signing. Postfix calls it via milter protocol on localhost:8891.
- The key selector is `default` (can be customized in Phase 2 for key rotation).
- SPF record: manual DNS entry. Output during setup: `Add TXT record: @ → v=spf1 mx -all`
- DMARC: manual DNS entry. Output: `Add TXT record: _dmarc → v=DMARC1; p=quarantine; rua=mailto:admin@...`
- All three DNS records should be printed together during `parousia-guard setup --dkim` so the operator can configure Hostinger in one session.

---

### US-9: Integration Test Suite

**As a** developer
**I want** comprehensive tests covering the full mail pipeline
**So that** I can refactor with confidence and catch regressions before deployment

**Test Cases:**

| Test | What it verifies |
|------|-----------------|
| `test_ingest_parses_mime` | Raw RFC 822 → parsed JSON with correct sender, subject, body |
| `test_ingest_handles_multipart` | Multipart MIME → plain text extracted, HTML ignored |
| `test_ingest_handles_attachments` | MIME with attachment → body extracted, attachment noted |
| `test_rest_ingest_accepts` | POST /ingest → 200, task_id returned |
| `test_rest_ingest_forwards` | POST /ingest → agent webhook receives correct payload |
| `test_rest_ingest_rejects_rate_limited` | Rate limit exhausted → 429 |
| `test_mcp_send_email_succeeds` | MCP send_email tool → SMTP sent, message_id returned |
| `test_mcp_send_email_rate_limited` | 101st call in window → error with retry_after |
| `test_rate_limiter_token_bucket` | Token bucket increments, expires, resets correctly |
| `test_rate_limiter_domain_cap` | Domain aggregate cap blocks after 500 |
| `test_rate_limiter_redis_down_graceful` | Redis unreachable → ALLOW (don't block mail) |
| `test_cli_setup_writes_aliases` | `parousia-guard setup --postfix` → /etc/aliases updated |
| `test_cli_validate_healthy` | `parousia-guard validate` → exit 0 on clean system |
| `test_cli_validate_unhealthy` | Postfix stopped → exit 1 with diagnostics |
| `test_cli_dkim_generates_keys` | `setup --dkim` → keypair created, DNS TXT printed |
| `test_e2e_full_pipeline` | swaks → Postfix → pipe → ingest → REST → agent mock receives |

**Technical Notes:**
- Tests use `pytest` with `pytest-asyncio` for async endpoints.
- Redis tests use `fakeredis` for unit tests, real Redis for integration.
- Postfix integration tests mock the pipe with a Python subprocess simulating stdin.
- Agent webhook is mocked with `httpx` or `responses`.
- E2E test requires a real Postfix instance — mark with `@pytest.mark.e2e` and skip in CI.

---

## Config File Specification

`/etc/parousia/config.yaml`:

```yaml
# Parousia Guard Configuration
# Phase 1 — single agent, single domain

domain: agents.yourdomain.com        # MX domain
hostname: mx.agents.yourdomain.com   # This server's FQDN

# Agent routing
agents:
  hermes:
    webhook_url: http://192.168.101.42:8000/webhook  # or Clubhouse IP
    rate_limit_per_hour: 100
  # Future: add more agents here

# Redis
redis:
  host: localhost
  port: 6379
  db: 0

# Rate limits
rate_limits:
  per_agent_per_hour: 100
  domain_per_day: 500

# Postfix
postfix:
  aliases_file: /etc/aliases
  guard_script: /usr/local/bin/parousia-guard

# DKIM
dkim:
  key_dir: /etc/parousia/dkim
  selector: default

# Server
server:
  rest_host: 127.0.0.1
  rest_port: 8080
  mcp_host: 0.0.0.0
  mcp_port: 8081

# Logging
logging:
  level: info
  format: json                          # json | text
  output: syslog                        # syslog | stdout | file
```

---

## Filesystem Layout (on AWS host)

```
/etc/parousia/
  config.yaml                          # Guard configuration
  dkim/
    agents.yourdomain.com.key           # DKIM private key (mode 600)
    agents.yourdomain.com.txt           # DKIM public key DNS record

/etc/postfix/
  main.cf                              # Postfix configuration
  master.cf                            # (default, no changes needed)

/etc/aliases                           # agent: "|/usr/local/bin/parousia-guard ingest"
/etc/aliases.db                        # (generated by newaliases)

/etc/opendkim/
  KeyTable                             # default._domainkey → key file
  SigningTable                         # *@agents.yourdomain.com → default
  TrustedHosts                         # 127.0.0.1, localhost

/usr/local/bin/parousia-guard          # pip-installed CLI entry point

/var/log/parousia/
  guard.log                            # Structured JSON logs
  ingest.log                           # Inbound mail processing logs
```

---

## Dependency Graph (Implementation Order)

```
US-1 (AWS) ──────────────────────────────┐
                                          ├──→ US-4 (CLI package)
US-2 (Postfix) ────→ US-3 (Pipe Aliases)─┘        │
                                                    ├──→ US-5 (REST ingress)
US-7 (Redis + Rate Limiting) ──────────────────────┤
                                                    ├──→ US-6 (MCP outbound)
US-8 (DKIM) ───────────────────────────────────────┘
                                                    │
US-9 (Tests) ──────────────────────────────────────┘ (ongoing throughout)
```

Phase 1 is complete when US-1 through US-9 all pass acceptance. The system can then:
- Receive email at `agent@agents.yourdomain.com` via public internet
- Pipe it instantly to the guard
- Route it to the Hermes webhook on Clubhouse
- Accept outbound send requests from agents via MCP
- Enforce per-agent rate limits
- Sign outbound mail with DKIM
- Validate its own health via CLI

---

## Edge Cases & Failure Modes

| Scenario | Behavior |
|----------|----------|
| Agent webhook unreachable (Clubhouse down) | Queue task in Redis, retry 3x with backoff. After exhaustion, bounce with 5xx. |
| Redis down | Rate limiter fails open (ALLOW). Log warning. |
| Postfix queue full | Postfix default behavior: defer with 4xx. No guard intervention needed. |
| Disk full on AWS | Postfix defers inbound, refuses outbound. Monitor with `parousia-guard status`. |
| DKIM key compromised | Regenerate with `parousia-guard setup --dkim --rotate`, update DNS. Phase 2 feature. |
| Agent sends to 10,000 recipients (loop) | Caught by `smtpd_recipient_limit = 50` + rate limiter 100/hr. Blast radius: 50 max recipients per email, 100 emails max per hour. |
| Inbound spam to agent address | Postfix accepts, guard delivers to agent. Agent is responsible for content filtering. Phase 2: add spamassassin or rspamd. |
| AWS port 25 restriction not yet lifted | Outbound uses submission port 587 with auth, or temporary SendGrid relay. Inbound via Cloudflare Email Routing → non-standard port. Document as fallback. |

---

## Success Metrics (Phase 1)

- End-to-end latency: email sent → agent webhook receives task in < 5 seconds
- Rate limit enforcement: 101st email in window rejected with clear error
- CLI setup: `parousia-guard setup --postfix --dkim` completes in < 30 seconds on fresh Ubuntu
- Test coverage: >80% line coverage on `src/parousia/`
- DKIM signature: outbound mail verified by `dkimverify` or Gmail's "signed-by" header
- Zero persistent state beyond Redis counters and log files (no mailboxes, no mail spools for agents)
