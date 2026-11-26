"""Server-side TOTP vault — SQLite + Fernet, account-scoped (MP-8).

Follows the MP-3 `credential_store.py` pattern: a Fernet key file beside
the database, encrypt-on-write / decrypt-on-read, best-effort key-file
handling that fails closed on corrupt key material. The TOTP store is a
**separate module** with a different access model — account-scoped, and
the decrypted secret exists only inside `get_secret` (to compute a code),
never in a tool response.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from cryptography.fernet import Fernet, InvalidToken

# Sensible deployment defaults (the MP-3 convention: under /var/lib/parousia).
DEFAULT_DB_PATH = "/var/lib/parousia/totp.db"
DEFAULT_KEY_PATH = "/var/lib/parousia/totp_store.key"

# RFC 4231 digests pyotp accepts; RFC 6238's reference is SHA1 (the
# universal interop default every authenticator app speaks).
_KNOWN_ALGORITHMS = ("sha1", "sha256", "sha512")


class TOTPStoreError(Exception):
    """Raised when the TOTP store cannot be opened, or its key material is
    missing/corrupt (fail closed), or a stored secret fails to decrypt."""


class StoredTOTP(NamedTuple):
    """One enrolled TOTP (metadata + the decrypted secret).

    The decrypted secret lives in this object **only** on the
    compute-a-code path (`get_secret`); every listing/serialization
    surface uses `TOTPStore.list`, which returns metadata only.
    """

    agent_id: str
    handle: str
    issuer: str
    account_name: str
    secret: str
    algorithm: str
    digits: int
    period: int
    created_at: str


def validate_secret(secret: str) -> str:
    """Normalize + validate a base32 TOTP secret (RFC 6238 §4).

    Uppercases, strips the padding/whitespace services sometimes emit,
    and validates the base32 alphabet. Empty/invalid → TOTPStoreError —
    enroll must refuse a secret no authenticator app will ever accept.
    """
    import base64

    normalized = (secret or "").strip().upper().replace(" ", "").replace("=", "")
    if not normalized:
        raise TOTPStoreError("TOTP secret is empty")
    try:
        base64.b32decode(normalized + "=" * (-len(normalized) % 8))
    except Exception as e:
        raise TOTPStoreError("TOTP secret is not valid base32") from e
    return normalized


def validate_params(algorithm: str, digits: int, period: int) -> None:
    """Validate the RFC 6238 parameters an otpauth:// URI can carry."""
    if (algorithm or "").lower() not in _KNOWN_ALGORITHMS:
        raise TOTPStoreError(
            f"unsupported TOTP algorithm {algorithm!r} "
            f"(want sha1|sha256|sha512)"
        )
    if not isinstance(digits, int) or not (6 <= digits <= 10):
        raise TOTPStoreError(f"digits must be 6..10 (got {digits!r})")
    if not isinstance(period, int) or period <= 0:
        raise TOTPStoreError(f"period must be a positive int (got {period!r})")


class TOTPStore:
    """SQLite + Fernet TOTP vault, keyed (agent_id, handle)."""

    KEY_MODE = 0o600
    DB_MODE = 0o600

    def __init__(self, db_path: str = DEFAULT_DB_PATH, key_path: str = DEFAULT_KEY_PATH):
        self.db_path = Path(db_path)
        self.key_path = Path(key_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._fernet: Optional[Fernet] = None

    # ── open/close ───────────────────────────────────────────────

    def connect(self) -> "TOTPStore":
        """Open (creating directories/tables/key on first use)."""
        key = self._load_or_create_key()
        self._fernet = Fernet(key)

        parent = self.db_path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.db_path.exists()
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS totp_secrets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                handle TEXT NOT NULL,
                issuer TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                secret_enc BLOB NOT NULL,
                algorithm TEXT NOT NULL DEFAULT 'sha1',
                digits INTEGER NOT NULL DEFAULT 6,
                period INTEGER NOT NULL DEFAULT 30,
                created_at TEXT NOT NULL,
                UNIQUE (agent_id, handle)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_totp_agent_handle "
            "ON totp_secrets(agent_id, handle)"
        )
        self._conn.commit()
        if is_new:
            try:
                os.chmod(self.db_path, self.DB_MODE)
            except OSError:
                pass
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _require(self) -> tuple[sqlite3.Connection, Fernet]:
        if self._conn is None or self._fernet is None:
            raise TOTPStoreError("TOTP store is not connected")
        return self._conn, self._fernet

    # ── key management (MP-3 pattern) ────────────────────────────

    def _load_or_create_key(self) -> bytes:
        """Load (or generate + persist, 0600) the store's Fernet key."""
        if self.key_path.exists():
            data = self.key_path.read_bytes()
            if not data:
                raise TOTPStoreError("TOTP store key file is empty")
            try:
                Fernet(data)
            except Exception as e:
                # Invalid/rotated key material — fail closed, never serve
                # garbage or the wrong plaintext.
                raise TOTPStoreError(
                    "TOTP store key is invalid (rotated or corrupt)"
                ) from e
            return data
        parent = self.key_path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # 0600 from the start: temp file, chmod, rename — the key never
        # exists group/world-readable.
        tmp = self.key_path.with_suffix(self.key_path.suffix + ".tmp")
        tmp.write_bytes(key)
        os.chmod(tmp, self.KEY_MODE)
        tmp.replace(self.key_path)
        return key

    # ── crypto ───────────────────────────────────────────────────

    def _encrypt(self, fernet: Fernet, value: str) -> bytes:
        return fernet.encrypt(value.encode("utf-8"))

    def _decrypt(self, fernet: Fernet, blob: bytes) -> str:
        try:
            return fernet.decrypt(bytes(blob)).decode("utf-8")
        except (InvalidToken, TypeError, ValueError) as e:
            raise TOTPStoreError("stored TOTP secret failed to decrypt") from e

    # ── CRUD ─────────────────────────────────────────────────────

    def enroll(
        self,
        agent_id: str,
        handle: str,
        issuer: str,
        account_name: str,
        secret: str,
        algorithm: str = "sha1",
        digits: int = 6,
        period: int = 30,
    ) -> StoredTOTP:
        """Upsert one enrollment for (agent_id, handle).

        The base32 secret is normalized/validated, then stored
        **encrypted** — the plaintext never touches the disk.
        """
        conn, fernet = self._require()
        if not agent_id or not agent_id.strip():
            raise TOTPStoreError("agent_id is required")
        if not handle or not handle.strip():
            raise TOTPStoreError("handle is required")
        normalized = validate_secret(secret)
        validate_params(algorithm, digits, period)
        now = datetime.now(timezone.utc).isoformat()
        secret_enc = self._encrypt(fernet, normalized)
        conn.execute(
            """
            INSERT INTO totp_secrets
                (agent_id, handle, issuer, account_name, secret_enc,
                 algorithm, digits, period, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, handle) DO UPDATE SET
                issuer = excluded.issuer,
                account_name = excluded.account_name,
                secret_enc = excluded.secret_enc,
                algorithm = excluded.algorithm,
                digits = excluded.digits,
                period = excluded.period
            """,
            (
                agent_id.strip(),
                handle.strip(),
                issuer or "",
                account_name or "",
                secret_enc,
                (algorithm or "sha1").lower(),
                digits,
                period,
                now,
            ),
        )
        conn.commit()
        return StoredTOTP(
            agent_id=agent_id.strip(),
            handle=handle.strip(),
            issuer=issuer or "",
            account_name=account_name or "",
            secret=normalized,  # in-memory only, caller holds it transiently
            algorithm=(algorithm or "sha1").lower(),
            digits=digits,
            period=period,
            created_at=now,
        )

    def get_secret(self, agent_id: str, handle: str) -> Optional[StoredTOTP]:
        """Resolve ONE of this agent's enrollments, decrypted.

        Account-scoped by the (agent_id, handle) key: another agent's
        handle simply does not exist under this agent_id, so it reads as
        None (indistinguishable from absent). The decrypted secret exists
        ONLY inside this return value — on the compute-a-code path; never
        serialize it into a response.
        """
        conn, fernet = self._require()
        if not agent_id or not handle:
            return None
        row = conn.execute(
            "SELECT agent_id, handle, issuer, account_name, secret_enc,"
            " algorithm, digits, period, created_at"
            " FROM totp_secrets WHERE agent_id = ? AND handle = ?",
            (agent_id.strip(), handle.strip()),
        ).fetchone()
        if row is None:
            return None
        return StoredTOTP(
            agent_id=row[0],
            handle=row[1],
            issuer=row[2],
            account_name=row[3],
            secret=self._decrypt(fernet, row[4]),
            algorithm=row[5],
            digits=row[6],
            period=row[7],
            created_at=row[8],
        )

    def list(self, agent_id: str) -> list[dict]:
        """List this agent's enrollments — **metadata only**, never secrets."""
        conn, _ = self._require()
        rows = conn.execute(
            "SELECT handle, issuer, account_name, algorithm, digits, period,"
            " created_at"
            " FROM totp_secrets WHERE agent_id = ?"
            " ORDER BY created_at, handle",
            (agent_id.strip() if agent_id else "",),
        ).fetchall()
        return [
            {
                "handle": r[0],
                "issuer": r[1],
                "account_name": r[2],
                "algorithm": r[3],
                "digits": r[4],
                "period": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    def remove(self, agent_id: str, handle: str) -> bool:
        """Delete ONE of this agent's enrollments. Returns whether a row went."""
        conn, _ = self._require()
        if not agent_id or not handle:
            return False
        cur = conn.execute(
            "DELETE FROM totp_secrets WHERE agent_id = ? AND handle = ?",
            (agent_id.strip(), handle.strip()),
        )
        conn.commit()
        return cur.rowcount > 0