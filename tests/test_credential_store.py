"""MP-3 (part B): server-side credential store — tests.

Invariants under test (from the wi-MP-3 brief):
- encrypted at rest (plaintext never in the DB file),
- admin-populated CRUD (add / get / list / delete),
- list returns metadata only,
- key file is 0600.
"""

import os
import stat

import pytest

from parousia.vault.credential_store import (
    CredentialStore,
    CredentialStoreError,
    StoredCredential,
    normalize_origin,
)


@pytest.fixture
def store(tmp_path):
    s = CredentialStore(
        str(tmp_path / "creds.db"),
        str(tmp_path / "creds.key"),
    )
    s.connect()
    yield s
    s.close()


# ── normalize_origin ─────────────────────────────────────────────


class TestNormalizeOrigin:
    def test_full_url_drops_path_query_fragment(self):
        assert (
            normalize_origin("https://site.example/login?next=/x#frag")
            == "https://site.example"
        )

    def test_case_and_scheme_normalized(self):
        assert normalize_origin("HTTPS://Site.Example/") == "https://site.example"

    def test_bare_host_treated_as_https(self):
        assert normalize_origin("site.example") == "https://site.example"

    def test_explicit_non_default_port_kept(self):
        assert normalize_origin("http://host.example:8080/x") == "http://host.example:8080"

    def test_default_ports_dropped(self):
        assert normalize_origin("https://host.example:443/x") == "https://host.example"
        assert normalize_origin("http://host.example:80/x") == "http://host.example"

    def test_empty_is_empty(self):
        assert normalize_origin("") == ""
        assert normalize_origin("   ") == ""


# ── CRUD roundtrip ───────────────────────────────────────────────


class TestCrud:
    def test_add_get_roundtrip(self, store):
        store.add("https://site.example", "work", "alice@example.com", "hunter2!")
        cred = store.get("https://site.example", "work")
        assert cred == StoredCredential(
            origin="https://site.example",
            handle="work",
            username="alice@example.com",
            password="hunter2!",
        )

    def test_get_is_origin_bound(self, store):
        """A handle saved for site-a never resolves for site-b — the key
        is (origin, handle), not handle alone."""
        store.add("https://site-a.example", "work", "user-a", "pass-a")
        store.add("https://site-b.example", "work", "user-b", "pass-b")
        assert store.get("https://site-a.example", "work").password == "pass-a"
        assert store.get("https://site-b.example", "work").password == "pass-b"
        # An origin with no stored handle → None (never the other site's).
        assert store.get("https://site-c.example", "work") is None

    def test_get_url_form_matches_origin_key(self, store):
        """The tool passes the page's URL — normalization matches the stored
        bare origin."""
        store.add("https://site.example", "h", "u", "p")
        assert store.get("https://site.example/login?id=1", "h").username == "u"

    def test_missing_returns_none(self, store):
        assert store.get("https://nothing.example", "nope") is None
        assert store.get("https://nothing.example", "") is None

    def test_add_upserts_same_origin_handle(self, store):
        store.add("https://site.example", "work", "old-user", "old-pass")
        store.add("https://site.example", "work", "new-user", "new-pass")
        cred = store.get("https://site.example", "work")
        assert (cred.username, cred.password) == ("new-user", "new-pass")
        # Upsert, not a second row.
        assert len([r for r in store.list() if r["handle"] == "work"]) == 1

    def test_add_normalizes_origin(self, store):
        cred = store.add("https://Site.Example/Login", "h", "u", "p")
        assert cred.origin == "https://site.example"
        assert store.get("https://site.example/other", "h") is not None

    def test_add_rejects_empty_handle(self, store):
        with pytest.raises(CredentialStoreError):
            store.add("https://site.example", "  ", "u", "p")

    def test_add_rejects_bad_origin(self, store):
        for bad in ("", "https://", "://"):
            with pytest.raises(CredentialStoreError):
                store.add(bad, "h", "u", "p")

    def test_delete(self, store):
        store.add("https://site.example", "work", "u", "p")
        assert store.delete("https://site.example", "work") is True
        assert store.get("https://site.example", "work") is None
        assert store.delete("https://site.example", "work") is False

    def test_delete_normalizes_origin(self, store):
        store.add("https://site.example", "work", "u", "p")
        assert store.delete("https://site.example/deep/path", "work") is True


# ── Encryption at rest ────────────────────────────────────────────


class TestEncryptionAtRest:
    def test_plaintext_not_in_db_file(self, store, tmp_path):
        """The on-disk database must not contain the plaintext username or
        password — Fernet ciphertext only."""
        secret_user = "alice-very-secret@example.com"
        secret_pass = "P@ssw0rd-Super-Seekrit-42"
        store.add("https://site.example", "work", secret_user, secret_pass)
        store.close()  # flush WAL into the main file view

        raw = b""
        for name in ("creds.db", "creds.db-wal", "creds.db-shm"):
            p = tmp_path / name
            if p.exists():
                raw += p.read_bytes()
        assert secret_user.encode() not in raw, "username plaintext on disk"
        assert secret_pass.encode() not in raw, "password plaintext on disk"

    def test_ciphertext_present_in_db(self, store):
        """The rows DO hold data — it is encrypted, not absent."""
        store.add("https://site.example", "work", "u-ser", "p-ass")
        row = store._conn.execute(
            "SELECT username_enc, password_enc FROM credentials "
            "WHERE origin = ? AND handle = ?",
            ("https://site.example", "work"),
        ).fetchone()
        assert row is not None
        username_enc, password_enc = bytes(row[0]), bytes(row[1])
        assert b"u-ser" not in username_enc
        assert b"p-ass" not in password_enc
        # Fernet tokens are b64(0x80 || ...) — decode and check the version byte.
        import base64 as _b64

        def _unpad(b: bytes) -> bytes:
            return _b64.urlsafe_b64decode(b + b"=" * (-len(b) % 4))

        assert _unpad(username_enc)[:1] == b"\x80"
        assert _unpad(password_enc)[:1] == b"\x80"

    def test_key_file_is_0600(self, tmp_path):
        key_path = tmp_path / "creds.key"
        s = CredentialStore(str(tmp_path / "creds.db"), str(key_path))
        s.connect()
        s.close()
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        assert mode == 0o600, oct(mode)

    def test_wrong_key_fails_closed(self, tmp_path):
        """A rotated key must fail closed — never return garbage or the
        wrong plaintext."""
        from cryptography.fernet import Fernet

        s = CredentialStore(str(tmp_path / "c.db"), str(tmp_path / "c.key"))
        s.connect()
        s.add("https://s.example", "h", "user", "pass")
        s.close()

        # Overwrite the key with a different valid Fernet key
        # (generate_key() already returns the b64-encoded key bytes). The key
        # itself parses fine; the STORED ciphertext no longer matches it, so
        # every read of the old rows must fail closed.
        tmp_path.joinpath("c.key").write_bytes(Fernet.generate_key())
        s2 = CredentialStore(str(tmp_path / "c.db"), str(tmp_path / "c.key"))
        s2.connect()
        with pytest.raises(CredentialStoreError):
            s2.get("https://s.example", "h")
        s2.close()

    def test_empty_key_file_rejected(self, tmp_path):
        key_path = tmp_path / "empty.key"
        key_path.write_bytes(b"")
        s = CredentialStore(str(tmp_path / "c.db"), str(key_path))
        with pytest.raises(CredentialStoreError):
            s.connect()

    def test_garbage_key_file_rejected(self, tmp_path):
        key_path = tmp_path / "garbage.key"
        key_path.write_bytes(b"not-a-valid-fernet-key-at-all")
        s = CredentialStore(str(tmp_path / "c.db"), str(key_path))
        with pytest.raises(CredentialStoreError):
            s.connect()


# ── list (metadata only) ─────────────────────────────────────────


class TestList:
    def test_list_has_no_secret_material(self, store):
        store.add("https://site.example", "work", "secret-user", "secret-pass")
        rows = store.list()
        assert len(rows) == 1
        row = rows[0]
        assert row["origin"] == "https://site.example"
        assert row["handle"] == "work"
        # No field carries the secret values.
        import json

        assert "secret-user" not in json.dumps(rows)
        assert "secret-pass" not in json.dumps(rows)

    def test_list_empty(self, store):
        assert store.list() == []

    def test_unconnected_store_fails_closed(self, tmp_path):
        s = CredentialStore(str(tmp_path / "x.db"), str(tmp_path / "x.key"))
        with pytest.raises(CredentialStoreError):
            s.get("https://x.example", "h")
        with pytest.raises(CredentialStoreError):
            s.add("https://x.example", "h", "u", "p")