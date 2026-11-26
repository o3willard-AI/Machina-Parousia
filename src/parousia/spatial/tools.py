"""MCP tool handler implementations for spatial tools.

browse_to, interact, extract_page_state — registered on the existing MCP server (port 8081).

All handlers are async: they drive ``playwright.async_api`` from inside the
MCP server's asyncio event loop (the synchronous Playwright API cannot be used
there). ``dispatch`` and every ``_handle_*`` method are coroutines.
"""

import json
import logging
import os
import re
import socket
import tempfile
from typing import Any, Optional

from parousia.config import ParousiaConfig
from parousia.spatial.browser_pool import BrowserPoolManager
from parousia.spatial.serializer import SpatialSerializer
# MP-10b: the managed Tor transport's error base (fail-loud, never a
# silent direct-egress fallback — see _resolve_tor).
from parousia.spatial.tor import TorError
from parousia.vault.credential_store import CredentialStore, normalize_origin

logger = logging.getLogger("parousia.spatial.tools")


# ── MP-3 helpers ─────────────────────────────────────────────────


def _client_hints_for(user_agent: str) -> dict:
    """Derive consistent Sec-CH-UA client-hint headers for a UA string.

    The brands/version mirror the UA's Chrome version so the high-entropy
    hints never contradict the UA header (a WAF tell in itself).
    """
    match = re.search(r"Chrome/(\d+)", user_agent or "")
    version = match.group(1) if match else "142"
    if "Windows" in (user_agent or ""):
        platform = "Windows"
    elif "Macintosh" in (user_agent or "") or "Mac OS" in (user_agent or ""):
        platform = "macOS"
    else:
        platform = "Linux"
    return {
        "Sec-CH-UA": (
            f'"Chromium";v="{version}", '
            f'"Google Chrome";v="{version}", '
            '"Not?A_Brand";v="99"'
        ),
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": f'"{platform}"',
    }


def _effective_egress_ip() -> str:
    """The host's source IP for internet egress — no packet leaves.

    UDP ``connect`` only selects the default-route source address; the
    destination is TEST-NET-1 (never routed). Offline-safe, no external
    service dependency. Returns "unknown" when there is no route.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(2.0)
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "unknown"


# ── MP-4: form introspection + captcha detection (issue #18) ──────


# Pure-DOM extraction — NO mutation, no clicks. Runs in the page and returns
# every form's controls with labels/flags/limits; secret values (password
# inputs) are nulled IN THE PAGE so they never even cross the evaluate
# return. The Python assembly (_assemble_form_report) re-masks (belt and
# braces + unit-testable without a browser).
DUMP_FORM_JS = """
(() => {
  const visible = (el) => {
    if (!el) return false;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (el.offsetParent === null && style.position !== 'fixed') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const labelText = (el) => {
    let text = '';
    const id = el.id;
    if (id) {
      const lab = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
      if (lab) text = (lab.textContent || '').trim();
    }
    if (!text) {
      const wrap = el.closest('label');
      if (wrap && wrap !== el) text = (wrap.textContent || '').trim();
    }
    if (!text) text = (el.getAttribute('aria-label') || '').trim();
    if (!text) {
      const labelledby = el.getAttribute('aria-labelledby');
      if (labelledby) {
        for (const ref of labelledby.split(/\\s+/)) {
          const t = document.getElementById(ref);
          if (t && (t.textContent || '').trim()) { text = (t.textContent || '').trim(); break; }
        }
      }
    }
    if (!text) {
      const name = el.getAttribute('name');
      if (name) {
        const lab = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
        if (lab) text = (lab.textContent || '').trim();
      }
    }
    return text;
  };
  const limitsOf = (el) => {
    const lim = {};
    for (const a of ['maxlength', 'minlength', 'min', 'max', 'pattern']) {
      const v = el.getAttribute(a);
      if (v !== null && v !== '') lim[a] = v;
    }
    return lim;
  };
  const forms = [];
  const allForms = Array.from(document.forms);
  for (let fi = 0; fi < allForms.length; fi++) {
    const form = allForms[fi];
    const controls = [];
    for (const el of Array.from(form.elements)) {
      const tag = (el.tagName || '').toLowerCase();
      const type = tag === 'input'
        ? (el.getAttribute('type') || 'text').toLowerCase()
        : tag;
      const isSecret = type === 'password';
      const rawValue = isSecret ? null : (el.value || '').trim();
      const ctl = {
        tag: tag,
        type: type,
        name: el.getAttribute('name') || '',
        id: el.id || '',
        label: labelText(el),
        placeholder: (el.getAttribute('placeholder') || ''),
        required: el.required === true || el.hasAttribute('required'),
        disabled: el.disabled === true || el.hasAttribute('disabled'),
        readonly: el.readOnly === true || el.hasAttribute('readonly'),
        visible: visible(el),
        is_secret: isSecret,
        value: isSecret ? null : rawValue,
        current_value: isSecret ? null : rawValue,
        limits: limitsOf(el),
      };
      if (tag === 'select') {
        ctl.multiple = el.multiple === true;
        ctl.select_options = Array.from(el.options).map((o) => ({
          value: o.value,
          label: (o.textContent || '').trim(),
          selected: o.selected,
        }));
      }
      if (type === 'radio' || type === 'checkbox') {
        ctl.checked = el.checked === true;
        const gname = el.getAttribute('name') || '';
        const group = [];
        if (gname) {
          const sibs = document.querySelectorAll(
            'input[type="' + type + '"][name="' +
            (window.CSS && CSS.escape ? CSS.escape(gname) : gname) + '"]'
          );
          for (const cand of Array.from(sibs)) {
            group.push({
              value: cand.value,
              label: labelText(cand),
              checked: cand.checked,
            });
          }
        }
        ctl.group_options = group;
      }
      controls.push(ctl);
    }
    forms.push({
      index: fi,
      id: form.id || '',
      name: form.getAttribute('name') || '',
      action: form.action || '',
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      controls: controls,
    });
  }
  return { form_count: forms.length, forms: forms };
})()
"""

# Detect (never solve) captcha presence. Vendor-specific markers first,
# then a generic captcha-named element fallback (vendor 'other').
CAPTCHA_JS = """
(() => {
  const out = { present: false, vendor: 'none', type: null, sitekey: null };
  const q = (sel) => document.querySelector(sel);
  const iframeHas = (needle) => {
    for (const f of document.querySelectorAll('iframe')) {
      if ((f.src || '').indexOf(needle) !== -1) return true;
    }
    return false;
  };
  // reCAPTCHA v2 (widget element; data-size=invisible → the invisible badge)
  const rec = q('.g-recaptcha');
  if (rec) {
    out.present = true;
    out.vendor = 'recaptcha_v2';
    const size = (rec.getAttribute('data-size') || '').toLowerCase();
    out.type = size === 'invisible' ? 'invisible' : 'visible';
    out.sitekey = rec.getAttribute('data-sitekey') || null;
  }
  // reCAPTCHA v3: script with a render= param, no widget element
  if (!out.present) {
    let v3 = false;
    for (const s of document.querySelectorAll('script[src]')) {
      const src = s.src || '';
      if (src.indexOf('recaptcha/api.js') !== -1 && src.indexOf('render=') !== -1) v3 = true;
    }
    if (v3) {
      out.present = true;
      out.vendor = 'recaptcha_v3';
      out.type = 'invisible';
      const m = (document.querySelector('script[src*="render="]') || {src: ''}).src.match(/render=([^&]+)/);
      out.sitekey = m ? decodeURIComponent(m[1]) : null;
    }
  }
  if (!out.present && iframeHas('google.com/recaptcha')) {
    out.present = true;
    out.vendor = 'recaptcha_v2';
    out.type = 'visible';
  }
  // hCaptcha
  if (!out.present) {
    const hc = q('.h-captcha, iframe[src*="hcaptcha.com"]');
    if (hc || typeof window.hcaptcha !== 'undefined') {
      out.present = true;
      out.vendor = 'hcaptcha';
      const invis = q('.h-captcha[data-size="invisible"], [data-hcaptcha-size="invisible"]');
      out.type = invis ? 'invisible' : 'visible';
      const el = q('.h-captcha');
      out.sitekey = el ? (el.getAttribute('data-sitekey') || null) : null;
    }
  }
  // Cloudflare Turnstile
  if (!out.present) {
    const ts = q('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]');
    if (ts || typeof window.turnstile !== 'undefined') {
      out.present = true;
      out.vendor = 'turnstile';
      out.type = 'visible';
      const el = q('.cf-turnstile');
      out.sitekey = el ? (el.getAttribute('data-sitekey') || null) : null;
    }
  }
  // Generic fallback: anything captcha-named (vendor unknown)
  if (!out.present) {
    const generic = q('[class*="captcha" i], [id*="captcha" i]');
    if (generic) {
      out.present = true;
      out.vendor = 'other';
      out.type = 'visible';
    }
  }
  return out;
})()
"""

# Best-effort observed-cap probe for multi-selects (probe_limits flag,
# default OFF): select-and-fire-until-no-op — set each option selected,
# dispatch click/input/change so the page's OWN JS enforcement runs (that
# is the only place a silent cap lives — Empower's 10-item cap was declared
# nowhere in the HTML; a plain DOM attribute read cannot see it), and count
# how many stay selected. When the count stops growing the page has
# refused selection N+1 → that IS the observed cap. Mutates the selection
# temporarily and restores the original selection afterwards.
MULTISELECT_PROBE_JS = """
(() => {
  const results = [];
  const allForms = Array.from(document.forms);
  for (let fi = 0; fi < allForms.length; fi++) {
    const form = allForms[fi];
    for (const el of Array.from(form.elements)) {
      if ((el.tagName || '').toLowerCase() !== 'select' || !el.multiple) continue;
      const name = el.getAttribute('name') || el.id || '(unnamed)';
      const original = Array.from(el.options).filter((o) => o.selected).map((o) => o.value);
      let observed = null;
      let capped_by = null;
      try {
        for (const o of Array.from(el.options)) o.selected = false;
        const n = el.options.length;
        let last = 0;
        for (let i = 0; i < n; i++) {
          el.options[i].selected = true;
          try { el.options[i].click(); } catch (_) {}
          try {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          } catch (_) {}
          const cnt = el.selectedOptions.length;
          if (cnt <= last) {
            observed = last;
            capped_by = 'runtime-enforced (page JS refused selection #'
              + (last + 1) + ')';
            break;
          }
          last = cnt;
        }
        if (observed === null) {
          observed = n;
          capped_by = 'none observed: every option stays selectable';
        }
      } finally {
        for (const o of Array.from(el.options)) o.selected = original.indexOf(o.value) !== -1;
        try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (_) {}
      }
      results.push({
        form: form.getAttribute('name') || form.id || ('form[' + fi + ']'),
        name: name,
        option_count: el.options.length,
        observed_cap: observed,
        capped_by: capped_by,
      });
    }
  }
  return { probed: results };
})()
"""


def _captcha_from_signals(signals: Optional[dict]) -> dict:
    """Normalize the in-page captcha signals into the reported field.

    Pure parser (unit-testable without a browser): ``present`` only for a
    known vendor; ``type``/``sitekey`` only when present. Defensive against
    a non-dict evaluate return (detect nothing rather than crash the tool).
    """
    s = signals if isinstance(signals, dict) else {}
    vendor = (s.get("vendor") or "none")
    if not isinstance(vendor, str):
        return {"present": False, "vendor": "none", "type": None, "sitekey": None}
    vendor = vendor.strip().lower() or "none"
    present = bool(s.get("present")) and vendor != "none"
    if not present:
        return {"present": False, "vendor": "none", "type": None, "sitekey": None}
    return {
        "present": True,
        "vendor": vendor,
        "type": s.get("type") or None,
        "sitekey": s.get("sitekey") or None,
    }


def _assemble_form_report(raw: Optional[dict]) -> dict:
    """Assemble the dump_form report from the in-page extraction.

    Belt-and-braces secret masking: the extraction JS already nulls
    password values in the page, and this re-masks on the Python side so
    the invariant holds even if the extraction output ever changes.
    """
    raw = raw or {}
    forms = []
    for form in raw.get("forms", []) or []:
        controls = []
        for c in form.get("controls", []) or []:
            c = dict(c)
            if c.get("is_secret") or (c.get("type") or "").lower() == "password":
                c["is_secret"] = True
                c["value"] = None
                c["current_value"] = None
            controls.append(c)
        entry = dict(form)
        entry["controls"] = controls
        forms.append(entry)
    return {"form_count": raw.get("form_count", len(forms)), "forms": forms}


# ── MP-6 (part B): failure forensics — block-vendor detection ─────


# Block-vendor markers, checked most-specific-first. Header markers are
# stronger than body text (a legit page may mention a vendor by name), so
# headers classify first; body phrases are the CloudFront "The request
# could not be satisfied" style — distinctive enough to be trusted.
_BLOCK_HEADER_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    # CloudFront: the canonical WAF refusal header.
    ("cloudfront", ("x-cache",)),
    ("cloudflare", ("cf-ray", "cf-mitigated", "cf-cache-status")),
    ("akamai", ("x-akamai",)),
]
_BLOCK_BODY_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    (
        "cloudfront",
        (
            "the request could not be satisfied",
            "error from cloudfront",
        ),
    ),
    (
        "cloudflare",
        (
            "just a moment",
            "attention required! | cloudflare",
            "checking your browser before accessing",
        ),
    ),
    (
        "akamai",
        (
            "access denied",
            "akamai",
        ),
    ),
]


def _classify_block_headers(headers: Optional[dict]) -> str:
    """Classify a block vendor from response headers ("" → unknown)."""
    if not headers:
        return ""
    lowered = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    for vendor, keys in _BLOCK_HEADER_MARKERS:
        for key in keys:
            # Akamai ships many x-akamai-* headers; prefix-match the family.
            hit = key in lowered or any(
                h.startswith(key + "-") for h in lowered
            )
            if not hit:
                continue
            # x-cache is present on every CloudFront response (HITs too) —
            # only the ERROR refusal marks a block, not a cache HIT.
            if key == "x-cache":
                if "error from cloudfront" in lowered.get(key, ""):
                    return vendor
                continue
            return vendor
    return ""


def _classify_block_text(text: Optional[str]) -> str:
    """Classify a block vendor from page text ("" → unknown)."""
    if not text:
        return ""
    low = text.lower()[:6000]
    for vendor, phrases in _BLOCK_BODY_MARKERS:
        for phrase in phrases:
            if phrase in low:
                return vendor
    return ""


async def _detect_block_vendor(page: Any, response: Any = None) -> str:
    """Detect which WAF/CDN (if any) refused the request.

    Headers first (strongest signal — ``x-cache: Error from cloudfront``
    is CloudFront's own refusal marker), then the page body's tell-tale
    phrases ("The request could not be satisfied", "Just a moment…",
    Akamai's "Access Denied"). ``none`` when nothing matches.
    """
    headers = {}
    try:
        if response is not None:
            raw_headers = response.headers or {}
            headers = dict(raw_headers)
    except Exception:
        headers = {}
    vendor = _classify_block_headers(headers)
    if vendor:
        return vendor
    text = ""
    try:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception:
        text = ""
    return _classify_block_text(text) or "none"


async def _failure_forensics(page: Any, response: Any = None) -> dict:
    """Forensic evidence for a failed/refused browser operation.

    MP-6: the difference between "the tool is broken" and "the site is
    refusing us" — block vendor, a screenshot on disk, and a truncated DOM
    snapshot. Every leg is best-effort: a forensics failure must never
    mask the original tool error, so each piece degrades to None/"none".
    """
    out: dict = {"block_vendor": "none", "screenshot_path": None, "dom_snapshot": None}
    try:
        out["block_vendor"] = await _detect_block_vendor(page, response)
    except Exception:
        pass
    try:
        fd, path = tempfile.mkstemp(prefix="parousia-fail-", suffix=".png")
        os.close(fd)
        await page.screenshot(path=path)
        out["screenshot_path"] = path
    except Exception:
        pass
    try:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        out["dom_snapshot"] = (text or "")[:2048]
    except Exception:
        pass
    return out


def _http_status(response: Any) -> Optional[int]:
    """Best-effort int HTTP status from a Playwright response (None on
    anything unparseable — mocked responses in tests carry Mock status)."""
    try:
        if response is None:
            return None
        return int(response.status)
    except Exception:
        return None


# ── Tool schemas ──────────────────────────────────────


def browse_to_schema() -> dict:
    return {
        "name": "browse_to",
        "description": (
            "Navigate to a URL and optionally extract content. "
            "Returns the DOM as SDOM (structured DOM) for further processing. "
            "Session cookies persist across calls (persistent context). "
            "On HTTP >= 400 or navigation failure the result carries "
            "forensics: block_vendor, screenshot_path, dom_snapshot. "
            "MP-9: the result also carries 'stealth' (true/false) — whether "
            "full fingerprint hardening (config spatial.stealth_enabled) "
            "was active for this fetch. "
            "MP-10a: the optional 'proxy' argument names the transport for "
            "THIS request from the operator's spatial.proxies menu; '' "
            "(default) = direct egress, always — a request that does not "
            "ask NEVER sets a proxy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proxy": {
                    "type": "string",
                    "default": "",
                    "description": (
                        "MP-10a: optional named transport (a name from the "
                        "operator-configured spatial.proxies menu, e.g. "
                        "'residential-1'). '' or omitted = DIRECT egress, "
                        "always — proxying is opt-in and per-request. The "
                        "named proxy is used for this request's browser "
                        "context (a separate persistent context per "
                        "(agent, proxy)). Unknown names error with the "
                        "available list. 'tor' selects the managed Tor transport "
                        "(requires spatial.tor_enabled=true; fail-loud if unavailable)."
                    ),
                },
                "url": {"type": "string", "description": "URL to navigate to"},
                "timeout_ms": {"type": "integer", "description": "Timeout in milliseconds. Default: 30000."},
                "user_agent": {
                    "type": "string",
                    "description": "Optional per-navigation User-Agent override (consistent Sec-CH-UA client hints are applied). Default: the configured browser identity.",
                },
                "extract_mode": {
                    "type": "string",
                    "enum": ["standard", "content_only", "interactive_only"],
                    "description": "Extraction mode. Default: 'standard'.",
                },
            },
            "required": ["url"],
        },
    }


def interact_schema() -> dict:
    return {
        "name": "interact",
        "description": (
            "Perform an interaction with a web element (click, type, scroll_into_view, "
            "select, check, uncheck, hover, press). "
            "Session cookies persist across calls (persistent context). "
            "On failure the result carries forensics: block_vendor, "
            "screenshot_path, dom_snapshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proxy": {
                    "type": "string",
                    "default": "",
                    "description": (
                        "MP-10a: optional named transport (a name from the "
                        "operator-configured spatial.proxies menu). '' or "
                        "omitted = the agent's DEFAULT context (direct "
                        "egress unless the operator mapped a proxy). The "
                        "named proxy targets this request's browser context "
                        "(a separate persistent context per (agent, proxy)) "
                        "— the interaction runs on that transport's page. "
                        "Unknown names error with the available list. "
                        "'tor' selects the managed Tor transport "
                        "(requires spatial.tor_enabled=true; fail-loud if "
                        "unavailable)."
                    ),
                },
                "id": {"type": "string", "description": "Element ID to interact with"},
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "scroll_into_view", "select", "check", "uncheck", "hover", "press"],
                    "description": "Interaction action to perform",
                },
                "text": {"type": "string", "description": "Text to type (for 'type' action)"},
                "timeout_ms": {"type": "integer", "description": "Timeout in milliseconds. Default: 30000."},
            },
            "required": ["id", "action"],
        },
    }


def extract_page_state_schema() -> dict:
    return {
        "name": "extract_page_state",
        "description": (
            "Extract the current page state as SDOM (structured DOM). "
            "Returns SDOM content for further processing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["full", "changes", "context_only"],
                    "description": "Extraction mode. Default: 'full'.",
                },
            },
        },
    }


def fill_credentials_schema() -> dict:
    return {
        "name": "fill_credentials",
        "description": (
            "Fill a saved credential (username + password) into the current "
            "page's login form. Origin-bound: the current page origin must "
            "match the credential's stored origin. Returns a fill summary "
            "only — never the secret values."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Origin the credential is stored for (e.g. https://site.example)",
                },
                "handle": {
                    "type": "string",
                    "description": "Credential handle (as stored by the operator)",
                },
                "submit": {
                    "type": "boolean",
                    "description": "Also click a submit button after filling. Default: false.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Timeout in milliseconds for field lookups. Default: 30000.",
                },
            },
            "required": ["origin", "handle"],
        },
    }


def dump_form_schema() -> dict:
    return {
        "name": "dump_form",
        "description": (
            "Introspect every form on the current page BEFORE filling: per "
            "control — name, type, current value (passwords are NEVER "
            "returned: is_secret instead), associated label, placeholder, "
            "required/disabled/readonly/visibility, select options with "
            "human labels, radio/checkbox group options with labels, HTML "
            "limits (maxlength/minlength/min/max/pattern), captcha "
            "presence, and — optionally — an observed multi-select cap "
            "probe. Password values never leave the page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "probe_limits": {
                    "type": "boolean",
                    "description": (
                        "Best-effort runtime probe for multi-selects: "
                        "click-until-no-op and report the ACTUAL observed "
                        "selection cap (catches JS-enforced caps that are "
                        "invisible in the HTML, e.g. a silent 10-item "
                        "limit). Mutates the selection temporarily and "
                        "restores it. Default: false (never mutates)."
                    ),
                    "default": False,
                },
            },
        },
    }


ALL_SPATIAL_SCHEMAS = [
    browse_to_schema(),
    interact_schema(),
    extract_page_state_schema(),
    fill_credentials_schema(),
    dump_form_schema(),
]


# ── Tool handler registry ─────────────────────────────


class SpatialToolHandlers:
    """Handle MCP tool invocations for spatial tools."""

    def __init__(
        self,
        config: ParousiaConfig,
        browser_pool: BrowserPoolManager,
        serializer: SpatialSerializer,
        credential_store: Optional[CredentialStore] = None,
    ):
        self.config = config
        self.browser_pool = browser_pool
        self.serializer = serializer
        # MP-3 (part B): server-side credential store for fill_credentials.
        # None → the tool answers "credential store unavailable" (and can
        # never fall back to anything less structured).
        self.credential_store = credential_store

    async def dispatch(self, name: str, arguments: dict[str, Any], agent_id: str) -> str:
        """Route tool call to the correct handler and return JSON result."""
        handlers = {
            "browse_to": self._handle_browse_to,
            "interact": self._handle_interact,
            "extract_page_state": self._handle_extract_page_state,
            "fill_credentials": self._handle_fill_credentials,
            "dump_form": self._handle_dump_form,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown spatial tool: {name}"})
        try:
            result = await handler(arguments, agent_id)
        except Exception as e:
            logger.error("spatial tool error", extra={"tool": name, "error": str(e)})
            result = {"error": str(e)}
        return json.dumps(result)

    async def _apply_identity(
        self, page: Any, user_agent: Optional[str], timeout_ms: int
    ) -> None:
        """MP-3 (part A): apply a per-navigation UA override + client hints.

        The persistent context already carries the configured identity at
        launch (browser_pool._launch_kwargs). A ``user_agent`` argument
        overrides it for THIS navigation via CDP ``Emulation.setUserAgentOverride``
        (with full ``userAgentMetadata`` so the client hints stay consistent)
        and mirrors the high-entropy headers via ``set_extra_http_headers``.
        CDP unavailability (non-chromium, detached) degrades to the context UA.
        """
        if not user_agent:
            return
        hints = _client_hints_for(user_agent)
        try:
            cdp = await page.context.new_cdp_session(page)
            version = re.search(r"Chrome/(\d+)", user_agent)
            version = version.group(1) if version else "142"
            platform = hints["Sec-CH-UA-Platform"].strip('"')
            brands = [
                {"brand": "Chromium", "version": version},
                {"brand": "Google Chrome", "version": version},
                {"brand": "Not?A_Brand", "version": "99"},
            ]
            await cdp.send(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": user_agent,
                    "acceptLanguage": "en-US,en;q=0.9",
                    "platform": platform,
                    "userAgentMetadata": {
                        "brands": brands,
                        "fullVersionList": brands,
                        "fullVersion": f"{version}.0.0.0",
                        "platform": platform,
                        "platformVersion": "15.0.0",
                        "architecture": "x86",
                        "model": "",
                        "mobile": False,
                        "bitness": "64",
                        "wow64": False,
                    },
                },
            )
        except Exception as e:
            logger.debug("CDP UA override unavailable; using context UA", extra={"error": str(e)})
        try:
            await page.set_extra_http_headers(hints)
        except Exception as e:
            logger.debug("extra client-hint headers unavailable", extra={"error": str(e)})

    def _resolve_proxy(self, name: str):
        """MP-10a (issue #11 ws1, chunk 1 of 3): agent-called transport
        selection.

        ``name`` comes from the ``proxy`` argument on browse_to/interact.
        ``''`` → ``("", None)`` — DIRECT egress, always: the default NEVER
        sets a proxy (the whole point of the opt-in gate; the operator
        mapping still applies via the pool, exactly as before). A name
        must exist in the operator's ``spatial.proxies`` menu (a registry
        of available transports, NOT a routing rule); unknown names —
        including the reserved ``tor``, which chunk 2 will register —
        resolve to a clear error listing the available names.

        Returns ``(resolved_url, error_dict)``; exactly one is non-empty.
        """
        name = (name or "").strip()
        if not name:
            return "", None  # DIRECT egress, always (the opt-in gate)
        # MP-10b: the reserved name — Tor is handled BEFORE the named
        # registry (an operator "tor" menu entry can't shadow the
        # managed transport). Never resolves to direct egress.
        if name == "tor":
            return self._resolve_tor()
        proxies = getattr(getattr(self.config, "spatial", None), "proxies", None) or {}
        if name not in proxies:
            available = ", ".join(sorted(proxies)) if proxies else "(none configured)"
            return "", {"error": f"unknown proxy name '{name}'; available: {available}"}
        return str(proxies[name]), None

    def _resolve_tor(self):
        """MP-10b (issue #11 ws1, chunk 2 of 3): the managed Tor transport.

        The reserved ``"tor"`` name. OPT-IN (``spatial.tor_enabled``),
        AGENT-CALLED (only reachable via an explicit ``proxy="tor"``),
        FAIL-LOUD (every failure is a clear error — Tor is never
        silently skipped: the agent must not believe it is proxied when
        it isn't, and ``"tor"`` must NEVER resolve to direct egress).

        Returns ``(socks5_url, error_dict)``; exactly one is non-empty.
        """
        cfg = getattr(self.config, "spatial", None)
        if not bool(getattr(cfg, "tor_enabled", False)):
            return "", {
                "error": (
                    "Tor is not enabled (set spatial.tor_enabled=true) "
                    "to use proxy='tor'"
                )
            }
        try:
            url = self.browser_pool.tor_manager().ensure_running()
        except TorError as exc:
            return "", {"error": f"Tor transport unavailable: {exc}"}
        if not url:
            # Belt-and-braces: "tor" NEVER resolves to a direct/empty
            # override, even if the transport returned nothing.
            return "", {
                "error": "Tor transport unavailable: no SOCKS URL "
                "(refusing direct egress)"
            }
        return url, None

    async def _egress_identity(self, page: Any, agent_id: str, proxy_name: str = "") -> dict:
        """MP-3 (part A): the egress identity a browse result reports.

        Effective UA (read from the page — reflects any per-navigation
        override), the host's effective source IP (no packet sent), and the
        configured egress route (proxy server or "direct"), so an agent can
        tell "site refusing bots" from "wrong egress / site down".

        MP-10a: ``proxy_name`` — the agent-CALLED transport for THIS
        request. When set, the route reports the NAME (never the resolved
        URL — registry URLs may embed credentials); the operator mapping
        below is only the DEFAULT-path report, unchanged from MP-3.
        """
        ua = ""
        try:
            ua = await page.evaluate("() => navigator.userAgent")
        except Exception:
            # Page detached / evaluate unavailable → the configured identity.
            cfg = getattr(self.config, "spatial", None)
            ua = getattr(cfg, "user_agent", "") or ""
        proxy = ""
        if proxy_name:
            # The transport actually used for this request, by NAME.
            proxy = proxy_name
        else:
            try:
                raw = self.browser_pool.proxy_for(agent_id)
                # Only a real string counts as a route — anything else
                # (missing helper, unexpected type) reports "direct".
                if isinstance(raw, str):
                    proxy = raw
            except Exception:
                proxy = ""
        return {
            "user_agent": ua,
            "ip": _effective_egress_ip(),
            "proxy": proxy or "direct",
        }

    # ── browse_to ──────────────────────────────────────

    async def _handle_browse_to(self, args: dict, agent_id: str) -> dict:
        url = args["url"]
        timeout_ms = args.get("timeout_ms", 30000)
        extract_mode = args.get("extract_mode", "standard")
        user_agent = args.get("user_agent")
        # MP-10a: agent-CALLED transport for THIS request (name from the
        # spatial.proxies menu). '' / omitted = DIRECT, always.
        proxy_name = (args.get("proxy") or "").strip()
        proxy_url, proxy_error = self._resolve_proxy(proxy_name)
        if proxy_error:
            return proxy_error

        browser = await self.browser_pool.get_browser(agent_id, override_proxy=proxy_url)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            # Navigate the agent's persistent page
            page = browser.page
            # MP-3: per-navigation UA override + consistent client hints
            await self._apply_identity(page, user_agent, timeout_ms)
            response = await page.goto(url, timeout=timeout_ms)
            status = response.status if response is not None else 200

            # Extract content as SDOM, then serialize to a JSON-safe dict
            sdom = self.serializer.to_sdom(await page.content(), url, status)

            result = {
                "url": url,
                "extracted": True,
                "sdom": sdom.model_dump(),
                # MP-3: the egress identity this fetch presented — so the
                # agent can distinguish "refusing bots" from "site down".
                # MP-10a: proxy_name reports the transport actually used.
                "egress": await self._egress_identity(page, agent_id, proxy_name=proxy_name),
                # MP-4: captcha presence on the landed page (detect, never
                # solve) — the agent sees it without a second call.
                "captcha": await self._detect_captcha(page),
                # MP-10a: the agent-CALLED transport for this request, by
                # NAME ("" = direct; never the URL — it may embed creds).
                "proxy": proxy_name,
                # MP-9: whether full fingerprint hardening was active for
                # this fetch (config spatial.stealth_enabled).
                "stealth": bool(
                    getattr(
                        getattr(self.config, "spatial", None),
                        "stealth_enabled",
                        False,
                    )
                ),
            }
            # MP-6 (part B): a WAF refusal served as a normal HTTP error
            # response (the Empower CloudFront 403) is NOT "site down" —
            # flag the vendor + forensics on the result so the agent can
            # tell "the site is refusing us" from a broken tool.
            http_status = _http_status(response)
            if (http_status or 0) >= 400:
                result["refused"] = True
                result["http_status"] = http_status
                try:
                    result.update(await _failure_forensics(page, response))
                except Exception:
                    pass
            return result
        except Exception as e:
            logger.error("browse_to failed", extra={"url": url, "error": str(e)})
            result = {"error": f"Failed to browse to {url}: {str(e)}"}
            # MP-6: forensics on the failure path — best-effort, and a
            # forensics failure must never mask the original error.
            try:
                page
            except NameError:
                page = None
            if page is not None:
                try:
                    result.update(await _failure_forensics(page))
                except Exception:
                    pass
            return result

    # ── interact ───────────────────────────────────────

    async def _handle_interact(self, args: dict, agent_id: str) -> dict:
        element_id = args["id"]
        action = args["action"]
        text = args.get("text")
        timeout_ms = args.get("timeout_ms", 30000)
        # MP-10a: agent-CALLED transport for THIS request — the interaction
        # runs on that transport's page (its own persistent context, keyed
        # by (agent, proxy)). '' / omitted = the agent's DEFAULT context.
        proxy_name = (args.get("proxy") or "").strip()
        proxy_url, proxy_error = self._resolve_proxy(proxy_name)
        if proxy_error:
            return proxy_error

        browser = await self.browser_pool.get_browser(agent_id, override_proxy=proxy_url)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            page = browser.page

            # Perform the interaction based on action type
            if action == "click":
                await page.click(f"#{element_id}", timeout=timeout_ms)
            elif action == "type":
                if text is None:
                    return {"error": "Text required for 'type' action"}
                await page.type(f"#{element_id}", text, timeout=timeout_ms)
            elif action == "scroll_into_view":
                await page.scroll_into_view(f"#{element_id}")
            elif action == "select":
                value = args.get("value")
                if not value:
                    return {"error": "Value required for 'select' action"}
                await page.select_option(f"#{element_id}", value)
            elif action == "check":
                await page.check(f"#{element_id}")
            elif action == "uncheck":
                await page.uncheck(f"#{element_id}")
            elif action == "hover":
                await page.hover(f"#{element_id}")
            elif action == "press":
                key = args.get("key")
                if not key:
                    return {"error": "Key required for 'press' action"}
                await page.press(f"#{element_id}", key)
            else:
                return {"error": f"Unknown interaction action: {action}"}

            return {
                "id": element_id,
                "action": action,
                "success": True,
                # MP-10a: the agent-CALLED transport for this request, by
                # NAME ("" = the default context; never the URL).
                "proxy": proxy_name,
            }
        except Exception as e:
            logger.error("interact failed", extra={"element_id": element_id, "action": action, "error": str(e)})
            result = {"error": f"Failed to interact with {element_id} using {action}: {str(e)}"}
            # MP-6: cheap forensics on the interact failure path too —
            # same evidence (vendor/screenshot/DOM), same best-effort rule.
            try:
                page
            except NameError:
                page = None
            if page is not None:
                try:
                    result.update(await _failure_forensics(page))
                except Exception:
                    pass
            return result

    # ── extract_page_state ─────────────────────────────

    async def _handle_extract_page_state(self, args: dict, agent_id: str) -> dict:
        mode = args.get("mode", "full")

        browser = await self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}

        try:
            page = browser.page

            # Extract content as SDOM, then serialize to a JSON-safe dict
            sdom = self.serializer.to_sdom(await page.content(), page.url, 200)

            return {
                "mode": mode,
                "extracted": True,
                "sdom": sdom.model_dump(),
            }
        except Exception as e:
            logger.error("extract_page_state failed", extra={"mode": mode, "error": str(e)})
            return {"error": f"Failed to extract page state in {mode} mode: {str(e)}"}

    # ── fill_credentials (MP-3 part B) ──────────────────

    # Username-field heuristic, most specific first. The first visible
    # matching element before/around the password field gets the username.
    _USERNAME_SELECTORS = (
        "input[type='email']",
        "input[name*='user' i]",
        "input[id*='user' i]",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[name*='login' i]",
        "input[id*='login' i]",
        "input[type='text']",
        "input:not([type])",
    )
    _PASSWORD_SELECTOR = "input[type='password']"
    _SUBMIT_SELECTORS = (
        "button[type='submit']",
        "input[type='submit']",
        "button:not([type])",
    )

    async def _handle_fill_credentials(self, args: dict, agent_id: str) -> dict:
        """Origin-bound credential fill — returns a summary, never the secret.

        The credential is resolved from the server-side store by
        (origin, handle); the fill is refused unless the browser's CURRENT
        page origin matches the stored origin, so a handle saved for one
        site can never be replayed into another. The response carries only
        which fields were filled — never username or password values.
        """
        origin = args.get("origin", "")
        handle = args.get("handle", "")
        submit = bool(args.get("submit", False))
        timeout_ms = args.get("timeout_ms", 30000)

        if self.credential_store is None:
            return {"error": "credential store unavailable"}

        browser = await self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}
        page = browser.page

        # ── Origin binding: the CURRENT page must match the stored origin.
        want_origin = normalize_origin(origin)
        if not want_origin:
            return {"error": "origin must be a URL or scheme://host"}
        try:
            current_origin = normalize_origin(page.url)
        except Exception:
            current_origin = ""
        if current_origin != want_origin:
            return {
                "error": (
                    f"refusing to fill: current page origin {current_origin or 'unknown'} "
                    f"does not match credential origin {want_origin}"
                ),
                "origin_bound": True,
            }

        cred = self.credential_store.get(want_origin, handle)
        if cred is None:
            return {"error": f"no credential stored for origin {want_origin} handle {handle}"}

        # ── Fill. Username via heuristic (first visible candidate), then
        # the first visible password field. fill() types into the field;
        # values never leave this scope into the response.
        try:
            user_locator = None
            user_selector = None
            for selector in self._USERNAME_SELECTORS:
                locator = page.locator(selector).first
                if await locator.is_visible():
                    user_locator = locator
                    user_selector = selector
                    break

            pwd_locator = page.locator(self._PASSWORD_SELECTOR).first
            pwd_visible = await pwd_locator.is_visible()
            if not pwd_visible:
                return {
                    "error": "no visible password field on this page — navigate to the login form first"
                }
            if user_locator is None:
                # No recognizable username field — some login forms are
                # password-only (e.g. passphrase gates). Fill just the password.
                await pwd_locator.fill(cred.password)
                filled_fields = ["password"]
            else:
                await user_locator.fill(cred.username)
                await pwd_locator.fill(cred.password)
                filled_fields = ["username", "password"]

            submitted = False
            if submit:
                for selector in self._SUBMIT_SELECTORS:
                    locator = page.locator(selector).first
                    if await locator.is_visible():
                        await locator.click()
                        submitted = True
                        break

            # Summary only — the secret values are NEVER echoed.
            return {
                "filled": True,
                "origin": want_origin,
                "handle": handle,
                "fields_filled": filled_fields,
                "submitted": submitted,
            }
        except Exception as e:
            # Never include cred values in the error path.
            logger.error(
                "fill_credentials failed",
                extra={"origin": want_origin, "handle": handle, "error": str(e)},
            )
            return {"error": f"Failed to fill credential for {want_origin}: {str(e)}"}

    # ── dump_form + captcha (MP-4, issues #18) ──────────

    async def _detect_captcha(self, page: Any) -> dict:
        """Detect (never solve) captcha presence on the current page.

        Returns ``{present, vendor, type, sitekey}`` — vendor one of
        ``recaptcha_v2 | recaptcha_v3 | hcaptcha | turnstile | other |
        none``; ``type`` is ``invisible`` vs ``visible`` (an invisible
        challenge that passed a real-UA session is worth knowing so the
        agent proceeds; a visible one means hand off to the human).
        """
        try:
            signals = await page.evaluate(CAPTCHA_JS)
        except Exception as e:
            logger.debug("captcha detection unavailable", extra={"error": str(e)})
            return {"present": False, "vendor": "none", "type": None, "sitekey": None}
        return _captcha_from_signals(signals)

    async def _handle_dump_form(self, args: dict, agent_id: str) -> dict:
        """Form introspection: per-control structure, limits, and captcha —
        BEFORE the agent fills anything.

        Read-only by default: the extraction is pure DOM (no clicks, no
        mutation). The optional ``probe_limits`` flag additionally runs the
        click-until-no-op multi-select cap probe (the ONLY way to see a
        JS-enforced cap like Empower's silent 10-item limit — it is declared
        nowhere in the HTML); the probe mutates the selection temporarily
        and restores it, so it stays opt-in.

        Password values NEVER leave the page: the extraction nulls them
        in-page and ``_assemble_form_report`` re-masks on the Python side.
        """
        probe = bool(args.get("probe_limits", False))

        browser = await self.browser_pool.get_browser(agent_id)
        if not browser:
            return {"error": f"Unable to get browser for agent {agent_id}"}
        page = browser.page

        try:
            raw = await page.evaluate(DUMP_FORM_JS)
            report = _assemble_form_report(raw)
            report["captcha"] = await self._detect_captcha(page)
            if probe:
                probe_raw = await page.evaluate(MULTISELECT_PROBE_JS)
                report["probe"] = (probe_raw or {}).get("probed", [])
            return report
        except Exception as e:
            logger.error("dump_form failed", extra={"error": str(e)})
            return {"error": f"Failed to dump form: {str(e)}"}
