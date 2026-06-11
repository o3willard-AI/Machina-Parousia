# Spatial — Capability Guide

Parousia gives every agent a persistent web browser. Agents navigate, interact, and extract page state — all through MCP tools. No screenshots, no pixel-level mouse emulation. Instead, pages are distilled into **SDOM** (Structured DOM) — a compressed, element-ID-addressable representation optimized for LLM context windows.

---

## Architecture

```
Agent → MCP browse_to/interact/extract_page_state (stdio transport)
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
