"""MP-8: server-side TOTP vault + MCP tools (issue #11 workstream 3).

Covers the DONE gate:
- enroll a known otpauth:// URI (fixed secret), then get_totp_code
  matches ``pyotp.TOTP(secret).at(<fixed timestamp>)`` — deterministic,
  no clock dependence (time.time is patched).
- get_totp_code's valid_seconds is sane (0 < valid <= period).
- list_totp / remove_totp work.
- account-scoping: an agent enrolled as A cannot read/remove B's handle
  (refused), a missing handle -> clean not-found.
- the secret never appears in ANY response (enroll/code/list/remove and
  their error paths), and never in the DB file at rest (encrypted).
"""

import json
import os
import stat as stat_mod
from unittest.mock import patch

import pytest

from parousia.totp.store import TOTPStore, TOTPStoreError
from parousia.totp.tools import (
    ALL_TOTP_SCHEMAS,
    TotpHandlers,
    parse_otpauth_uri,
    slugify_handle,
)

import asyncio

# RFC 6238 test vector material — 160 bits (pyotp >= 2.9 enforces a
# 112-bit minimum, so the 80-bit classic "JBSWY3DPEHPK3PXP" is rejected).
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
SECRET2 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJR"
# A fixed timestamp for deterministic code checks.
FIXED_NOW = 1_700_000_000.0

# A full, realistic URI (issuer in the label, params carried).
URI = (
    "otpauth://totp/Example%20Corp:alice@example.com"
    f"?secret={SECRET}&issuer=Example+Corp&algorithm=SHA1&digits=6&period=30"
)


@pytest.fixture
def store(tmp_path):
    s = TOTPStore(str(tmp_path / "totp.db"), str(tmp_path / "totp.key"))
    s.connect()
    yield s
    s.close()


@pytest.fixture
def handlers(store):
    return TotpHandlers(store)


def _dispatch(h, name, **arguments):
    return json.loads(asyncio.run(h.dispatch(name, arguments, "agent-a")))


def _dispatch_as(h, agent_id, name, **arguments):
    return json.loads(asyncio.run(h.dispatch(name, arguments, agent_id)))


# ── Store ─────────────────────────────────────────────────────────


class TestTOTPStore:
    def test_enroll_roundtrip(self, store):
        enrolled = store.enroll(
            "agent-a", "example:alice", "Example Corp",
            "alice@example.com", SECRET, digits=6, period=30,
        )
        assert enrolled.secret == SECRET
        entry = store.get_secret("agent-a", "example:alice")
        assert entry is not None
        assert entry.secret == SECRET
        assert entry.issuer == "Example Corp"
        assert entry.algorithm == "sha1"

    def test_secret_encrypted_at_rest(self, store, tmp_path):
        """The DB file must not contain the plaintext base32 secret."""
        store.enroll("agent-a", "h", "i", "a", SECRET)
        store.close()  # flush WAL
        raw = b""
        for name in ("totp.db", "totp.db-wal", "totp.db-shm"):
            p = tmp_path / name
            if p.exists():
                raw += p.read_bytes()
        assert SECRET.encode() not in raw, "plaintext secret on disk"
        assert SECRET.lower().encode() not in raw

    def test_key_file_is_0600(self, tmp_path):
        key = tmp_path / "k.key"
        s = TOTPStore(str(tmp_path / "t.db"), str(key))
        s.connect()
        s.close()
        assert stat_mod.S_IMODE(os.stat(key).st_mode) == 0o600

    def test_account_scoped_by_key(self, store):
        """(agent_id, handle) is the key — B's handle under A reads None."""
        store.enroll("agent-b", "shared", "i", "acct", SECRET)
        assert store.get_secret("agent-a", "shared") is None
        assert store.get_secret("agent-b", "shared") is not None

    def test_list_is_metadata_only(self, store):
        store.enroll("agent-a", "h1", "Example Corp", "alice", SECRET)
        entries = store.list("agent-a")
        assert entries == [
            {
                "handle": "h1",
                "issuer": "Example Corp",
                "account_name": "alice",
                "algorithm": "sha1",
                "digits": 6,
                "period": 30,
                "created_at": entries[0]["created_at"],
            }
        ]
        assert "secret" not in json.dumps(entries)
        assert SECRET not in json.dumps(entries)

    def test_remove_is_account_scoped(self, store):
        store.enroll("agent-b", "h", "i", "a", SECRET)
        # A cannot remove B's enrollment — not under agent-a.
        assert store.remove("agent-a", "h") is False
        assert store.get_secret("agent-b", "h") is not None
        # B can.
        assert store.remove("agent-b", "h") is True
        assert store.get_secret("agent-b", "h") is None

    def test_missing_handle_is_none(self, store):
        assert store.get_secret("agent-a", "nope") is None

    def test_upsert_replaces(self, store):
        store.enroll("agent-a", "h", "Old Issuer", "acct", SECRET)
        store.enroll("agent-a", "h", "New Issuer", "acct", SECRET2)
        entry = store.get_secret("agent-a", "h")
        assert entry.issuer == "New Issuer"
        assert entry.secret == SECRET2
        # One row, not two.
        assert len(store.list("agent-a")) == 1

    def test_empty_key_file_fails_closed(self, tmp_path):
        key = tmp_path / "empty.key"
        key.write_bytes(b"")
        s = TOTPStore(str(tmp_path / "t.db"), str(key))
        with pytest.raises(TOTPStoreError):
            s.connect()

    def test_garbage_key_file_fails_closed(self, tmp_path):
        key = tmp_path / "garbage.key"
        key.write_bytes(b"not-a-fernet-key-at-all")
        s = TOTPStore(str(tmp_path / "t.db"), str(key))
        with pytest.raises(TOTPStoreError):
            s.connect()


# ── URI parsing ──────────────────────────────────────────────────


class TestParseOtpauthUri:
    def test_full_uri(self):
        parsed = parse_otpauth_uri(URI)
        assert parsed["secret"] == SECRET
        assert parsed["issuer"] == "Example Corp"
        assert parsed["account_name"] == "alice@example.com"
        assert parsed["algorithm"] == "sha1"
        assert parsed["digits"] == 6
        assert parsed["period"] == 30
        assert parsed["handle"] == "example-corp:alice-example-com"

    def test_defaults(self):
        parsed = parse_otpauth_uri(f"otpauth://totp/alice?secret={SECRET}")
        assert parsed["algorithm"] == "sha1"
        assert parsed["digits"] == 6
        assert parsed["period"] == 30
        assert parsed["issuer"] == "alice"

    def test_bare_label_uses_query_issuer(self):
        parsed = parse_otpauth_uri(
            f"otpauth://totp/alice?secret={SECRET}&issuer=Example"
        )
        assert parsed["issuer"] == "Example"
        assert parsed["account_name"] == "alice"

    def test_label_issuer_wins_over_query(self):
        parsed = parse_otpauth_uri(
            "otpauth://totp/Label%20Issuer:acct"
            f"?secret={SECRET}&issuer=Query+Issuer"
        )
        assert parsed["issuer"] == "Label Issuer"
        assert parsed["account_name"] == "acct"

    def test_not_otpauth_rejected(self):
        with pytest.raises(TOTPStoreError):
            parse_otpauth_uri("https://example.com/secret=abc")

    def test_hotp_rejected(self):
        with pytest.raises(TOTPStoreError):
            parse_otpauth_uri(f"otpauth://hotp/alice?secret={SECRET}")

    def test_bad_secret_rejected(self):
        with pytest.raises(TOTPStoreError):
            parse_otpauth_uri("otpauth://totp/alice?secret=!!!not-base32!!!")

    def test_empty_secret_rejected(self):
        with pytest.raises(TOTPStoreError):
            parse_otpauth_uri("otpauth://totp/alice")

    def test_bad_digits_rejected(self):
        with pytest.raises(TOTPStoreError):
            parse_otpauth_uri(
                f"otpauth://totp/alice?secret={SECRET}&digits=five"
            )

    def test_bad_algorithm_rejected(self):
        with pytest.raises(TOTPStoreError):
            parse_otpauth_uri(
                f"otpauth://totp/alice?secret={SECRET}&algorithm=md5"
            )

    def test_secret_whitespace_normalized(self):
        parsed = parse_otpauth_uri(
            f"otpauth://totp/alice?secret={SECRET[:4]}%20{SECRET[4:]}"
        )
        assert parsed["secret"] == SECRET


def test_slugify_handle():
    assert slugify_handle("Example Corp", "alice@example.com") == \
        "example-corp:alice-example-com"
    assert slugify_handle("", "") == "totp"
    assert slugify_handle("Only Issuer", "") == "only-issuer"


# ── Handlers (account-scoped) ────────────────────────────────────


class TestHandlers:
    def test_enroll_then_code_deterministic(self, handlers):
        """enroll a known URI, then get_totp_code matches
        pyotp.TOTP(secret).at(<fixed timestamp>) — no clock dependence."""
        import pyotp

        enrolled = _dispatch(handlers, "enroll_totp", otpauth_uri=URI)
        assert enrolled["enrolled"] is True
        assert enrolled["handle"] == "example-corp:alice-example-com"
        assert enrolled["algorithm"] == "sha1"

        with patch("time.time", return_value=FIXED_NOW):
            result = _dispatch(handlers, "get_totp_code", handle=enrolled["handle"])
        expected = pyotp.TOTP(SECRET).at(FIXED_NOW)
        assert result["code"] == expected, (result, expected)
        assert result["digits"] == 6

    def test_valid_seconds_sane(self, handlers):
        _dispatch(handlers, "enroll_totp", otpauth_uri=URI)
        handle = "example-corp:alice-example-com"
        for now in (FIXED_NOW, FIXED_NOW + 7, FIXED_NOW + 29.5, FIXED_NOW + 30):
            with patch("time.time", return_value=now):
                result = _dispatch(handlers, "get_totp_code", handle=handle)
            assert 0 < result["valid_seconds"] <= 30, result
            assert result["valid_seconds"] == int(30 - (now % 30))

    def test_sha256_and_8_digits(self, handlers):
        import pyotp

        uri = (
            "otpauth://totp/Vault:ops?secret="
            + SECRET2
            + "&algorithm=SHA256&digits=8&period=60"
        )
        enrolled = _dispatch(handlers, "enroll_totp", otpauth_uri=uri)
        assert enrolled["algorithm"] == "sha256"
        assert enrolled["digits"] == 8
        assert enrolled["period"] == 60
        with patch("time.time", return_value=FIXED_NOW):
            result = _dispatch(handlers, "get_totp_code", handle=enrolled["handle"])
        import hashlib
        expected = pyotp.TOTP(
            SECRET2,
            digits=8,
            interval=60,
            digest=hashlib.sha256,
        ).at(FIXED_NOW)
        assert result["code"] == expected

    def test_cross_agent_handle_refused(self, handlers):
        """An agent enrolled as A cannot read B's handle: refused (clean
        not-found — the store keys by agent_id, so B's handle under A is
        simply absent)."""
        # Enroll as agent-B.
        _dispatch_as(
            handlers, "agent-b", "enroll_totp",
            otpauth_uri=f"otpauth://totp/Other:bob?secret={SECRET}",
        )
        # agent-a asks for agent-b's handle → not enrolled for agent-a.
        result = _dispatch(handlers, "get_totp_code", handle="other:bob")
        assert "error" in result
        assert "no TOTP enrollment" in result["error"]
        # And cannot remove it either.
        result = _dispatch(handlers, "remove_totp", handle="other:bob")
        assert result["removed"] is False
        # agent-b's enrollment survives both of agent-a's attempts.
        entry = handlers.store.get_secret("agent-b", "other:bob")
        assert entry is not None
        # And agent-b can still use it.
        import pyotp
        with patch("time.time", return_value=FIXED_NOW):
            own = _dispatch_as(handlers, "agent-b", "get_totp_code", handle="other:bob")
        assert own["code"] == pyotp.TOTP(SECRET).at(FIXED_NOW)

    def test_missing_handle_clean_not_found(self, handlers):
        result = _dispatch(handlers, "get_totp_code", handle="ghost")
        assert result == {"error": "no TOTP enrollment for handle ghost"}
        result = _dispatch(handlers, "remove_totp", handle="ghost")
        assert result["removed"] is False

    def test_list_and_remove(self, handlers):
        _dispatch(handlers, "enroll_totp", otpauth_uri=URI)
        listed = _dispatch(handlers, "list_totp")
        assert listed["count"] == 1
        assert listed["entries"][0]["handle"] == "example-corp:alice-example-com"
        assert "secret" not in json.dumps(listed)

        removed = _dispatch(handlers, "remove_totp", handle="example-corp:alice-example-com")
        assert removed["removed"] is True
        assert _dispatch(handlers, "list_totp")["count"] == 0

    def test_secret_never_in_any_response(self, handlers):
        """The DONE gate's core invariant: the base32 secret appears in NO
        response — enroll, code, list, remove, and their error paths."""
        import re

        enrolled = _dispatch(handlers, "enroll_totp", otpauth_uri=URI)
        code = _dispatch(handlers, "get_totp_code", handle=enrolled["handle"])
        listed = _dispatch(handlers, "list_totp")
        removed = _dispatch(handlers, "remove_totp", handle=enrolled["handle"])
        missing = _dispatch(handlers, "get_totp_code", handle="none")

        blob = json.dumps([enrolled, code, listed, removed, missing])
        assert SECRET not in blob
        assert SECRET.lower() not in blob
        # Not even as a fragment of any base32-looking run.
        base32_run = re.compile(r"[A-Z2-7]{8,}")
        for match in base32_run.findall(blob):
            assert match not in SECRET or len(match) < len(SECRET)

    def test_no_store_refused_not_crashed(self):
        h = TotpHandlers(None)
        result = _dispatch(h, "list_totp")
        assert result == {"error": "TOTP store unavailable"}
        result = _dispatch(
            h, "enroll_totp", otpauth_uri=URI
        )
        assert result == {"error": "TOTP store unavailable"}

    def test_enroll_replaces_same_handle(self, handlers):
        _dispatch(handlers, "enroll_totp", otpauth_uri=URI)
        # Re-enroll with a different URI for the same issuer/account →
        # replaced, not duplicated.
        other_uri = URI.replace(SECRET, SECRET2)
        enrolled = _dispatch(handlers, "enroll_totp", otpauth_uri=other_uri)
        assert enrolled["enrolled"] is True
        assert _dispatch(handlers, "list_totp")["count"] == 1
        entry = handlers.store.get_secret("agent-a", enrolled["handle"])
        assert entry.secret == SECRET2

    def test_explicit_handle_override(self, handlers):
        enrolled = _dispatch(
            handlers, "enroll_totp", otpauth_uri=URI, handle="my-handle"
        )
        assert enrolled["handle"] == "my-handle"
        assert handlers.store.get_secret("agent-a", "my-handle") is not None

    def test_bad_uri_clean_error(self, handlers):
        result = _dispatch(handlers, "enroll_totp", otpauth_uri="garbage")
        assert "error" in result
        result = _dispatch(handlers, "enroll_totp")
        assert result == {"error": "otpauth_uri is required"}


# ── Registry ──────────────────────────────────────────────────────


class TestRegistry:
    def test_four_tools_listed(self):
        names = {s["name"] for s in ALL_TOTP_SCHEMAS}
        assert names == {"enroll_totp", "get_totp_code", "list_totp", "remove_totp"}

    def test_schemas_required_fields(self):
        by_name = {s["name"]: s for s in ALL_TOTP_SCHEMAS}
        assert by_name["enroll_totp"]["inputSchema"]["required"] == ["otpauth_uri"]
        assert by_name["get_totp_code"]["inputSchema"]["required"] == ["handle"]
        assert by_name["remove_totp"]["inputSchema"]["required"] == ["handle"]


# ── MCP wiring: dispatch through the REAL server ─────────────────


class TestMcpWiring:
    def _build(self, monkeypatch, store, tmp_path):
        import fakeredis

        import parousia.guard.mcp_server as mcp_mod
        import parousia.temporal.db as tdb
        from parousia.config import ParousiaConfig

        monkeypatch.setattr(
            mcp_mod, "load_config",
            lambda: ParousiaConfig(domain="agents.test.com", totp_store={
                "db_path": str(tmp_path / "t.db"),
                "key_path": str(tmp_path / "t.key"),
            }),
        )
        monkeypatch.setattr(
            "parousia.guard.mcp_server.redis_lib.Redis",
            lambda **kw: fakeredis.FakeRedis(),
        )

        def fake_temporal(*a, **kw):
            db = tdb.TemporalDB(db_path=":memory:")
            db.connect()
            db.create_tables()
            return db

        monkeypatch.setattr(mcp_mod, "TemporalDB", fake_temporal)
        # Inject OUR store into _build_server (the real config paths point
        # at /var/lib; tests own this tmp store instead).
        monkeypatch.setattr(mcp_mod, "TOTPStore", lambda db, key: store)
        # Inbox: tmp file.
        from parousia.inbox.inbox_store import InboxStore
        monkeypatch.setattr(
            "parousia.inbox.inbox_store.InboxStore",
            lambda *a, **kw: InboxStore(str(tmp_path / "inbox.db")),
        )
        from unittest.mock import MagicMock
        monkeypatch.setattr(mcp_mod, "MemoryRecorder", MagicMock)
        return mcp_mod._build_server()

    def _call(self, server, name, arguments):
        import mcp.types as types

        async def run():
            handler = server.request_handlers[types.CallToolRequest]
            result = await handler(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(name=name, arguments=arguments)
                )
            )
            return json.loads(result.root.content[0].text)

        return asyncio.run(run())

    def test_tools_listed_and_dispatch(self, tmp_path, monkeypatch):
        store = TOTPStore(str(tmp_path / "t.db"), str(tmp_path / "t.key"))
        store.connect()
        try:
            server, _ = self._build(monkeypatch, store, tmp_path)

            import mcp.types as types

            async def list_tools():
                handler = server.request_handlers[types.ListToolsRequest]
                listed = await handler(types.ListToolsRequest(params=None))
                return {t.name for t in listed.root.tools}

            names = asyncio.run(list_tools())
            assert {"enroll_totp", "get_totp_code", "list_totp", "remove_totp"} <= names

            # stdio/no-auth path: agent_id falls to "default".
            enrolled = self._call(server, "enroll_totp", {"otpauth_uri": URI})
            assert enrolled["enrolled"] is True
            handle = enrolled["handle"]

            listed = self._call(server, "list_totp", {})
            assert listed["count"] == 1

            import pyotp
            with patch("time.time", return_value=FIXED_NOW):
                code = self._call(server, "get_totp_code", {"handle": handle})
            assert code["code"] == pyotp.TOTP(SECRET).at(FIXED_NOW)

            removed = self._call(server, "remove_totp", {"handle": handle})
            assert removed["removed"] is True
            assert self._call(server, "list_totp", {})["count"] == 0
        finally:
            store.close()