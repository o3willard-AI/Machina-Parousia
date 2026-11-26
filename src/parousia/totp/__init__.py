"""Parousia TOTP vault — server-side RFC 6238 authenticator (MP-8).

Enrollment happens from an `otpauth://totp/...` URI (the string a service
emits when you enable app-based 2FA); the base32 secret is stored
**Fernet-encrypted at rest** and only ever decrypted **in memory** to
compute a code. The secret never appears in any tool response.
"""

from parousia.totp.store import TOTPStore, TOTPStoreError
from parousia.totp.tools import (
    ALL_TOTP_SCHEMAS,
    TotpHandlers,
    enroll_totp_schema,
    get_totp_code_schema,
    list_totp_schema,
    remove_totp_schema,
    parse_otpauth_uri,
    slugify_handle,
)

__all__ = [
    "ALL_TOTP_SCHEMAS",
    "TotpHandlers",
    "TOTPStore",
    "TOTPStoreError",
    "enroll_totp_schema",
    "get_totp_code_schema",
    "list_totp_schema",
    "remove_totp_schema",
    "parse_otpauth_uri",
    "slugify_handle",
]