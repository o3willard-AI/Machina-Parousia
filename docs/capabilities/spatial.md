# Spatial — Capability Guide

Parousia gives every agent a persistent web browser. Agents navigate, interact, and extract page state — all through MCP tools. No screenshots, no pixel-level mouse emulation. Instead, pages are distilled into **SDOM** (Structured DOM) — a compressed, element-ID-addressable representation optimized for LLM context windows.

---

## Architecture

```
Agent → MCP browse_to/interact/extract_page_state/check_egress_ip (stdio transport)
  → SpatialToolHandlers.dispatch()
  → BrowserPoolManager.get_browser(agent_id)
  → Playwright Chromium (per-agent persistent profile)
  → SpatialSerializer.to_sdom() (HTML → SDOM via BeautifulSoup4)
  → SDOM returned to agent
```

Each agent gets its own Chromium instance with a persistent profile (cookies, localStorage, sessions survive across calls). The browser pool enforces a configurable max instance limit and auto-cleans idle browsers.

---

## Tools

### `browse_to`

Navigate to a URL and return the page as SDOM.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `url` | ✅ | URL to navigate to |
| `timeout_ms` | ❌ | Navigation timeout in ms (default: 30000) |
| `extract_mode` | ❌ | `standard` (default), `content_only`, or `interactive_only` |
| `proxy` | ❌ | Named transport for **this** request — a name from the operator's `spatial.proxies` menu. `""`/omitted = **direct egress, always** (see [Egress transports](#egress-transports-tor-and-stealth)) |
| `user_agent` | ❌ | Per-navigation User-Agent override (consistent Sec-CH-UA client hints applied). Default: the configured browser identity |

**Extraction modes:**

| Mode | Returns | Use case |
|------|---------|----------|
| `standard` | All interactive elements + content sections | General browsing |
| `content_only` | Content sections only, no interactive elements | Reading articles, extracting text |
| `interactive_only` | Only interactive elements (buttons, links, inputs, forms) | Form interaction, navigation scanning |

**Request:**
```json
{
  "tool": "browse_to",
  "arguments": {
    "url": "https://news.ycombinator.com",
    "extract_mode": "standard"
  }
}
```

**Response:**
```json
{
  "url": "https://news.ycombinator.com",
  "extracted": true,
  "sdom": {
    "meta": {
      "url": "https://news.ycombinator.com",
      "title": "Hacker News",
      "status": 200,
      "extracted_at": "2026-06-11T14:30:00Z",
      "page_type": "generic",
      "interactive_count": 65,
      "content_sections": 1
    },
    "interactive_elements": [
      {"id": "a1", "tag": "a", "text": "New", "href": "/newest", "type": "link", "rect": {"x": 10, "y": 0, "w": 40, "h": 20}},
      {"id": "a2", "tag": "a", "text": "Show HN: Parousia — sovereign agent presence", "href": "/item?id=12345", "type": "link", "rect": {"x": 10, "y": 60, "w": 700, "h": 20}},
      {"id": "a3", "tag": "a", "text": "237 comments", "href": "/item?id=12345", "type": "link", "rect": {"x": 10, "y": 80, "w": 100, "h": 20}}
    ],
    "content_sections": [
      {
        "heading": "Hacker News",
        "heading_level": 1,
        "text": "Hacker News new | past | comments | ask | show | jobs | submit",
        "text_truncated": false
      }
    ],
    "context": {
      "has_form": false,
      "has_navigation": true,
      "forms": []
    }
  }
}
```

**SDOM element types:**

| `type` | Tags | Notes |
|--------|------|-------|
| `link` | `<a href>` | Navigation links |
| `button` | `<button>`, `<input type=submit>` | Clickable buttons |
| `input` | `<input>` (text, email, password, etc.) | Typeable fields |
| `textarea` | `<textarea>` | Multi-line text input |
| `select` | `<select>` | Dropdown menus |
| `checkbox` | `[role=checkbox]`, `<input type=checkbox>` | Toggleable |
| `radio` | `[role=radio]`, `<input type=radio>` | Radio groups |

---

### `interact`

Perform actions on SDOM elements using their IDs (from `browse_to` or `extract_page_state` responses).

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `id` | ✅ | Element ID from SDOM (e.g., `a2`, `btn1`, `input3`) |
| `action` | ✅ | One of: `click`, `type`, `scroll_into_view`, `select`, `check`, `uncheck`, `hover`, `press` |
| `text` | ❌ | Text to type (required for `type` action) |
| `value` | ❌ | Value to select (required for `select` action) |
| `key` | ❌ | Key to press (required for `press` action) |
| `timeout_ms` | ❌ | Timeout in ms (default: 30000) |
| `proxy` | ❌ | Named transport for **this** request (same semantics as `browse_to`'s `proxy`, with one nuance: `""`/omitted = the agent's **default context** — direct egress unless the operator mapped a proxy for that agent; see [Egress transports](#egress-transports-tor-and-stealth)) |

**Action reference:**

| Action | What it does | Requires |
|--------|-------------|----------|
| `click` | Clicks the element | — |
| `type` | Types text into the element | `text` param |
| `scroll_into_view` | Scrolls element into viewport | — |
| `select` | Selects an option in a dropdown | `value` param |
| `check` | Checks a checkbox | — |
| `uncheck` | Unchecks a checkbox | — |
| `hover` | Hovers over the element | — |
| `press` | Presses a key on the element | `key` param |

**Example — click a link:**
```json
{
  "tool": "interact",
  "arguments": {
    "id": "a2",
    "action": "click"
  }
}
```

**Example — fill a search form:**
```json
{
  "tool": "interact",
  "arguments": {
    "id": "input0",
    "action": "type",
    "text": "self-hosted agent infrastructure"
  }
}
```

**Response:**
```json
{
  "id": "input0",
  "action": "type",
  "success": true
}
```

After interacting, call `extract_page_state` to get the updated SDOM.

---

### `extract_page_state`

Extract the current page as SDOM without re-navigating. Use this after interactions to see the updated page state.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `mode` | ❌ | `full` (default), `changes`, or `context_only` |

**Modes:**

| Mode | Returns | Use case |
|------|---------|----------|
| `full` | Complete SDOM of current page | After navigation or major state change |
| `changes` | Only elements that changed since last extraction | Efficient polling after a single interaction |
| `context_only` | Content sections only, no interactive elements | Reading article text after navigation |

**Request:**
```json
{
  "tool": "extract_page_state",
  "arguments": {
    "mode": "full"
  }
}
```

---

### `fill_credentials`

Fill a stored credential into a login form on the **current page's origin**. Credentials are held in an encrypted-at-rest vault (Fernet, populated by the human via `parousia-guard vault add`). The fill is **origin-bound** (only the exact site the credential was saved for) and the agent **never sees the secret** — the tool returns a fill summary only.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `origin` | ✅ | Origin the credential is stored for (e.g. `https://site.example`) — must match the current page origin |
| `handle` | ✅ | The credential handle (as stored by the human) |
| `submit` | ❌ | Also click a submit button after filling (default: `false`) |
| `timeout_ms` | ❌ | Timeout in ms for field lookups (default: 30000) |

**Response:**
```json
{ "filled": true, "fields": ["username", "password"] }
```

If no credential is stored for the current origin, the tool answers `"no credential stored"` — it never fabricates or falls back to anything less structured.

---

### `dump_form`

Introspect the current page's form controls so an agent knows exactly what to fill before it does. Returns per-field `name` / `type` / `label` / current value / options (select) / radio-group options — with **password values always nulled** (both in the extraction JS and re-masked on the Python side). Includes **captcha detection** (`{present, vendor, type, sitekey}` for reCAPTCHA v2/v3, hCaptcha, Turnstile).

**Response:**
```json
{
  "form": {
    "fields": [
      {"name": "email", "type": "email", "label": "Email", "current_value": ""},
      {"name": "password", "type": "password", "label": "Password", "is_secret": true, "current_value": null}
    ],
    "submit": {"type": "button", "label": "Sign in"}
  },
  "captcha": {"present": true, "vendor": "cloudflare", "type": "turnstile", "sitekey": "0x4AAA..."}
}
```

---

### `check_egress_ip`

Read-only verification of the agent-called transport — the 6th spatial tool. Navigates to an IP-echo endpoint (`https://api.ipify.org`) and returns the **actual public IP** the request presented, so an agent can confirm a chosen transport (e.g. `proxy="tor"`) is genuinely active before attempting a signup.

**Parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `proxy` | ❌ | Named transport to verify — same semantics as `browse_to`'s `proxy` (`""`/omitted = direct egress) |
| `timeout_ms` | ❌ | Echo navigation timeout in ms (default: 30000) |

**Request:**
```json
{
  "tool": "check_egress_ip",
  "arguments": { "proxy": "residential-1" }
}
```

**Response:**
```json
{ "ip": "203.0.113.7", "proxy": "residential-1" }
```

`proxy` is the transport **by name**, never the resolved URL (registry URLs may embed credentials). Read-only: nothing is submitted, nothing is stored — the echo fetch is the only traffic.

---

## Egress transports, Tor, and stealth

Browser transport selection is **opt-in, agent-called, per request** — never a routing rule.

- `proxy` omitted or `""` → **direct egress** (`browse_to`) / the agent's default context (`interact`) — the default, always
- a name from the operator's `spatial.proxies` menu → that named proxy, in its own persistent context per (agent, proxy)
- an unknown name → loud error listing the available transports
- `tor` → the **reserved, managed Tor transport**. Requires `spatial.tor_enabled: true` — while disabled (the default) or unavailable, the call **fails loud**, never a silent direct-egress fallback. The SOCKS listener binds loopback only (`spatial.tor_socks_port`, default 9050), and Tor is never auto-started: an explicit `proxy="tor"` with `tor_enabled=true` is the only path that launches it.

`browse_to` additionally accepts a per-navigation **`user_agent`** override; the configured browser identity (`spatial.user_agent`) is the default.

**Operator config (`config.yaml` `spatial:` block):**

```yaml
spatial:
  proxies:                       # MENU of named transports (name → proxy URL),
    residential-1: "http://user:pass@proxy.example:8080"   # never a routing rule
    datacenter: "socks5://proxy.example:1080"
  proxy_servers:                 # operator-side per-agent mapping (agent_id → URL)
    hermes: "http://user:pass@proxy.example:8080"
  proxy_default: ""              # default proxy for mapped agents ("" = direct)
  tor_enabled: false              # opt-in; `proxy="tor"` fails loud while false
  tor_socks_port: 9050
  stealth_enabled: false          # full fingerprint hardening — OFF by default
```

Results and egress reports always carry the transport **name**, never the resolved URL.

**Stealth (MP-9):** `spatial.stealth_enabled` (default **off**) switches the pool from the plain `navigator.webdriver` suppression script to a full stealth init script — `navigator.webdriver` → `undefined`, `window.chrome` realism, plugins/languages/platform realism, WebGL vendor/renderer, `history.length`. Ordinary browsing is exactly as today while off; when on, the stealth script **replaces** the plain suppression script (they are never stacked).

---

## Failure forensics

When `browse_to` / `interact` fail (HTTP ≥ 400 or an exception), the result carries **forensics** so an agent can tell "the tool is broken" from "the site is refusing us":

| Field | Description |
|-------|-------------|
| `refused` | `true` when the HTTP response was ≥ 400 |
| `http_status` | The HTTP status code |
| `block_vendor` | `cloudfront` / `cloudflare` / `akamai` / `none` — classified from headers first (e.g. CloudFront's `x-cache: Error from cloudfront`, Cloudflare's `cf-ray`) then body phrases |
| `screenshot_path` | Path to a `.png` screenshot of the failure |
| `dom_snapshot` | First ~2 KB of body text |

Every forensics leg is best-effort — a forensics failure never masks the original tool error.

---

## Browser pool

The browser pool manages per-agent Chromium instances:

| Property | Default | Description |
|----------|---------|-------------|
| `max_instances` | 10 | Maximum concurrent browser instances |
| `idle_timeout_seconds` | 300 | Close idle browsers after 5 minutes |
| `profile_dir` | `/var/lib/parousia/browsers` | Per-agent profile directories |
| `launch_args` | `--no-sandbox --disable-dev-shm-usage --disable-gpu --headless=new` | Chromium launch flags |

Each agent's browser profile persists across sessions — cookies, localStorage, and sessions survive restarts. Profiles are locked during use to prevent concurrent access. Stale locks (from crashed processes) are detected and cleaned automatically.

**Pool lifecycle:**
1. Agent calls `browse_to` → pool checks for existing browser
2. If browser exists and is alive → reuse it
3. If browser doesn't exist → launch new Chromium, create profile directory
4. After idle timeout → browser closed, resources freed
5. On Parousia shutdown → all browsers closed gracefully

**Config:**
```yaml
spatial:
  enabled: true
  chromium_path: "/usr/bin/chromium-browser"
  profile_dir: "/var/lib/parousia/browsers"
  idle_timeout_seconds: 300
  max_instances: 6
  launch_args:
    - "--no-sandbox"
    - "--disable-dev-shm-usage"
    - "--disable-gpu"
    - "--headless=new"
  # Identity & egress (see "Egress transports, Tor, and stealth" above):
  # user_agent: "…"        # per-agent browser identity (default: current desktop Chrome UA)
  # proxies: {}            # named transport menu — agent calls one by name via `proxy`
  # tor_enabled: false     # reserved `tor` transport, opt-in
  # stealth_enabled: false # full fingerprint hardening, OFF by default
```

---

## SDOM page type classification

The serializer classifies pages by type to help agents understand context:

| Type | Detection | Example |
|------|-----------|---------|
| `login` | Password field + submit button | Login pages |
| `search_results` | `<form role=search>` or `<input type=search>` | Google, internal search |
| `form` | Any `<form>` with inputs | Signup, contact forms |
| `error` | HTTP status ≥ 400 or error keywords | 404, 500 pages |
| `dashboard` | Multiple widgets, charts, data tables | Admin panels |
| `article` | Long-form text with headings | Blog posts, news |
| `product` | Price element + add-to-cart | Ecommerce |
| `generic` | None of the above | Default |

---

## Typical agent workflow

```
1. browse_to("https://news.ycombinator.com")
   → Returns SDOM with 65 interactive elements, page_type: generic

2. interact(id="a2", action="click")
   → Clicks the top story link

3. extract_page_state(mode="full")
   → Returns SDOM of the story page, page_type: article

4. interact(id="textarea0", action="type", text="Great work! This is exactly what agents need.")
   → Types a comment

5. interact(id="btn3", action="click")
   → Submits the comment
```
