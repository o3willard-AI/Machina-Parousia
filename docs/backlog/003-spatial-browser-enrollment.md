# Spatial Browser — enrollment / anti-bot field findings & extension proposals

> **Status: BACKLOG (unscoped).** Raised 2026-11-25 from a real field report
> (Ricky, node-grid generalist @ 192.168.101.230) while attempting a live enrollment
> on `https://www.empowerinnovation.net/` (CloudFront WAF). The enrollment ultimately
> succeeded only via a **local-Hermes fallback** (local Chromium + local vault + UA
> override) — the Parousia browser itself was hard-blocked. This doc captures the
> evidence and the concrete extension proposals that would let the task run on
> Parousia alone, with no local fallback.

## The field evidence

- `browse_to https://www.empowerinnovation.net/` → SDOM `meta.status = 403`,
  title "ERROR: The request could not be satisfied" (CloudFront WAF). Empty
  interactive + content arrays.
- Local Chromium on the same task: form loaded but also 403'd, UA
  `HeadlessChrome/156.0.0.0`.
- After a CDP `Network.setUserAgentOverride` to a normal desktop Chrome UA, the
  **same** local browser loaded the real form immediately.
- Plain `curl` (no browser at all) was never blocked by the site.

**Diagnosis:** the block is **bot-signature based** (headless UA / automation tells),
not IP or geo. The site serves non-browser clients happily; it refuses the
headless-browser signature. This is the same wall already documented in
`002-account-provisioning.md` workstream #4, but observed here on a *simple
enrollment* — not just account creation.

## Why Parousia failed where the local fallback succeeded

The Parousia browser exposes **no control over its identifying surface** — no
user-agent override, no client-hints control, no way to suppress the `webdriver`
flag. It presents the default headless signature and is refused before any tool can
help. The tool calls themselves were correct; the tool simply has no lever for the
one thing that mattered.

## Extension proposals (Ricky, prioritized)

### P0 — Identity & egress control (without this the browser is unusable on WAF'd sites)
- Per-navigation **user-agent override** + consistent Client Hints.
- A **"realistic session" profile**: suppress `navigator.webdriver`, present normal
  plugins/languages/viewport, clear other headless tells.
- Return the **egress identity (IP + UA)** in every `browse_to` result, so the agent
  can distinguish "the site is refusing bots" from "the site is down".
- Optional **egress/proxy selection** so an enrollment can originate from an
  attributable IP instead of a datacenter one.

*Overlaps:* `002-account-provisioning.md` workstream #4 (fingerprint hardening) and
workstream #1 (Tor/residential proxy). This extends both with the **per-navigation**
and **egress-identity-reporting** specifics.

### P0 — A credential path for the *remote* browser
- Today the vault is **local-only**: `browser_vault` injects server-side into
  *local* Hermes' managed browser. There is no way to get a saved login into the
  Parousia browser, so **any enrollment needing a password is impossible on
  Parousia**, independent of the 403.
- Needed: Parousia integrates with a vault (its own, or a broker) exposing
  `fill(origin, handle)`; or the agent's vault can inject into a nominated remote
  session. Invariant to preserve: the agent never sees the secret, and fills stay
  origin-bound.

*This is the largest genuinely-new gap — it is not covered anywhere in the existing
Phase 3 backlog or `002-account-provisioning.md`.*

### P1 — Form introspection as a first-class primitive (highest practical value)
- A `dump_form` action returning, per control: name, type, current value, associated
  label text, required-ness, hidden fields, select options, and radio/checkbox values
  **with their human labels**.
- Critically: **JS-enforced / client-side constraints.** On Empower's form the sector
  multi-select **silently capped at 10** — declared nowhere in the HTML, found only by
  clicking and re-counting (an 11th click no-ops silently). Return a
  "limits/validation" section: max/min selections, field rules, and any constraint
  violation the page would raise.

### P1 — CAPTCHA detection & reporting (detect, never solve)
- Report presence, vendor, and type (invisible vs visible) + sitekey. Empower ran
  invisible reCAPTCHA v2, which **passed for a real-UA session** — valuable to know so
  the agent proceeds rather than aborting. A **visible** challenge should mean "stop
  and hand off to the human."

### P2 — Couple the browser to the inbox (double opt-in)
- Enrollment routinely needs the confirmation email. Parousia owns **both** the inbox
  and the browser — wire them:
  - `wait_for_email(pattern, timeout)` → the message;
  - a way to pull links out and open them (`browse_to` the confirmation URL).
- This makes "join a mailing list" one reliable capability instead of two hand-glued
  halves.

### P2 — Failure forensics
- On any browse/interact failure return **screenshot + DOM snapshot + HTTP status +
  block vendor** (CloudFront vs Cloudflare vs Akamai). That is the difference between
  "the tool is broken" and "the site is refusing us".
- Confirm and advertise **session persistence** (cookies across calls) explicitly —
  enrollments depend on it and the agent should not have to guess.

*Overlaps:* PRD Phase 3 backlog B4 (screenshot capture); the block-vendor reporting
is new.

### P2 — Option semantics
- Checkbox/radio values are opaque ids (e.g. `Form[429][]=2204`). Return the human
  label next to each value so the agent chooses on **meaning**, not ids.

## Packaging suggestion: an "enroll" composite capability

With the above, a documented composite — "given a URL, join/subscribe" — becomes
repeatable:

> detect signup vs email-subscribe → map the form → separate what is **policy**
> (role / consent / name — ask the human via the keyword register) from what is
> **mechanical** → fill → submit → verify the real outcome → complete double opt-in
> from the inbox → record the vault handle.

That is the sequence a future agent should run end-to-end **on Parousia** with no
local fallbacks.

## Notes / risks

- The local fallback worked, but required a working local Chromium (missing distro
  libs → needed root), a local vault, and a UA override — exactly the local
  dependencies Parousia is supposed to remove.
- Scope stays aligned with `002-account-provisioning.md`: legitimate, low-volume,
  agent-scoped operation — one account/subscription per purpose. Nothing here implies
  CAPTCHA *solving* or high-volume automation.
- All items are additive MCP tools / config to the existing spatial module — no new
  server, no new package.
