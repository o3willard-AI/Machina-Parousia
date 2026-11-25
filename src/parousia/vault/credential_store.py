"""Server-side credential store for the Parousia browser (MP-3, GitHub issue #17).

A small encrypted-at-rest store mapping ``origin → {handle, username,
password}`` for browser credential fills.

Design invariants (tested):

- **Admin/human-populated only.** Writes happen through the
  ``parousia-guard vault add`` CLI (or an admin REST call) — never through any
  agent-facing surface. The MCP surface only ever *reads* through
  ``fill_credentials``, which returns a fill summary, never the secret.
- **Encrypted at rest.** SQLite rows hold Fernet ciphertext for the username
  and the password; the key lives in a 0600 file beside the database
  (auto-generated on first use). Plaintext never touches the disk.
- **Origin-bound.** Credentials are keyed ``(origin, handle)``; the fill tool
  refuses to fill a page whose current origin does not match the stored
  origin, so a handle saved for site-a can never fill a page on site-b.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from cryptography.fernet import Fernet, InvalidToken


class CredentialStoreError(Exception):
    """Raised when the credential store cannot be opened or its key material
    is missing/unreadable (never to echo secret values)."""


class StoredCredential(NamedTuple):
    """One decrypted credential (server-side only — never serialized to an
    agent-facing response)."""

    origin: str
    handle: str
    username: str
    password: str


def normalize_origin(url_or_origin: str) -> str:
    """Normalize a URL or bare origin to ``scheme://host[:port]``.

    The credential key is the *origin* (scheme + host + optional port), not
    the full URL, so ``https://site.example/login`` matches a credential
    saved for ``https://site.example``. Paths, queries, and fragments are
    dropped; scheme and host are lowercased.
    """
    from urllib.parse import urlsplit

    raw = (url_or_origin or "").strip()
    if not raw:
        return ""
    # Bare host (no scheme)? Treat as https, the dominant case.
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = parts.port  # None when absent in the URL
    netloc = host if port in (None, 80, 443) and scheme in ("http", "https") else f"{host}:{port}"
    return f"{scheme}://{netloc}"


class CredentialStore:
    """SQLite + Fernet credential store for browser fills."""

    KEY_MODE = 0o600
    DB_MODE = 0o600

    def __init__(self, db_path: str, key_path: str):
        self.db_path = Path(db_path)
        self.key_path = Path(key_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._fernet: Optional[Fernet] = None

    # ── open/close ───────────────────────────────────────────────

    def connect(self) -> "CredentialStore":
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
            CREATE TABLE IF NOT EXISTS credentials (
                origin TEXT NOT NULL,
                handle TEXT NOT NULL,
                username_enc BLOB NOT NULL,
                password_enc BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (origin, handle)
            )
            """
        )
        self._conn.commit()
        if is_new or not self.db_path.exists():
            # Databases may pre-exist from a previous run; only chmod what we
            # created. Existing files keep their (operator-set) permissions.
            try:
                if is_new:
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
            raise CredentialStoreError("credential store is not connected")
        return self._conn, self._fernet

    # ── key management ───────────────────────────────────────────

    def _load_or_create_key(self) -> bytes:
        """Load (or generate + persist, 0600) the store's Fernet key."""
        if self.key_path.exists():
            data = self.key_path.read_bytes()
            if not data:
                raise CredentialStoreError("credential store key file is empty")
            try:
                Fernet(data)
            except Exception as e:
                # Invalid/rotated key material — fail closed, never serve
                # garbage or the wrong plaintext.
                raise CredentialStoreError(
                    "credential store key is invalid (rotated or corrupt)"
                ) from e
            return data
        parent = self.key_path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # Write 0600 from the start: write to a temp file and chmod before
        # rename, so the key never exists group/world-readable.
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
            raise CredentialStoreError("stored credential failed to decrypt") from e

    # ── CRUD ─────────────────────────────────────────────────────

    def add(self, origin: str, handle: str, username: str, password: str) -> StoredCredential:
        """Upsert a credential for (origin, handle). Admin CLI use only."""
        conn, fernet = self._require()
        norm = normalize_origin(origin)
        if not norm:
            raise CredentialStoreError("origin must be a URL or scheme://host")
        if not handle or not handle.strip():
            raise CredentialStoreError("handle is required")
        now = datetime.now(timezone.utc).isoformat()
        username_enc = self._encrypt(fernet, username)
        password_enc = self._encrypt(fernet, password)
        conn.execute(
            """
            INSERT INTO credentials (origin, handle, username_enc, password_enc,
                                     created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin, handle) DO UPDATE SET
                username_enc = excluded.username_enc,
                password_enc = excluded.password_enc,
                updated_at = excluded.updated_at
            """,
            (norm, handle.strip(), username_enc, password_enc, now, now),
        )
        conn.commit()
        return StoredCredential(norm, handle.strip(), username, password)

    def get(self, origin: str, handle: str) -> Optional[StoredCredential]:
        """Resolve a credential for (origin, handle). None when absent."""
        conn, fernet = self._require()
        norm = normalize_origin(origin)
        if not norm or not handle:
            return None
        row = conn.execute(
            "SELECT origin, handle, username_enc, password_enc FROM credentials "
            "WHERE origin = ? AND handle = ?",
            (norm, handle.strip()),
        ).fetchone()
        if row is None:
            return None
        return StoredCredential(
            origin=row[0],
            handle=row[1],
            username=self._decrypt(fernet, row[2]),
            password=self._decrypt(fernet, row[3]),
        )

    def delete(self, origin: str, handle: str) -> bool:
        """Delete a credential. Returns whether a row was removed."""
        conn, _ = self._require()
        cur = conn.execute(
            "DELETE FROM credentials WHERE origin = ? AND handle = ?",
            (normalize_origin(origin), handle.strip() if handle else handle),
        )
        conn.commit()
        return cur.rowcount > 0

    def list(self) -> list[dict]:
        """List stored credentials — metadata only (origin, handle, updated),
        never usernames or passwords."""
        conn, _ = self._require()
        rows = conn.execute(
            "SELECT origin, handle, created_at, updated_at FROM credentials "
            "ORDER BY origin, handle"
        ).fetchall()
        return [
            {"origin": r[0], "handle": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]