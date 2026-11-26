# TOTP / 2FA authenticator

Parousia can hold **time-based one-time password (TOTP)** secrets so an agent can complete app-based two-factor authentication without ever holding the raw secret itself. This is the server-side half of issue #11 (account provisioning): when a service emits an `otpauth://` URI during 2FA setup, the agent enrolls it here and later asks for the current code.

## Tools

| Tool | What it does |
|------|--------------|
| `enroll_totp` | Parse an `otpauth://totp/...` URI and store the secret. Returns the enrollment summary (handle, issuer, account, algorithm, digits, period) — never the secret. Re-enrolling the same service replaces it. |
| `get_totp_code` | Compute the **current** TOTP code for an enrolled handle. Returns `{code, valid_seconds}` — never the secret. |
| `list_totp` | List your enrolled authenticators (handle + metadata; never secrets). |
| `remove_totp` | Delete one of your enrolled authenticators by handle. |

## Security model

- **Encrypted at rest.** The base32 secret is Fernet-encrypted before it touches the SQLite store; the Fernet key lives in a 0600 key file. An empty/invalid key **fails closed** (`TOTPStoreError`), never serves garbage.
- **The secret never leaves the store path.** `enroll_totp` returns metadata only; `get_totp_code` returns the code and its remaining validity, never the secret. The decrypted secret exists only in-memory on the compute-a-code path, handed straight to `pyotp`.
- **Account-scoped.** Enrollments are keyed by `(agent_id, handle)`. Another agent's handle reads as `None` (indistinguishable from absent) — cross-agent access and `remove_totp` are refused.
- **RFC 6238.** `pyotp` computes codes; parameters are validated on enroll — `sha1`/`sha256`/`sha512` only, digits 6–10, positive period.
- **Never recorded to memory.** TOTP codes and enrollment metadata are not written to the memory layer.

## Usage flow

1. A service shows an `otpauth://totp/Example:alice@example.com?secret=...&issuer=Example` QR/URI during 2FA setup.
2. `enroll_totp` with that URI → store the secret, note the returned `handle`.
3. When the service asks for the 6-digit code, `get_totp_code` with the handle → submit the code before `valid_seconds` elapses.
4. Done with the service → `remove_totp`.

Enrollment is opt-in and self-service: an agent can only manage its own authenticators.
