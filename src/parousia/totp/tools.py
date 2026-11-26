"""MCP tool schemas + handlers for the TOTP vault (MP-8).

Four tools, account-scoped like the email tools: an agent enrolls/reads/
lists/removes only its OWN enrollments (the store keys everything by
``agent_id``; a cross-agent handle reads as not-found, never a 500).

The base32 secret appears ONLY inside the store (encrypted at rest) and
inside the compute-a-code path (decrypted in memory, handed to pyotp).
No tool response ever serializes it.
"""

import json
import re
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from parousia.totp.store import (
    TOTPStore,
    TOTPStoreError,
    validate_params,
    validate_secret,
)


def slugify_handle(issuer: str, account_name: str) -> str:
    """Derive a stable handle from issuer + account name.

    Slugified (`Example Corp` → `example-corp`) and joined with `:` —
    unique per agent by construction (two accounts collide only when
    both issuer and account name collide).
    """
    def slug(text: str) -> str:
        text = (text or "").strip().lower()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        return text.strip("-")

    issuer_s = slug(issuer)
    account_s = slug(account_name)
    if issuer_s and account_s:
        return f"{issuer_s}:{account_s}"
    return issuer_s or account_s or "totp"


def parse_otpauth_uri(uri: str) -> dict:
    """Parse an ``otpauth://totp/...`` URI (Google Authenticator key URI
    format, RFC 6238 / the de-facto `otpauth` scheme).

    Extracts secret (base32), issuer, account name, algorithm (default
    SHA1), digits (default 6), period (default 30). The label may carry
    ``issuer:account`` — the issuer label wins over the ``issuer`` query
    param when present (the label is what the service actually shows).
    """
    raw = (uri or "").strip()
    if not raw.lower().startswith("otpauth://"):
        raise TOTPStoreError("not an otpauth:// URI")
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme != "otpauth":
        raise TOTPStoreError(f"unsupported otpauth scheme {scheme!r} (want totp)")
    if (parts.netloc or "").lower() != "totp":
        raise TOTPStoreError(
            f"unsupported otpauth type {parts.netloc!r} (only totp is supported)"
        )
    query = parse_qs(parts.query)

    def one(key: str) -> str:
        values = query.get(key, [])
        return values[0] if values else ""

    secret = one("secret")
    normalized = validate_secret(secret)
    algorithm = (one("algorithm") or "sha1").lower()
    digits_str, period_str = one("digits"), one("period")
    try:
        digits = int(digits_str) if digits_str else 6
        period = int(period_str) if period_str else 30
    except ValueError as e:
        raise TOTPStoreError("digits/period must be integers") from e
    validate_params(algorithm, digits, period)

    # Label: "issuer:account" or bare "account" (percent-decoded).
    label = unquote(parts.path.lstrip("/"))
    if ":" in label:
        label_issuer, label_account = label.split(":", 1)
        label_issuer = label_issuer.strip()
    else:
        label_issuer, label_account = "", label.strip()
    issuer = label_issuer or one("issuer") or label_account or ""
    account_name = label_account or one("issuer") or label

    return {
        "secret": normalized,  # caller stores it; never serialized onward
        "issuer": issuer.strip(),
        "account_name": account_name.strip(),
        "algorithm": algorithm,
        "digits": digits,
        "period": period,
        "handle": slugify_handle(issuer, account_name),
    }


# ── Tool schemas ─────────────────────────────────────────────────


def enroll_totp_schema() -> dict:
    return {
        "name": "enroll_totp",
        "description": (
            "Enroll a TOTP authenticator (RFC 6238) from the otpauth:// URI "
            "a service emits when you enable app-based 2FA. Stores the "
            "secret SERVER-SIDE, encrypted; returns the enrollment summary "
            "(handle/issuer/account/algorithm/digits/period) — never the "
            "secret. Re-enrolling the same service replaces it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "otpauth_uri": {
                    "type": "string",
                    "description": "otpauth://totp/... URI (secret, issuer, digits, period)",
                },
                "handle": {
                    "type": "string",
                    "description": "Optional explicit handle (default: slug of issuer+account)",
                },
            },
            "required": ["otpauth_uri"],
        },
    }


def get_totp_code_schema() -> dict:
    return {
        "name": "get_totp_code",
        "description": (
            "Compute the CURRENT TOTP code for an enrolled handle "
            "(use it in the 2FA prompt within valid_seconds). Returns "
            "{code, valid_seconds} — never the secret."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Enrollment handle (from enroll_totp/list_totp)",
                },
            },
            "required": ["handle"],
        },
    }


def list_totp_schema() -> dict:
    return {
        "name": "list_totp",
        "description": (
            "List YOUR enrolled TOTP authenticators (handles + metadata; "
            "never secrets)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    }


def remove_totp_schema() -> dict:
    return {
        "name": "remove_totp",
        "description": (
            "Delete ONE of YOUR enrolled TOTP authenticators by handle. "
            "{removed: bool}; a handle belonging to another agent is "
            "refused (it is not yours to remove)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Enrollment handle to remove",
                },
            },
            "required": ["handle"],
        },
    }


ALL_TOTP_SCHEMAS = [
    enroll_totp_schema(),
    get_totp_code_schema(),
    list_totp_schema(),
    remove_totp_schema(),
]


# ── Handlers ─────────────────────────────────────────────────────


class TotpHandlers:
    """Account-scoped TOTP tool handlers (dispatched by mcp_server)."""

    def __init__(self, store: Optional[TOTPStore] = None):
        # None → the tools answer "TOTP store unavailable" (never a 500,
        # never a silent no-op).
        self.store = store

    async def dispatch(self, name: str, arguments: dict[str, Any], agent_id: str) -> str:
        """Route a tool call to its handler; JSON result. Only the caller's
        OWN enrollments are ever touched (the store keys by agent_id)."""
        handlers = {
            "enroll_totp": self._handle_enroll,
            "get_totp_code": self._handle_code,
            "list_totp": self._handle_list,
            "remove_totp": self._handle_remove,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown TOTP tool: {name}"})
        try:
            result = await handler(arguments, agent_id)
        except TOTPStoreError as e:
            # Operator-input problems (bad URI, bad params) → clean errors.
            return json.dumps({"error": str(e)})
        except Exception as e:  # noqa: BLE001 — never leak internals
            return json.dumps({"error": f"TOTP tool failed: {e}"})
        return json.dumps(result)

    def _require_store(self) -> TOTPStore:
        if self.store is None:
            raise TOTPStoreError("TOTP store unavailable")
        return self.store

    # ── enroll ──

    async def _handle_enroll(self, args: dict, agent_id: str) -> dict:
        store = self._require_store()
        uri = args.get("otpauth_uri", "")
        if not uri.strip():
            return {"error": "otpauth_uri is required"}
        parsed = parse_otpauth_uri(uri)
        # An explicit handle overrides the derived slug.
        handle = (args.get("handle") or "").strip() or parsed["handle"]
        enrolled = store.enroll(
            agent_id=agent_id,
            handle=handle,
            issuer=parsed["issuer"],
            account_name=parsed["account_name"],
            secret=parsed["secret"],
            algorithm=parsed["algorithm"],
            digits=parsed["digits"],
            period=parsed["period"],
        )
        # Response: metadata ONLY — the secret never leaves the store path.
        return {
            "enrolled": True,
            "handle": enrolled.handle,
            "issuer": enrolled.issuer,
            "account_name": enrolled.account_name,
            "algorithm": enrolled.algorithm,
            "digits": enrolled.digits,
            "period": enrolled.period,
        }

    # ── code ──

    async def _handle_code(self, args: dict, agent_id: str) -> dict:
        import time as _time

        import pyotp

        store = self._require_store()
        handle = (args.get("handle") or "").strip()
        if not handle:
            return {"error": "handle is required"}
        entry = store.get_secret(agent_id, handle)
        if entry is None:
            # Cross-agent AND missing handles read identically: clean
            # not-found, never a 500, never a hint about other agents.
            return {"error": f"no TOTP enrollment for handle {handle}"}
        totp = pyotp.TOTP(
            entry.secret,
            digits=entry.digits,
            interval=entry.period,
            digest=getattr(__import__("hashlib"), entry.algorithm),
        )
        code = totp.at(_time.time())
        # Seconds until the current code rotates (0 <= valid < period).
        now = _time.time()
        valid_seconds = entry.period - (now % entry.period)
        # Only the code — never the secret, never the entry itself.
        return {
            "handle": entry.handle,
            "issuer": entry.issuer,
            "code": code,
            "digits": entry.digits,
            "valid_seconds": int(valid_seconds),
        }

    # ── list ──

    async def _handle_list(self, args: dict, agent_id: str) -> dict:
        store = self._require_store()
        entries = store.list(agent_id)
        # list() returns metadata only (store strips secrets at the SQL
        # level — secret_enc never reaches the driver here).
        return {"entries": entries, "count": len(entries)}

    # ── remove ──

    async def _handle_remove(self, args: dict, agent_id: str) -> dict:
        store = self._require_store()
        handle = (args.get("handle") or "").strip()
        if not handle:
            return {"error": "handle is required"}
        # Account-scoped delete: another agent's handle is simply not under
        # this agent_id — removed: False, indistinguishable from missing.
        removed = store.remove(agent_id, handle)
        return {"removed": removed, "handle": handle}