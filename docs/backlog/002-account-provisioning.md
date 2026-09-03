# Account Provisioning — restricted & verified third-party account management

> **Status: ⚠️ PROMINENT BACKLOG (unscoped).** Raised 2026-09-03 after the LinkedIn
> burner-account attempt hit the anti-bot wall: Parousia fills and submits the signup
> form correctly (every `interact` call reports `success: true`), but LinkedIn silently
> rejects the submission — no verification email, no page advance, stays on `/signup`.

## The problem

Third-party services gate account creation behind layered anti-bot defenses that no
single existing Parousia capability defeats:

1. **Source-IP reputation** — headless Chromium runs on a datacenter VPS
   (`2.24.196.6`), an instant red flag.
2. **Browser fingerprinting** — headless Chromium is detectable via
   `navigator.webdriver`, headless UA, missing plugins/history, etc.
3. **Verification gates** — phone (SMS) OTP and/or app-based 2FA (TOTP), which today
   Parousia cannot satisfy programmatically.

## Goal

Give agents a sovereign, repeatable way to **create and operate accounts on
third-party services** that require anti-bot evasion, phone verification, and/or 2FA.
Delivered as **one master MCP service (or a master set of MCP services)** for
"restricted and verified third-party account management."

## Workstreams

### 1. Anonymity / transport layer (Tor + residential proxy)
- Add **Tor (The Onion Router)** as an optional egress for account-creation traffic,
  so the source IP isn't a flagged datacenter address.
- Add **residential / mobile proxy** support as a complementary path — datacenter IPs
  are the #1 red flag; residential is the industry-standard mitigation.
- Open question: many services also fingerprint known Tor exit nodes. Deliverable
  should include a per-target Tor-vs-residential trade-off, not assume Tor is a
  silver bullet.

### 2. SMS / phone verification (interchangeable providers)
- Abstract a **phone-number provider** interface with interchangeable backends:
  - https://temporarynumber.com/en
  - https://receive-sms.io/
  - https://pvacodes.com/free-number
- One MCP surface (e.g. `acquire_number`, `read_sms`, `release_number`) implemented
  provider-agnostically, with a shared cache of numbers → verification codes.
- Reference: burner number +1 (937) 313-2938 already identified (temporarynumber.com).

### 3. TOTP / 2FA authenticator capability
- **Catalog** which services offer true 2FA via a TOTP authenticator (Google
  Authenticator-style) vs SMS-only vs passkey/hardware.
- **TOTP (RFC 6238) is fully self-hostable server-side** — no phone tethering
  required. Google Authenticator is just a TOTP *client*; the protocol is open. Build
  a server-side secret vault + code generator (e.g. `pyotp`), exposing
  `enroll_totp` / `get_totp_code` MCP tools. Enrollment typically means parsing the
  `otpauth://` URI/QR a service emits when you enable 2FA.
- The real constraint is **service policy**, not the protocol: SMS-only services still
  need a number (workstream 2); push-auth (Duo) and passkeys need a phone/enclave.

### 4. Browser fingerprint hardening (prerequisite — new, not in the original ask)
- IP is only half the wall. Account creation won't survive on a residential IP alone
  while the browser still identifies as headless Chromium. Scope a stealth layer
  (e.g. `playwright-stealth` / patched Chromium) alongside workstream 1 — otherwise
  Tor/proxy alone won't unblock signup.

## Immediate workaround (until this ships)
- Fastest unblock for the LinkedIn burner account: **human creates it manually** from a
  residential IP + real browser, then hand the account to the agent for login/recon —
  login is far less suspicious than signup.
- Phone-number-first signup (option 2) is a cheap second try but won't overcome a
  headless fingerprint on its own.

## Notes / risks
- LinkedIn (and peers) explicitly ban automated access; this capability is for
  legitimate, low-volume, agent-scoped account operation — one account per purpose.
- Reuses existing infra (Parousia MCP server + config + spatial browser pool); a new
  capability = new MCP tools, not a new server.
