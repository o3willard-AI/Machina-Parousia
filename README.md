# Machina Parousia

**"The Machine's Presence"** — An agent-native home-base that gives AI agents a presence in the world: email, temporal awareness, and spatial browsing.

## Status: MVP (v0.1.0)

| Phase | Component | Status |
|-------|-----------|--------|
| 1 | Postfix ingress, guard layer, REST API, MCP server | ✅ |
| 2 | Temporal scheduling (SQLite, DSL, 5 MCP tools, iCal export) | ✅ |
| 3 | Spatial browsing (SDOM, browser pool ×6, Crawl4AI tools) | ✅ |
| — | Conflict auto-resolution | ✅ |
| — | Multi-agent routing (agent@domain → per-agent inbox) | 🚧 Ingress works, inbox storage pending |

**Tests:** 127/127 passing (Phase 1–3)

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  External World                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │  Email   │  │ Calendar │  │  Web (Crawl4AI)   │   │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘   │
│       │              │                │              │
│  ┌────▼──────────────▼────────────────▼─────────┐   │
│  │           Parousia Guard (:8080)              │   │
│  │  ┌─────────┐ ┌──────────┐ ┌───────────────┐  │   │
│  │  │ Ingest  │ │ Temporal │ │    Spatial    │  │   │
│  │  │ (email) │ │  Engine  │ │ Browser Pool  │  │   │
│  │  └────┬────┘ └────┬─────┘ └───────┬───────┘  │   │
│  │       │            │              │           │   │
│  │  ┌────▼────────────▼──────────────▼───────┐   │   │
│  │  │          MCP Server (:8081)             │   │   │
│  │  │  check_inbox · schedule_event · browse  │   │   │
│  │  └──────────────────┬─────────────────────┘   │   │
│  └─────────────────────┼─────────────────────────┘   │
│                         │                             │
│                 ┌───────▼───────┐                     │
│                 │  AI Agents    │                     │
│                 │  (6 max)      │                     │
│                 └───────────────┘                     │
└─────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Install
git clone https://github.com/o3willard-AI/Machina-Parousia.git
cd Machina-Parousia
pip install -e .

# Configure
cp config/parousia.example.yaml /etc/parousia/config.yaml
# Edit domain, agents, spatial settings

# Run
python -m parousia.cli.main serve
```

### On the deployed instance (AWS)

```bash
ssh -i ~/.ssh/linus-test-key ubuntu@32.197.57.145
sudo systemctl status parousia-guard
# REST API: http://32.197.57.145:8080
# Health:    http://32.197.57.145:8080/health
```

## MCP Tools

Agents connect to Parousia via MCP at `http://<host>:8081/sse`:

| Tool | Description |
|------|-------------|
| `get_temporal_context` | What's happening now/soon |
| `schedule_event` | Create calendar events |
| `cancel_event` | Remove events by ID |
| `set_timer_alarm` | One-shot timers |
| `nominate_milestone` | Flag dates for monthly pulse |
| `browse_url` | Navigate to a URL, return SDOM |
| `click_element` | Click by SDOM ID |
| `type_text` | Type into input fields |
| `scroll_page` | Scroll in browser viewport |
| `check_inbox` | Read agent's email queue |

## Configuration

```yaml
domain: machinaparousia.ai
server:
  rest_port: 8080
  mcp_port: 8081

agents:
  agent-name:
    webhook_url: "http://localhost:8080/webhook"
    rate_limit_per_hour: 100

spatial:
  enabled: true
  chromium_path: "/snap/bin/chromium"
  max_instances: 6
  idle_timeout_seconds: 300
```

## Deployment

The reference deployment runs on AWS EC2 (m7i-flex.large, Ubuntu 24.04) with:
- Postfix MTA for SMTP ingress
- systemd service (`parousia-guard`)
- Per-agent Chromium profiles
- macOS/Linux compatible

## Backlog

- [ ] Per-agent inbox storage (ingest pipeline complete, final landing needed)
- [ ] MCP `check_inbox` implementation
- [ ] DKIM signing
- [ ] TLS for REST/MCP endpoints
- [ ] Multi-domain support
- [ ] VNC/canvas fallback for spatial

## License

MIT
