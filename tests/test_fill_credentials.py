"""MP-3 (part B): the fill_credentials MCP tool — tests (issue #17).

Invariants under test (from the wi-MP-3 brief):
- fills are origin-bound (a handle for site-a refuses a fill on site-b),
- the agent NEVER sees the secret value (summary only),
- fills only happen through the server-side store (never an argument),
- credential fills are never recorded to the memory layer.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from parousia.config import ParousiaConfig
from parousia.memory.recorder import MemoryRecorder, _FACT_FORMATTERS
from parousia.memory.config import Mem0Config
from parousia.spatial.tools import (
    ALL_SPATIAL_SCHEMAS,
    SpatialToolHandlers,
    fill_credentials_schema,
)
from parousia.vault.credential_store import CredentialStore


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = CredentialStore(
        str(tmp_path / "creds.db"), str(tmp_path / "creds.key")
    )
    s.connect()
    yield s
    s.close()


def _page(url: str = "https://site.example/login", visible: bool = True):
    """A mocked Playwright page.

    Locators are memoized per selector (page.locator(sel) returns the SAME
    mock every time), so tests can assert on the exact locator objects the
    handler filled.
    """
    page = MagicMock()
    page.url = url
    locators: dict = {}

    def locator(selector):
        if selector not in locators:
            loc = MagicMock()
            loc.first = loc  # .first is the same mock
            loc.is_visible = AsyncMock(return_value=visible)
            loc.fill = AsyncMock(return_value=None)
            loc.click = AsyncMock(return_value=None)
            loc.selector = selector
            locators[selector] = loc
        return locators[selector]

    page.locator = MagicMock(side_effect=locator)
    page._locators = locators
    return page


def _handlers(page, credential_store):
    """Real handlers over a mocked browser page + real (tmp) store."""
    pool = MagicMock()
    pool.get_browser = AsyncMock(return_value=MagicMock(page=page))
    pool.proxy_for = MagicMock(return_value="")
    serializer = MagicMock()
    return SpatialToolHandlers(
        ParousiaConfig(), pool, serializer, credential_store=credential_store
    )


def _fill(handlers, origin, handle, **extra):
    return asyncio.run(
        handlers.dispatch(
            "fill_credentials",
            {"origin": origin, "handle": handle, **extra},
            "agent-x",
        )
    )


# ── Schema ───────────────────────────────────────────────────────


class TestSchema:
    def test_listed_in_all_spatial_schemas(self):
        names = [s["name"] for s in ALL_SPATIAL_SCHEMAS]
        assert "fill_credentials" in names

    def test_schema_shape(self):
        schema = fill_credentials_schema()
        assert schema["name"] == "fill_credentials"
        assert set(schema["inputSchema"]["required"]) == {"origin", "handle"}
        props = schema["inputSchema"]["properties"]
        assert "origin" in props and "handle" in props

    def test_no_secret_argument_in_schema(self):
        """The agent cannot pass a username/password — only a handle."""
        props = fill_credentials_schema()["inputSchema"]["properties"]
        assert set(props) == {"origin", "handle", "submit", "timeout_ms"}
        for banned in ("password", "username", "secret", "credential"):
            assert banned not in props


# ── Origin-bound fill ─────────────────────────────────────────────


class TestOriginBoundFill:
    def test_matching_origin_fills_both_fields(self, store):
        store.add("https://site.example", "work", "alice@example.com", "hunter2!")
        page = _page("https://site.example/login")
        handlers = _handlers(page, store)

        result = json.loads(_fill(handlers, "https://site.example", "work"))

        assert result["filled"] is True, result
        assert result["fields_filled"] == ["username", "password"]
        assert result["origin"] == "https://site.example"
        # The locators were filled with the STORE's credential values.
        cred = store.get("https://site.example", "work")
        user_fill = _first_user_fill(page)
        pwd_fill = _password_fill(page)
        assert user_fill is not None, "no username locator filled"
        user_fill.fill.assert_awaited_once()
        assert user_fill.fill.call_args.args[0] == cred.username
        pwd_fill.fill.assert_awaited_once()
        assert pwd_fill.fill.call_args.args[0] == cred.password

    def test_cross_origin_refused(self, store):
        """A handle saved for site-a can never fill a page on site-b."""
        store.add("https://site-a.example", "work", "user-a", "pass-a")
        page = _page("https://site-b.example/login")  # agent is on site-b
        handlers = _handlers(page, store)

        result = json.loads(
            _fill(handlers, "https://site-a.example", "work")
        )
        assert "error" in result
        assert "refusing to fill" in result["error"]
        assert result.get("origin_bound") is True
        # NOTHING was filled — no locator was even created by the handler.
        assert page._locators == {}, page._locators
        assert not _any_fill(page)

    def test_url_origin_forms_normalized(self, store):
        """Page on https://Site.example/ deep path matches stored bare origin."""
        store.add("https://portal.example", "main", "u@x", "p")
        page = _page("https://portal.example/auth/login?next=/home")
        handlers = _handlers(page, store)
        result = json.loads(_fill(handlers, "https://portal.example", "main"))
        assert result["filled"] is True

    def test_unknown_handle_refused(self, store):
        store.add("https://site.example", "other", "u", "p")
        page = _page("https://site.example/login")
        handlers = _handlers(page, store)
        result = json.loads(_fill(handlers, "https://site.example", "missing"))
        assert "error" in result
        assert "no credential stored" in result["error"]
        assert not _any_fill(page)

    def test_no_store_refused(self, store):
        """No store wired → explicit refusal, never a fallback."""
        page = _page("https://site.example/login")
        handlers = _handlers(page, None)
        result = json.loads(_fill(handlers, "https://site.example", "work"))
        assert result == {"error": "credential store unavailable"}

    def test_no_password_field_refused(self, store):
        """A page with no visible login form never gets the secret typed."""
        store.add("https://site.example", "work", "u", "p")
        # All locators invisible → no username, no password field.
        page = _page("https://site.example/landing", visible=False)
        handlers = _handlers(page, store)
        result = json.loads(_fill(handlers, "https://site.example", "work"))
        assert "error" in result
        assert "password field" in result["error"]
        assert not _any_fill(page)

    def test_optional_submit(self, store):
        store.add("https://site.example", "work", "u", "p")
        page = _page("https://site.example/login")
        handlers = _handlers(page, store)
        result = json.loads(
            _fill(handlers, "https://site.example", "work", submit=True)
        )
        assert result["filled"] is True
        assert result["submitted"] is True
        # A submit button was clicked.
        clicked = [
            sel
            for sel, loc in page._locators.items()
            if loc.click.await_count
        ]
        assert any("submit" in sel for sel in clicked), clicked


# ── The agent never sees the secret ───────────────────────────────


class TestSecretNeverLeaks:
    def test_result_json_has_no_secret(self, store):
        secret_user = "alice-very-secret@example.com"
        secret_pass = "P@ssw0rd-Seekrit-42"
        store.add("https://site.example", "work", secret_user, secret_pass)
        page = _page("https://site.example/login")
        handlers = _handlers(page, store)

        raw = _fill(handlers, "https://site.example", "work")

        assert secret_user not in raw
        assert secret_pass not in raw
        parsed = json.loads(raw)
        assert "username" not in parsed
        assert "password" not in parsed
        assert set(parsed) == {
            "filled",
            "origin",
            "handle",
            "fields_filled",
            "submitted",
        }

    def test_error_path_has_no_secret(self, store):
        """Failures echo origins/handles, never credential values."""
        secret_pass = "S3cret-Error-Path"
        store.add("https://site.example", "work", "u", secret_pass)
        # Simulate a fill failure mid-way: the password locator raises.
        # (Touch the locator first so it exists in the page's memo.) 
        page = _page("https://site.example/login")
        page.locator("input[type='email']")
        page.locator("input[type='password']")
        pwd = _password_fill(page)
        pwd.fill = AsyncMock(side_effect=RuntimeError("frame detached"))

        handlers = _handlers(page, store)
        raw = _fill(handlers, "https://site.example", "work")
        assert secret_pass not in raw


# ── Memory layer never records fills ──────────────────────────────


class TestMemoryNeverRecordsFills:
    def test_no_formatter_registered(self):
        assert "fill_credentials" not in _FACT_FORMATTERS

    def test_record_tool_call_skips_fill_credentials(self):
        """Even with a formatter accidentally registered, the explicit
        never-record guard skips the fill."""
        recorder = MemoryRecorder(Mem0Config())
        mock_memory = MagicMock()
        recorder._get_memory = lambda: mock_memory

        # Sabotage: register a formatter for fill_credentials.
        from parousia.memory import recorder as recorder_mod

        recorder_mod._FACT_FORMATTERS["fill_credentials"] = (
            lambda args, result, agent_id: "leaked fill fact"
        )
        try:
            recorder.record_tool_call(
                "fill_credentials",
                {"origin": "https://x", "handle": "h"},
                {"filled": True},
                "agent",
            )
            if recorder._sync_thread:
                recorder._sync_thread.join(timeout=2.0)
        finally:
            recorder_mod._FACT_FORMATTERS.pop("fill_credentials", None)

        # The never-record guard held: nothing reached Mem0.
        mock_memory.add.assert_not_called()

    def test_record_tool_call_skips_fill_without_sabotage(self):
        """Normal path (no formatter): also no Mem0 call."""
        recorder = MemoryRecorder(Mem0Config())
        mock_memory = MagicMock()
        recorder._get_memory = lambda: mock_memory
        recorder.record_tool_call(
            "fill_credentials", {"origin": "https://x", "handle": "h"}, {}, "agent"
        )
        if recorder._sync_thread:
            recorder._sync_thread.join(timeout=2.0)
        mock_memory.add.assert_not_called()


# ── Helpers ──────────────────────────────────────────────────────


def _first_user_fill(page):
    """The first non-password locator that got filled (the username field)."""
    from parousia.spatial.tools import SpatialToolHandlers as _STH

    ordered = list(_STH._USERNAME_SELECTORS)
    for sel in ordered:
        loc = page._locators.get(sel)
        if loc is not None and loc.fill.await_count:
            return loc
    # Fallback: any filled non-password locator.
    for sel, loc in page._locators.items():
        if sel != "input[type='password']" and loc.fill.await_count:
            return loc
    return None


def _password_fill(page):
    return page._locators.get("input[type='password']")


def _any_fill(page):
    return any(loc.fill.await_count for loc in page._locators.values())
