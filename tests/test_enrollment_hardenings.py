"""MP-6: enrollment hardenings — tests (issue #19).

Parts:
- A — wait_for_email (browser↔inbox coupling) + link extraction.
- B — failure forensics: block-vendor detection, screenshot + DOM snapshot
  on the browse_to/interact failure paths.
- C — session persistence: cookies survive across two browse_to calls.

Pure tests (always run): link extraction, block-vendor classification,
wait_for_email over a real tmp InboxStore (fast cadence).
Real-browser tests (skipif playwright/chromium unavailable — repo
convention): the forensics + persistence integration against a real
headless Chromium with route-intercepted http://local.test origins.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from parousia.config import ParousiaConfig
from parousia.inbox.inbox_store import InboxMessage, InboxStore
from parousia.inbox.wait_for import (
    DEFAULT_TIMEOUT_MS,
    MAX_TIMEOUT_MS,
    extract_links,
    wait_for_email,
)
from parousia.spatial.tools import (
    SpatialToolHandlers,
    _classify_block_headers,
    _classify_block_text,
)

try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


ORIGIN = "http://local.test"


# ── Fixtures / helpers ───────────────────────────────────────────


def _msg(agent_id: str, subject: str, body: str = "", sender: str = "no-reply@x.example", **kw) -> InboxMessage:
    return InboxMessage(
        id=kw.get("id", f"m-{subject[:8]}"),
        agent_id=agent_id,
        sender=sender,
        recipient=f"{agent_id}@parousia.example",
        subject=subject,
        body_text=body,
        body_html=kw.get("body_html"),
        received_at=kw.get("received_at", datetime.now(timezone.utc).isoformat()),
    )


@pytest.fixture
def inbox(tmp_path):
    store = InboxStore(str(tmp_path / "inbox.db"))
    yield store


async def _fast_sleep(seconds: float) -> None:
    """Real but tiny cadence so the deadline actually advances — caps any
    requested poll interval at 10ms so tests stay fast."""
    await asyncio.sleep(max(0.001, min(seconds if seconds else 0.01, 0.01)))


def _spatial_handlers(page):
    """Real SpatialToolHandlers over a real page via a mocked pool."""
    pool = MagicMock()
    pool.get_browser = AsyncMock(return_value=SimpleNamespace(page=page))
    pool.proxy_for = MagicMock(return_value="")
    serializer = MagicMock()
    sdom = MagicMock()
    sdom.model_dump.return_value = {"url": "u", "status": 200}
    serializer.to_sdom.return_value = sdom
    return SpatialToolHandlers(ParousiaConfig(), pool, serializer)


def _set_content(html: str) -> str:
    return f"<html><body>{html}</body></html>"


# ── Part A: link extraction (pure) ────────────────────────────────


class TestExtractLinks:
    def test_text_body_urls(self):
        body = "Confirm here: https://local.test/confirm?token=abc or http://other.test/x"
        links = extract_links(body)
        assert links == [
            "https://local.test/confirm?token=abc",
            "http://other.test/x",
        ]

    def test_trailing_punctuation_stripped(self):
        links = extract_links("Go to https://local.test/confirm. Thanks!")
        assert links == ["https://local.test/confirm"]

    def test_html_body_links(self):
        body_html = '<a href="https://local.test/optin?k=1">confirm</a> plain mailto ignored'
        links = extract_links("", body_html)
        assert links == ["https://local.test/optin?k=1"]

    def test_dedupe_and_order(self):
        body = "https://local.test/a then https://local.test/b then https://local.test/a again"
        assert extract_links(body) == ["https://local.test/a", "https://local.test/b"]

    def test_text_and_html_merged(self):
        links = extract_links(
            "see https://local.test/from-text",
            '<a href="https://local.test/from-html">x</a>',
        )
        assert links == ["https://local.test/from-text", "https://local.test/from-html"]

    def test_empty(self):
        assert extract_links("") == []
        assert extract_links(None) == []
        assert extract_links("no links here") == []


# ── Part A: wait_for_email (real tmp store, fast cadence) ─────────


class TestWaitForEmail:
    @pytest.mark.asyncio
    async def test_finds_matching_subject_case_insensitive(self, inbox):
        inbox.store_message(_msg("agent-x", "Confirm your Empower signup", "Click https://local.test/c/1"))
        result = await wait_for_email(inbox, "agent-x", "EMPOWER signup", timeout_ms=1000, sleep=_fast_sleep)
        assert result["found"] is True, result
        assert result["message"]["subject"] == "Confirm your Empower signup"
        assert result["links"] == ["https://local.test/c/1"]

    @pytest.mark.asyncio
    async def test_matches_sender_too(self, inbox):
        inbox.store_message(_msg("agent-x", "Daily digest", sender="billing@acme.example"))
        result = await wait_for_email(inbox, "agent-x", "acme", timeout_ms=1000, sleep=_fast_sleep)
        assert result["found"] is True
        assert result["message"]["sender"] == "billing@acme.example"

    @pytest.mark.asyncio
    async def test_account_scoped_no_cross_agent_match(self, inbox):
        """A matching mail stored for ANOTHER agent is invisible — the
        caller's own inbox only (list_messages filters by agent_id)."""
        inbox.store_message(_msg("agent-y", "Confirm your signup", sender="bot@acme.example"))
        result = await wait_for_email(inbox, "agent-x", "confirm", timeout_ms=200, sleep=_fast_sleep)
        assert result == {"found": False}, result

    @pytest.mark.asyncio
    async def test_timeout_returns_found_false(self, inbox):
        inbox.store_message(_msg("agent-x", "unrelated subject"))
        result = await wait_for_email(inbox, "agent-x", "never-arrives", timeout_ms=100, sleep=_fast_sleep)
        assert result == {"found": False}

    @pytest.mark.asyncio
    async def test_empty_inbox_times_out(self, inbox):
        result = await wait_for_email(inbox, "agent-x", "anything", timeout_ms=100, sleep=_fast_sleep)
        assert result == {"found": False}

    @pytest.mark.asyncio
    async def test_empty_pattern_is_an_error(self, inbox):
        result = await wait_for_email(inbox, "agent-x", "", timeout_ms=100, sleep=_fast_sleep)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_later_message_found_after_first_miss(self, inbox):
        """The poll actually loops: the match arrives only after the first
        (empty) check, via a store write from the fast-cadence sleep."""
        inbox.store_message(_msg("agent-x", "noise"))
        task = asyncio.create_task(
            wait_for_email(
                inbox, "agent-x", "late-mail", timeout_ms=5000, sleep=_fast_sleep
            )
        )
        await asyncio.sleep(0.02)
        inbox.store_message(_msg("agent-x", "late-mail subject"))
        result = await asyncio.wait_for(task, timeout=10)
        assert result["found"] is True

    def test_bounds_constants(self):
        assert DEFAULT_TIMEOUT_MS == 30_000
        assert MAX_TIMEOUT_MS == 120_000


# ── Part B: block-vendor classification (pure) ────────────────────


class TestClassifyBlockVendor:
    def test_cloudfront_x_cache_header(self):
        assert _classify_block_headers({"X-Cache": "Error from cloudfront"}) == "cloudfront"

    def test_cloudfront_x_cache_hit_is_not_a_block(self):
        assert _classify_block_headers({"x-cache": "Hit from cloudfront"}) == ""

    def test_cloudflare_cf_ray(self):
        assert _classify_block_headers({"cf-ray": "abc123"}) == "cloudflare"

    def test_akamai_header(self):
        assert _classify_block_headers({"X-Akamai-Transformed": "9 4_2093"}) == "akamai"

    def test_no_headers(self):
        assert _classify_block_headers({}) == ""
        assert _classify_block_headers(None) == ""

    def test_cloudfront_body_text(self):
        assert (
            _classify_block_text("ERROR: The request could not be satisfied")
            == "cloudfront"
        )

    def test_cloudflare_body_text(self):
        assert _classify_block_text("Just a moment...") == "cloudflare"
        assert _classify_block_text("Checking your browser before accessing") == "cloudflare"

    def test_akamai_body_text(self):
        assert _classify_block_text("Access Denied\nYou don't have permission") == "akamai"

    def test_clean_page_is_unknown(self):
        assert _classify_block_text("Welcome! Please log in.") == ""
        assert _classify_block_text("") == ""

    def test_cloudfront_wins_over_generic_akamai_phrase(self):
        """A CloudFront page that happens to say 'Access Denied' still
        classifies as cloudfront (checked first — most specific)."""
        assert (
            _classify_block_text(
                "ERROR: The request could not be satisfied. Access Denied."
            )
            == "cloudfront"
        )


# ── Real-browser integration (skipif playwright unavailable) ──────


@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright not installed")
class TestForensicsIntegration:
    @pytest_asyncio.fixture
    async def page(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()
            yield page
            await browser.close()

    async def _dispatch(self, handlers, name, **arguments):
        return json.loads(await handlers.dispatch(name, arguments, "agent-x"))

    async def _route_block(self, page):
        """Route the local origin to a CloudFront-style 403 WAF refusal."""

        async def handler(route):
            await route.fulfill(
                status=403,
                headers={"x-cache": "Error from cloudfront"},
                body=_set_content(
                    "<h1>ERROR: The request could not be satisfied</h1>"
                ),
                content_type="text/html",
            )

        await page.route(f"{ORIGIN}/**", handler)

    @pytest.mark.asyncio
    async def test_browse_to_refused_carries_forensics(self, page):
        """The Empower case end to end: a CloudFront 403 is served as a
        normal HTTP response — the result flags refused + block_vendor +
        a real screenshot file + the DOM snapshot with the marker text."""
        await self._route_block(page)
        handlers = _spatial_handlers(page)
        result = await self._dispatch(handlers, "browse_to", url=f"{ORIGIN}/blocked")

        assert result["refused"] is True
        assert result["http_status"] == 403
        assert result["block_vendor"] == "cloudfront"
        # A real screenshot file exists on disk.
        assert result["screenshot_path"] and os.path.exists(result["screenshot_path"])
        assert os.path.getsize(result["screenshot_path"]) > 0
        # The DOM snapshot carries the refusal marker (truncated).
        assert "could not be satisfied" in result["dom_snapshot"]
        # The pre-existing fields survive.
        assert result["extracted"] is True
        assert "egress" in result and "captcha" in result

    @pytest.mark.asyncio
    async def test_browse_to_success_has_no_refusal(self, page):

        async def handler(route):
            await route.fulfill(
                status=200,
                body=_set_content("<p>hello enroll</p>"),
                content_type="text/html",
            )

        await page.route(f"{ORIGIN}/**", handler)
        handlers = _spatial_handlers(page)
        result = await self._dispatch(handlers, "browse_to", url=f"{ORIGIN}/ok")
        assert "refused" not in result
        assert "block_vendor" not in result
        assert result["extracted"] is True

    @pytest.mark.asyncio
    async def test_interact_failure_carries_forensics(self, page):
        """interact's error path gets the same evidence (where cheap)."""
        await page.set_content(_set_content("<p>no matching controls</p>"))
        handlers = _spatial_handlers(page)
        result = await self._dispatch(
            handlers, "interact", id="no-such-element", action="click", timeout_ms=250
        )
        assert "error" in result
        assert result["block_vendor"] == "none"
        assert result["screenshot_path"] and os.path.exists(result["screenshot_path"])
        assert result["dom_snapshot"] is not None

    @pytest.mark.asyncio
    async def test_block_vendor_absent_case(self, page):
        """A clean page classifies as none (not a false positive)."""
        await page.set_content(_set_content("<h1>Welcome</h1>"))
        from parousia.spatial.tools import _detect_block_vendor

        assert await _detect_block_vendor(page) == "none"

    @pytest.mark.asyncio
    async def test_cloudflare_and_akamai_headers_classify(self, page):
        from parousia.spatial.tools import _detect_block_vendor

        await page.set_content(_set_content("<h1>Just a moment...</h1>"))
        response = SimpleNamespace(headers={"cf-ray": "x1"})
        assert await _detect_block_vendor(page, response) == "cloudflare"

        await page.set_content(_set_content("<h1>Access Denied</h1>"))
        response = SimpleNamespace(headers={"X-Akamai-Transformed": "yes"})
        assert await _detect_block_vendor(page, response) == "akamai"


@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright not installed")
class TestSessionPersistence:
    @pytest_asyncio.fixture
    async def page(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()
            yield page
            await browser.close()

    @pytest.mark.asyncio
    async def test_cookie_survives_two_browse_to_calls(self, page):
        """MP-6 (part C): the pool's persistent context keeps cookies —
        a cookie set by the first browse_to's page is visible to the
        second browse_to's page on the same origin."""

        async def handler(route):
            path = route.request.url.replace(f"{ORIGIN}/", "")
            if path == "set":
                body = (
                    "<script>document.cookie = 'enroll_session=abc123; path=/';"
                    "document.body.innerHTML = 'cookie set';</script>"
                )
            else:
                body = "<p>read page</p>"
            await route.fulfill(
                status=200, body=_set_content(body), content_type="text/html"
            )

        await page.route(f"{ORIGIN}/**", handler)
        handlers = _spatial_handlers(page)

        first = json.loads(
            await handlers.dispatch("browse_to", {"url": f"{ORIGIN}/set"}, "agent-x")
        )
        assert first["extracted"] is True

        second = json.loads(
            await handlers.dispatch(
                "browse_to", {"url": f"{ORIGIN}/read", "timeout_ms": 5000}, "agent-x"
            )
        )
        assert second["extracted"] is True

        cookies = await page.context.cookies(f"{ORIGIN}")
        named = {c["name"]: c["value"] for c in cookies}
        assert named.get("enroll_session") == "abc123"

    @pytest.mark.asyncio
    async def test_descriptions_advertise_persistence(self):
        """The tool descriptions say it, so agents don't guess."""
        from parousia.spatial.tools import (
            browse_to_schema,
            interact_schema,
        )

        assert "persist" in browse_to_schema()["description"].lower()
        assert "persist" in interact_schema()["description"].lower()
        assert "block_vendor" in browse_to_schema()["description"]
        assert "forensics" in interact_schema()["description"].lower()


# ── MCP dispatch wiring: wait_for_email through the real server ───


class TestWaitForEmailWiring:
    def _build(self, tmp_path, monkeypatch, inbox):
        import fakeredis

        import parousia.guard.mcp_server as mcp_mod
        import parousia.temporal.db as tdb

        monkeypatch.setattr(
            "parousia.inbox.inbox_store.InboxStore", lambda *a, **kw: inbox
        )
        monkeypatch.setattr(
            "parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: fakeredis.FakeRedis()
        )
        monkeypatch.setattr(
            tdb, "DEFAULT_DB_PATH", str(tmp_path / "temporal.db")
        )

        def fake_temporal(*a, **kw):
            db = tdb.TemporalDB(db_path=":memory:")
            db.connect()
            db.create_tables()
            return db

        monkeypatch.setattr(mcp_mod, "TemporalDB", fake_temporal)
        monkeypatch.setattr(mcp_mod, "MemoryRecorder", MagicMock)

        async def fake_spatial_dispatch(self, name, arguments, agent_id):
            return json.dumps({"error": f"spatial:{name}"})

        from parousia.spatial.tools import SpatialToolHandlers

        monkeypatch.setattr(SpatialToolHandlers, "dispatch", fake_spatial_dispatch)
        return mcp_mod._build_server()

    def _call_text(self, server, name, arguments):
        """Dispatch one call through the REAL handler; return the raw text
        (the SDK's schema-validation error path returns prose, not JSON)."""
        import mcp.types as types

        async def run():
            handler = server.request_handlers[types.CallToolRequest]
            result = await handler(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(name=name, arguments=arguments)
                )
            )
            return result.root.content[0].text

        return asyncio.run(run())

    def _call(self, server, name, arguments):
        return json.loads(self._call_text(server, name, arguments))

    def test_wait_for_email_listed_and_dispatches(self, tmp_path, monkeypatch):
        inbox = InboxStore(str(tmp_path / "inbox.db"))
        # stdio/no-auth path: the empty test config falls back to agent id
        # "default" — store the fixture mail under that id.
        inbox.store_message(
            _msg(
                "default",
                "Confirm your signup",
                "Visit https://local.test/confirm?t=9",
                sender="bot@acme.example",
            )
        )
        server, _account_store = self._build(tmp_path, monkeypatch, inbox)

        import mcp.types as types

        listed = asyncio.run(
            server.request_handlers[types.ListToolsRequest](
                types.ListToolsRequest(params=None)
            )
        )
        names = {t.name for t in listed.root.tools}
        assert "wait_for_email" in names

        # Found on the first poll → instant (no sleep leg runs).
        result = self._call(server, "wait_for_email", {"pattern": "confirm"})
        assert result["found"] is True, result
        assert result["message"]["subject"] == "Confirm your signup"
        assert result["links"] == ["https://local.test/confirm?t=9"]

    def test_wait_for_email_timeout_through_dispatch(self, tmp_path, monkeypatch):
        inbox = InboxStore(str(tmp_path / "inbox.db"))
        inbox.store_message(_msg("hermes", "unrelated"))
        server, _ = self._build(tmp_path, monkeypatch, inbox)
        result = self._call(
            server, "wait_for_email", {"pattern": "missing", "timeout_ms": 100}
        )
        assert result == {"found": False}

    def test_wait_for_email_requires_pattern(self, tmp_path, monkeypatch):
        """The SDK's input validation rejects the call before dispatch (the
        schema requires `pattern`) — the raw text names the missing field."""
        inbox = InboxStore(str(tmp_path / "inbox.db"))
        server, _ = self._build(tmp_path, monkeypatch, inbox)
        raw = self._call_text(server, "wait_for_email", {})
        assert "pattern" in raw, raw