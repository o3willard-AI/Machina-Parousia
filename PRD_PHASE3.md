# Parousia — Phase 3 PRD: Spatial Browsing ("Native Sight")

> **Goal**: Give AI agents a sovereign, persistent "presence in space" — a browser-aware
> spatial architecture that lets them navigate, interact with, and extract information from
> the web without the token-guzzling overhead of screenshots and pixel-level mouse emulation.
> Built as a co-equal capability alongside email (Phase 1) and temporal scheduling (Phase 2),
> sharing the same MCP server, config, and deployment footprint.

**Version**: 1.0 — Phase 3  
**Builds on**: Phase 1 (email) + Phase 2 (temporal scheduling)  
**Target**: Same AWS EC2 Ubuntu 24.04 host, Python 3.12+, SQLite (default)  

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
                    │                 │         │   REST :8080    │    (Clubhouse)
                    │                 │         │                 │
   Agent ──────────→│ parousia-guard ──→│ Postfix :25 ──→ outbound mail
                    │   MCP  :8081    │
                    │                 │
                    │  ═══════════ Phase 3 additions ════════════
                    │                 │
                    │   ┌─────────────┤
                    │   │  Browser    │  Per-agent long-lived
                    │   │   Pool      │  Chromium instances with
                    │   │             │  persistent profiles
                    │   └─────┬───────┤
                    │         │        │
                    │   ┌─────┴───────┤
                    │   │  Crawl4AI   │  Direct Python import
                    │   │  Lens       │  HTML → SDOM distillation
                    │   │             │  Content filtering, extraction
                    │   └─────┬───────┤
                    │         │        │
                    │   ┌─────┴───────┤
                    │   │  Browser-Use│  LLM-to-Playwright bridge
                    │   │  Cortex     │  Action selection, execution,
                    │   │             │  state verification
                    │   └─────┬───────┤
                    │         │        │
                    │   ┌─────┴───────┤
                    │   │  Spatial    │  SDOM spec + serializer
                    │   │  Serializer │  (mirrors temporal DSL pattern)
                    │   └─────────────┤
                    │                 │
   Agent ──────────→│ MCP spatial     │  New tools on :8081:
                    │ tools           │  · browse_to
                    │                 │  · interact
                    │                 │  · extract_page_state
                    └─────────────────┘
```

### Component Map

| Component | Role | Protocol | Storage |
|-----------|------|----------|---------|
| Postfix (Phase 1) | MTA — accepts inbound, sends outbound | SMTP | — |
| parousia-guard REST (Phase 1) | Inbound: Postfix pipe → parse → route to agent | HTTP | — |
| parousia-guard MCP (Phase 1–3) | Outbound email + temporal tools + spatial tools | MCP (JSON-RPC) | Redis (rate limits) |
| Temporal DSL Serializer (Phase 2) | DB → token-lean text for agent context | — | SQLite |
| **Browser Pool** | Per-agent long-lived Chromium + persistent profiles | CDP | Disk (profile dirs) |
| **Crawl4AI Lens** | HTML → SDOM distillation, content filtering | Python lib | — |
| **Browser-Use Cortex** | LLM-to-Playwright action bridge | Python lib | — |
| **Spatial SDOM Serializer** | Page state → token-lean SDOM for agent context | — | — |

---

## Phase 3 Scope

### Core Deliverables

| # | Deliverable | Description |
|---|-------------|-------------|
| 1 | **SDOM Formal Spec** | Complete schema for the Spatial Document Object Model — token-lean, action-oriented page representation |
| 2 | **Browser Pool Manager** | Per-agent Chromium lifecycle: launch, health-check, shutdown, profile isolation, cookie/session persistence |
| 3 | **Crawl4AI Integration** | Direct Python import — HTML-to-SDOM distillation pipeline with configurable filtering |
| 4 | **MCP Tool: `browse_to`** | Navigate to URL, run through Crawl4AI lens, return lean SDOM |
| 5 | **MCP Tool: `interact`** | Click, type, scroll, select on SDOM-tagged elements via Playwright |
| 6 | **MCP Tool: `extract_page_state`** | Markdown snapshot of current page state for action verification |
| 7 | **Config & CLI** | Spatial config section, `parousia spatial setup/status/validate` CLI |
| 8 | **Tests** | Full test suite: SDOM serialization, browser pool, MCP tools, agent isolation |

### Out of Scope (Phase 3 backlog)

| # | Item | Notes |
|---|------|-------|
| B1 | VNC / Virtual Monitor fallback | Canvas-heavy sites (Figma, maps, dashboard graphics) where semantic decoding fails |
| B2 | Browser-Use multi-step autonomous agent loops | `browse_to` + `interact` are single-step; closed-loop "achieve this goal" is future |
| B3 | Anti-bot / Cloudflare bypass via FireCrawl | FireCrawl as managed alternative to Crawl4AI for hostile targets |
| B4 | Visual screenshot capture | `browse_screenshot` tool for when the agent explicitly needs visual confirmation |
| B5 | Concurrent multi-tab orchestration | Single tab per agent for MVP; multi-tab in follow-on |
| B6 | Network interception / request mocking | Block trackers, inject headers, mock responses |

---

## SDOM Formal Specification

The Spatial Document Object Model (SDOM) is a token-lean, action-oriented representation of a web page. It mirrors the temporal DSL philosophy: strip everything the agent doesn't need, tag everything it does, and make interaction a simple function call.

### Schema

```
SDOM {
  meta: {
    url: string,           // Canonical URL after redirects
    status: int,           // HTTP status code
    title: string,         // <title> or derived
    loaded_at: string,     // ISO 8601 timestamp
  },

  interactive: [           // Actionable elements in visual order
    {
      id: string,          // Short volatile token: "i1", "b3", "l7"
      type: enum,          // "link" | "button" | "input" | "select" | "checkbox" | "radio" | "textarea"
      label: string | null,// aria-label, associated <label>, or placeholder
      text: string | null, // Visible text content (truncated to 120 chars)
      role: string | null, // ARIA role if present
      attributes: {        // Only relevant attributes
        href: string | null,
        input_type: string | null,   // "text", "email", "password", "search", "number"
        placeholder: string | null,
        checked: bool | null,
        selected: bool | null,
        disabled: bool,
        required: bool,
      },
      rect: {              // Bounding box for visual layout hints
        x: int, y: int, width: int, height: int,
      } | null,
    }
  ],

  content: [               // Semantic content sections
    {
      heading: string | null,       // Nearest heading, H1-H4
      level: int | null,            // 1-4
      text: string,                 // Markdown-rendered content (truncated per section to 500 chars)
      images: [                     // Inline images within this section
        { alt: string | null, src: string }
      ],
      links: [                      // Navigation links within this section
        { id: string, text: string, href: string }
      ],
    }
  ],

  forms: [                 // Standalone forms (login, search, signup)
    {
      id: string,          // "f1", "f2"
      action: string | null,
      method: string | null,
      fields: [string],    // IDs of interactive elements within this form
      submit_id: string | null,  // ID of submit button
    }
  ],

  navigation: {            // Site structure
    main_nav: [
      { text: string, href: string | null, id: string | null }
    ],
    breadcrumbs: [string],
  },

  context: {               // Global page state
    cookies_set: bool,
    session_active: bool,
    authenticated_as: string | null,   // Detected from page content
    content_type: enum,                // "article" | "search_results" | "product" | "form" | "dashboard" | "error" | "login" | "generic"
  },
}
```

### Design Principles

1. **Token-lean**: A 50,000-token page compresses to 500–800 tokens. The SDOM strips CSS, layout `<div>`s, tracking scripts, ad containers, cookie banners, and repetitive metadata.

2. **Action-oriented IDs**: Every interactive element gets a short, volatile, namespaced ID (`i1`, `b3`, `l7`). Agents call `interact(id="b3", action="click")` — no pixel coordinates, no CSS selectors.

3. **Semantic sections**: Content is grouped by heading hierarchy, not by DOM depth. A `<div>` with a visible heading gets its own content section. Sidebars, footers, and nav bars are excluded from content flow (captured separately in `navigation`).

4. **Form awareness**: Standalone forms are detected and surfaced as first-class objects with their field IDs and submit buttons, so the agent can fill-and-submit in two calls.

5. **Stateful context**: The `context` block surfaces what the agent would otherwise infer — "am I logged in?", "is this a search results page?", "did the page error out?".

### Interactive Element ID Convention

```
Prefix  Type        Example    Description
──────  ────        ───────    ───────────
i       input       i1, i2     Text inputs, search boxes
b       button      b1, b3     Buttons, submit buttons
l       link        l1, l7     Anchor tags, navigation links
s       select      s1         Dropdown selects
c       checkbox    c1         Checkboxes
r       radio       r1         Radio buttons
t       textarea    t1         Textareas
f       form        f1, f2     Form containers (referenced by field IDs)
```

IDs are scoped to a single page snapshot — no persistence across navigations. They increment monotonically within a page (first input found = `i1`, second = `i2`, etc.).

### Truncation Rules

| Field | Soft Cap | Behavior |
|-------|----------|----------|
| `interactive[].text` | 120 chars | Truncated with `…` |
| `content[].text` | 500 chars | Split into multiple content sections if needed |
| `content[].images` | 10 per section | Omit extras with `+N more images` annotation |
| `interactive[]` total | 200 elements | Omit extras with `+N more elements` annotation; prioritize visible + in-viewport |

---

## MCP Tool Specifications

### `browse_to`

```
Navigate to a URL, render the page, and return a token-lean SDOM representation.
Handles redirects, waits for page load, and runs through the Crawl4AI lens.

Input:
  url: string (required)
    The URL to navigate to.
  timeout_ms: int (default: 30000)
    Maximum wait time for page load.
  extract_mode: enum (default: "standard")
    "standard" — full SDOM with interactive elements + content
    "content_only" — content sections only, no interactive map
    "interactive_only" — interactive elements only, no content flow

Output:
  {
    navigated: true,
    sdom: { ... },          // SDOM object per spec above
    token_estimate: int,    // Estimated token count of the SDOM
    original_size: int,     // Raw HTML byte count for compression ratio
    compression_ratio: float, // SDOM tokens / raw HTML bytes
    load_time_ms: int,      // Page load + SDOM extraction wall time
    redirect_chain: [string] // URLs followed if redirects occurred
  }
```

### `interact`

```
Perform an action on an SDOM-tagged interactive element.
Maps the spatial ID directly to a Playwright action.

Input:
  id: string (required)
    Element ID from the current page's SDOM (e.g., "i1", "b3", "l7").
  action: enum (required)
    "click" — click the element
    "type" — type text into an input/textarea
    "scroll_into_view" — scroll element into viewport
    "select" — select an option from a <select>
    "check" / "uncheck" — toggle checkbox
    "hover" — hover over element
    "press" — press a keyboard key (Enter, Tab, Escape)
  text: string (optional)
    Text to type (required for action="type").
    Option value for action="select".
    Key name for action="press".
  timeout_ms: int (default: 10000)
    Maximum wait time for the action and any resulting navigation.

Output:
  {
    action_performed: true,
    element_id: string,      // The ID that was acted on
    action: string,
    page_changed: bool,      // Did the action trigger a navigation or DOM mutation?
    new_url: string | null,  // If navigation occurred
    sdom: { ... } | null,    // Updated SDOM if page_changed=true
    screenshot_needed: false // Always false in MVP (no VNC)
  }
```

### `extract_page_state`

```
Return a markdown snapshot of the current page state.
Useful for verifying that an action completed successfully
(e.g., confirming form submission, checking for error messages).

Input:
  mode: enum (default: "full")
    "full" — complete markdown of visible page content
    "changes" — only content that differs from last browse_to/interact
    "context_only" — only the SDOM context block (auth status, page type, cookies)

Output:
  {
    extracted: true,
    mode: string,
    url: string,
    markdown: string,        // Markdown-rendered page content
    token_estimate: int,
    context: { ... },        // SDOM context block
    extracted_at: string     // ISO 8601 timestamp
  }
```

---

## Browser Pool Architecture

### Lifecycle

```
                   ┌──────────────────┐
                   │  Agent requests   │
                   │  first spatial    │
                   │  tool call        │
                   └────────┬─────────┘
                            │
                   ┌────────▼─────────┐
                   │  Pool Manager     │
                   │  checks for       │
                   │  agent profile    │
                   └────────┬─────────┘
                            │
              ┌─────────────┼─────────────┐
              │ exists?      │              │ doesn't exist?
              ▼              │              ▼
    ┌─────────────────┐     │    ┌─────────────────┐
    │  Launch Chromium │     │    │  Create profile  │
    │  with existing   │     │    │  dir + launch    │
    │  profile dir     │     │    │  fresh Chromium  │
    └────────┬────────┘     │    └────────┬────────┘
             │              │             │
             └──────────────┼─────────────┘
                            │
                   ┌────────▼─────────┐
                   │  Health check:    │
                   │  CDP websocket    │
                   │  alive?           │
                   └────────┬─────────┘
                            │
              ┌─────────────┼─────────────┐
              │ healthy?     │              │ unhealthy?
              ▼              │              ▼
    ┌─────────────────┐     │    ┌─────────────────┐
    │  Return browser  │     │    │  Kill + relaunch │
    │  handle          │     │    │  (max 3 retries) │
    └─────────────────┘     │    └─────────────────┘
                            │
                   ┌────────▼─────────┐
                   │  Execute tool    │
                   │  call            │
                   └────────┬─────────┘
                            │
                   ┌────────▼─────────┐
                   │  Idle timeout:    │
                   │  5 min inactivity │
                   │  → graceful close │
                   └──────────────────┘
```

### Profile Storage

```
/var/lib/parousia/browsers/
├── hermes/
│   ├── Default/          # Chromium user data dir
│   │   ├── Cookies       # Persistent cookies
│   │   ├── Local Storage/
│   │   ├── Session Storage/
│   │   └── Preferences
│   └── profile.lock      # Prevents concurrent use
├── claude/
│   └── ...
└── pool.lock              # Global pool mutex
```

### Configuration

```yaml
# /etc/parousia/config.yaml (new section)
spatial:
  enabled: true
  chromium_path: "/usr/bin/chromium-browser"   # or auto-detect
  profile_dir: "/var/lib/parousia/browsers"
  idle_timeout_seconds: 300                     # 5 min
  max_instances: 10                             # Total across all agents
  launch_args:
    - "--no-sandbox"
    - "--disable-dev-shm-usage"
    - "--disable-gpu"
    - "--headless=new"
  crawl4ai:
    word_count_threshold: 200                   # Min words for content extraction
    exclude_tags: ["nav", "footer", "aside", "script", "style"]
    exclude_selectors: [".cookie-banner", ".ad-container", "#sidebar"]
    timeout_ms: 15000
```

### Agent Isolation

- Each agent gets its own Chromium profile directory (persistent cookies, localStorage, sessions)
- A `profile.lock` file prevents concurrent use of the same profile
- Agent A cannot access Agent B's cookies, sessions, or browsing history
- Tools accept an `agent_id` parameter; the pool manager routes to the correct profile
- Idle browsers are gracefully closed after 5 minutes of inactivity (profile state is preserved on disk)

---

## Integration Points

### MCP Server (port 8081)

The spatial tools join the existing tool list in `mcp_server.py`:

```python
# In _build_server(), after temporal tools:
from parousia.spatial.tools import ALL_SPATIAL_SCHEMAS, SpatialToolHandlers

spatial_handlers = SpatialToolHandlers(config, browser_pool)

# list_tools: add spatial schemas
for schema in ALL_SPATIAL_SCHEMAS:
    tools.append(Tool(**schema))

# call_tool: dispatch spatial tools
if name in SPATIAL_TOOL_NAMES:
    result = spatial_handlers.dispatch(name, arguments, agent_id)
    return [TextContent(type="text", text=result)]
```

### Config

The `ParousiaConfig` model gains a `SpatialConfig` section (mirrors the YAML above). Backward compatible — if the `spatial` key is absent, spatial tools return an appropriate error.

### CLI

```bash
parousia spatial setup      # Install chromium, crawl4ai, browser-use dependencies
parousia spatial status     # Show browser pool health, active instances, profile sizes
parousia spatial validate   # Check chromium path, crawl4ai import, launch a test page
parousia spatial cleanup    # Remove idle profiles, purge old session data
```

---

## Dependency Map

| Package | Version | Purpose | Install |
|---------|---------|---------|---------|
| `playwright` | ≥1.48 | Browser automation engine | `pip install playwright && playwright install chromium` |
| `crawl4ai` | ≥0.4 | HTML distillation + semantic extraction | `pip install crawl4ai` |
| `browser-use` | ≥0.2 | LLM-to-browser action bridge | `pip install browser-use` |
| Chromium | Any Playwright-bundled | Browser runtime | `playwright install chromium` |

All are pure Python packages with no system-level compilation required (playwright downloads its own Chromium binary).

---

## Test Plan

### Unit Tests

| Test Class | Coverage |
|-----------|----------|
| `TestSDOMSerializer` | SDOM generation from HTML fixtures, truncation rules, ID convention, all element types |
| `TestBrowserPool` | Profile creation, launch/health-check/close, agent isolation, idle timeout, max instances, lock files |
| `TestSpatialTools` | `browse_to` schema validation, `interact` schema validation, `extract_page_state` schema validation |
| `TestSpatialToolHandlers` | Handler dispatch, error cases, agent isolation, browser unavailable fallback |

### Integration Tests

| Test | Coverage |
|------|----------|
| `test_browse_to_local_html` | Serve a static HTML fixture via local HTTP, browse_to, verify SDOM |
| `test_interact_click` | Click a link on a local page, verify navigation + updated SDOM |
| `test_interact_type` | Type into an input, verify value set |
| `test_extract_page_state` | Extract markdown from a known page, verify content fidelity |
| `test_cookie_persistence` | Set a cookie via browse_to, restart browser, verify cookie survives |
| `test_agent_isolation` | Two agents browse different pages, verify separate profiles and no cross-contamination |
| `test_idle_timeout` | Verify browser closes after idle period, restarts on next request |

### Target

- Unit: 40+ tests
- Integration: 10+ tests
- Existing Phase 1 + 2 tests must continue to pass (no regressions)

---

## Implementation Plan

### Task 1: SDOM Spec + Serializer
- Define SDOM Python data models (pydantic)
- Implement SDOM serializer: HTML → SDOM via Crawl4AI
- Truncation rules, ID convention, content sectioning
- Tests: 15+ unit tests

### Task 2: Browser Pool Manager
- Profile directory management
- Chromium launch/health-check/close lifecycle
- Lock file concurrency control
- Idle timeout with configurable TTL
- Tests: 12+ unit tests

### Task 3: Spatial MCP Tools
- `browse_to` handler: navigate → SDOM via Crawl4AI lens
- `interact` handler: map SDOM ID → Playwright action
- `extract_page_state` handler: markdown snapshot
- Wire into existing MCP server on :8081
- Tests: 8+ unit + integration tests

### Task 4: Config + CLI
- `SpatialConfig` pydantic model
- Config YAML section with defaults
- CLI: `parousia spatial setup/status/validate/cleanup`
- Tests: 5+ tests

### Task 5: Integration + Polish
- End-to-end tests with local HTTP fixtures
- Cookie/session persistence verification
- Agent isolation verification
- Documentation: PRD → implementation plan
- Full test suite: verify no Phase 1/2 regressions
