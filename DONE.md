# What's Done

Complete feature inventory by implementation order. 320 tests passing as of June 2026.

---

## Phase 1 — Email (Stories 1-9)

| Story | Feature | Tests |
|-------|---------|-------|
| 1 | Project scaffold, CLI package (`parousia-guard`), Pydantic config | — |
| 2 | REST ingest endpoint (`POST /ingest`, `GET /health`) | — |
| 3 | MCP outbound endpoint (`send_email` tool) | — |
| 4 | Redis rate limiter (100/hr per agent, 500/day domain) | — |
| 5 | Postfix pipe transport + aliases | — |
| 6 | DKIM key generation CLI (`setup --dkim`) | — |
| 7 | DNS record formatting helper | — |
| 8 | Postfix blast shields (rate delay, concurrency limits) | — |
| 9 | Postfix installation + base config (Ubuntu 24.04) | — |

---

## Backlog Items 2-7

| Item | Feature |
|------|---------|
| 2 | postfwd Tier 2 SMTP rate limiting (`config/postfwd/rules.cf`, `setup --postfwd`) |
| 3 | Human-in-the-loop approval queue (Redis, REST endpoints, CLI) |
| 4 | Multi-agent routing (`from_agent` param on `send_email`, per-agent rate limits) |
| 5 | DKIM inbound validation (`dkimpy` integration in ingest pipeline) |
| 6 | TLS certificates (`setup --tls`, certbot wrapper, Postfix STARTTLS) |
| 7 | Monitoring dashboard (`GET /metrics`, `/dashboard`, `monitor` CLI) |

---

## Phase 2 — Temporal (Stories 10-18)

| Story | Feature |
|-------|---------|
| 10 | Temporal DB schema (SQLite: events + journal tables) |
| 11 | DSL serializer (4 modes: standard, planning, retrospective, full) |
| 12 | MCP `get_temporal_context` tool |
| 13 | MCP `schedule_event` tool |
| 14 | MCP `cancel_event` tool |
| 15 | MCP `set_timer_alarm` tool |
| 16 | MCP `nominate_milestone` tool |
| 17 | .ics email attachment ingest pipeline |
| 18 | Translation layer (iCal, Google Calendar, MS Graph export) |
| 19 | Opportunistic keyword injector + consideration hints |
| 20 | Temporal journal + monthly nomination pulse |
| 21 | Conflict auto-resolution (3-tier flexibility: none/low/high) |
| 22 | MCP `resolve_conflicts` tool |

---

## Phase 3 — Spatial (Stories 16-20)

| Story | Feature |
|-------|---------|
| 16 | SDOM spec + Pydantic models (`sdom_models.py`) |
| 17 | Browser pool manager (Playwright, per-agent profiles, concurrency, idle cleanup) |
| 18 | Spatial SDOM serializer (HTML → SDOM via BeautifulSoup4) |
| 19 | MCP spatial tools: `browse_to`, `interact` (8 actions), `extract_page_state` |
| 20 | Integration tests (11 scenarios with HTML fixtures) |

---

## Onboarding & Multi-Tenancy (Stories A-F)

| Story | Feature |
|-------|---------|
| A | AccountStore + `accounts` table (SQLite, bcrypt-hashed API keys) |
| B | Self-service onboarding endpoint (`POST /onboarding`, key shown once) |
| C | Per-agent inbox storage + ingest wiring + REST inbox endpoints |
| D | MCP auth middleware (`Authorization: Bearer ***, AccountStore validation) |
| E | Admin endpoints (paid account creation, suspend, reactivate) |
| F | `check_inbox` MCP tool + integration polish |

---

## CLI Surface

```
parousia-guard setup [--postfix] [--dkim] [--tls] [--postfwd] [--config]
parousia-guard validate
parousia-guard status
parousia-guard serve
parousia-guard test --to user@example.com
parousia-guard approval list|approve|reject
parousia-guard monitor [--once] [--interval N]
```

---

## MCP Tools (11 total)

| # | Tool | Capability |
|---|------|-----------|
| 1 | `send_email` | 📧 Email |
| 2 | `check_inbox` | 📧 Email |
| 3 | `get_temporal_context` | 🕐 Temporal |
| 4 | `schedule_event` | 🕐 Temporal |
| 5 | `cancel_event` | 🕐 Temporal |
| 6 | `set_timer_alarm` | 🕐 Temporal |
| 7 | `nominate_milestone` | 🕐 Temporal |
| 8 | `resolve_conflicts` | 🕐 Temporal |
| 9 | `browse_to` | 🌐 Spatial |
| 10 | `interact` | 🌐 Spatial |
| 11 | `extract_page_state` | 🌐 Spatial |

---

## Test coverage

- **320 tests total**, all passing
- pytest with `fakeredis` (Redis), in-memory SQLite (`:memory:`), mock SMTP
- Full test suite: `pytest tests/ -v`
- Targeted files: `pytest tests/test_mcp_server.py -v`
