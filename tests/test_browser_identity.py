"""MP-3 (part A): browser identity & egress control — tests (issue #16).

Covers the wi-MP-3 DONE gate items:
- UA / client hints set on the context (launch kwargs),
- ``navigator.webdriver`` suppressed (launch flag + init script),
- egress identity present in every ``browse_to`` result,
- per-navigation UA override applies consistent Sec-CH-UA,
- per-agent proxy selection (config-driven, default off).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from parousia.config import DEFAULT_CHROME_UA, ParousiaConfig
from parousia.spatial.browser_pool import (
    _WEBDRIVER_SUPPRESSION_SCRIPT,
    BrowserPoolManager,
    SpatialConfig,
)
from parousia.spatial.serializer import SpatialSerializer
from parousia.spatial.tools import (
    ALL_SPATIAL_SCHEMAS,
    SpatialToolHandlers,
    _client_hints_for,
    _effective_egress_ip,
)

# NOTE: no playwright skip marker — every test here is fully mocked (the
# launch kwargs / init script / CDP calls are asserted against mocks), so
# the identity gates run everywhere playwright is absent too.


# ── Helpers ─────────────────────────────────────────────────────


def _mock_browser():
    """A mocked persistent context + page with the async bits wired."""
    pw = MagicMock()
    browser = MagicMock()
    page = MagicMock()
    browser.pages = [page]
    browser.browser = MagicMock()
    browser.browser.is_connected.return_value = True
    browser.close = AsyncMock()
    browser.add_init_script = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    page.close = AsyncMock()
    # `await async_playwright().start()` → the playwright driver object
    # (the tests patch async_playwright with `lambda: pw`).
    pw.start = AsyncMock(return_value=pw)
    pw.stop = AsyncMock()
    pw.chromium = MagicMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=browser)
    page.title = AsyncMock(return_value="about:blank")
    page.goto = AsyncMock(return_value=None)
    return pw, browser, page


def _cfg_with_profile(config, tmp_path):
    """config copy with a tmp profile_dir (never the real /var/lib path)."""
    if hasattr(config, "model_copy"):  # pydantic (parousia.config.SpatialConfig)
        return config.model_copy(update={"profile_dir": str(tmp_path)})
    import dataclasses

    return dataclasses.replace(config, profile_dir=str(tmp_path))


def _launch_kwargs(config, tmp_path) -> dict:
    """Drive get_browser with mocked playwright; return the launch kwargs."""
    manager = BrowserPoolManager(_cfg_with_profile(config, tmp_path))
    pw, _, _ = _mock_browser()
    with pytest.MonkeyPatch.context() as mp:
        # raising=False: async_playwright may be unimportable (playwright is
        # an undeclared dep); the mock replaces whatever is there.
        mp.setattr(
            "parousia.spatial.browser_pool.async_playwright",
            lambda: pw,
            raising=False,
        )
        instance = asyncio.run(manager.get_browser("agent-x"))
    kwargs = pw.chromium.launch_persistent_context.call_args.kwargs
    return kwargs, instance


# ── Config defaults ──────────────────────────────────────────────


class TestIdentityDefaults:
    def test_default_ua_is_desktop_chrome_not_headless(self):
        assert "HeadlessChrome" not in DEFAULT_CHROME_UA
        assert "Chrome/" in DEFAULT_CHROME_UA
        cfg = ParousiaConfig().spatial
        assert cfg.user_agent == DEFAULT_CHROME_UA

    def test_viewport_locale_timezone_defaults(self):
        cfg = ParousiaConfig().spatial
        assert cfg.viewport_width == 1280 and cfg.viewport_height == 800
        assert cfg.locale == "en-US"
        assert cfg.timezone_id  # a real tz string

    def test_proxy_defaults_off(self):
        cfg = ParousiaConfig().spatial
        assert cfg.proxy_servers == {}
        assert cfg.proxy_default == ""


# ── Launch kwargs (identity on the context) ──────────────────────


class TestLaunchIdentity:
    def test_ua_and_viewport_and_locale_passed_to_launch(self, tmp_path):
        kwargs, _ = _launch_kwargs(ParousiaConfig().spatial, tmp_path)
        assert kwargs["user_agent"] == DEFAULT_CHROME_UA
        assert kwargs["viewport"] == {"width": 1280, "height": 800}
        assert kwargs["locale"] == "en-US"
        assert kwargs["timezone_id"]
        assert kwargs["headless"] is True

    def test_automation_controlled_flag_in_launch_args(self, tmp_path):
        """The Blink-level webdriver suppression flag is always present."""
        kwargs, _ = _launch_kwargs(ParousiaConfig().spatial, tmp_path)
        assert "--disable-blink-features=AutomationControlled" in kwargs["args"]

    def test_flag_not_duplicated_when_already_configured(self, tmp_path):
        cfg = ParousiaConfig().spatial.model_copy(
            update={"launch_args": ["--disable-blink-features=AutomationControlled"]}
        )
        kwargs, _ = _launch_kwargs(cfg, tmp_path)
        assert kwargs["args"].count("--disable-blink-features=AutomationControlled") == 1

    def test_configured_launch_args_preserved(self, tmp_path):
        cfg = ParousiaConfig().spatial.model_copy(
            update={"launch_args": ["--mute-audio"]}
        )
        kwargs, _ = _launch_kwargs(cfg, tmp_path)
        assert "--mute-audio" in kwargs["args"]

    def test_webdriver_suppression_init_script_registered(self, tmp_path):
        """The context gets the init script, and the agent's page is a
        FRESH page created after registration (so the script applies)."""
        cfg = ParousiaConfig().spatial
        manager = BrowserPoolManager(_cfg_with_profile(cfg, tmp_path))
        pw, browser, page = _mock_browser()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "parousia.spatial.browser_pool.async_playwright",
                lambda: pw,
                raising=False,
            )
            instance = asyncio.run(manager.get_browser("agent-x"))
        browser.add_init_script.assert_awaited_once()
        script = browser.add_init_script.call_args.args[0]
        assert "webdriver" in script
        assert script == _WEBDRIVER_SUPPRESSION_SCRIPT
        # The pre-existing (health-check) page was closed and a fresh one
        # created AFTER add_init_script — so the agent page carries it.
        page.close.assert_awaited()
        browser.new_page.assert_awaited_once()
        assert instance.page is page

    def test_suppression_script_sets_webdriver_false(self):
        """The init script pins navigator.webdriver to false (the JS-side
        half of the suppression; the Blink flag is the launch-arg half)."""
        assert "get: () => false" in _WEBDRIVER_SUPPRESSION_SCRIPT
        assert "navigator" in _WEBDRIVER_SUPPRESSION_SCRIPT


class TestProxySelection:
    def test_proxy_off_by_default(self, tmp_path):
        manager = BrowserPoolManager(ParousiaConfig().spatial)
        assert manager.proxy_for("any-agent") == ""
        kwargs, _ = _launch_kwargs(ParousiaConfig().spatial, tmp_path)
        assert "proxy" not in kwargs  # direct egress

    def test_per_agent_proxy(self):
        cfg = ParousiaConfig().spatial.model_copy(
            update={
                "proxy_servers": {
                    "pinned-agent": "http://proxy-a.internal:8080",
                    "socks-agent": "socks5://proxy-b.internal:1080",
                }
            }
        )
        manager = BrowserPoolManager(cfg)
        assert manager.proxy_for("pinned-agent") == "http://proxy-a.internal:8080"
        assert manager.proxy_for("socks-agent") == "socks5://proxy-b.internal:1080"
        # Unmapped agent → direct (default off)
        assert manager.proxy_for("other-agent") == ""

    def test_proxy_default_applies_to_unmapped_agents(self):
        cfg = ParousiaConfig().spatial.model_copy(
            update={
                "proxy_servers": {"a": "http://pa:1"},
                "proxy_default": "http://fallback:8080",
            }
        )
        manager = BrowserPoolManager(cfg)
        assert manager.proxy_for("unmapped") == "http://fallback:8080"
        assert manager.proxy_for("a") == "http://pa:1"  # explicit wins

    def test_proxy_passed_to_launch(self, tmp_path):
        cfg = ParousiaConfig().spatial.model_copy(
            update={"proxy_servers": {"agent-x": "http://egress:3128"}}
        )
        kwargs, _ = _launch_kwargs(cfg, tmp_path)
        assert kwargs["proxy"] == {"server": "http://egress:3128"}

    def test_dataclass_config_carries_identity_fields(self):
        """The standalone SpatialConfig (CLI/legacy) keeps the same shape."""
        cfg = SpatialConfig()
        assert cfg.user_agent
        assert cfg.viewport_width > 0 and cfg.viewport_height > 0
        assert cfg.locale and cfg.timezone_id
        assert cfg.proxy_servers == {} and cfg.proxy_default == ""


# ── Client hints + egress IP helpers ──────────────────────────────


class TestClientHints:
    def test_hints_derived_from_chrome_version(self):
        hints = _client_hints_for(DEFAULT_CHROME_UA)
        assert '"Chromium";v="142"' in hints["Sec-CH-UA"]
        assert '"Google Chrome";v="142"' in hints["Sec-CH-UA"]
        assert '"Not?A_Brand";v="99"' in hints["Sec-CH-UA"]
        assert hints["Sec-CH-UA-Mobile"] == "?0"
        assert hints["Sec-CH-UA-Platform"] == '"Windows"'

    def test_platform_follows_ua(self):
        mac_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        )
        assert _client_hints_for(mac_ua)["Sec-CH-UA-Platform"] == '"macOS"'
        linux_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0.0.0"
        assert _client_hints_for(linux_ua)["Sec-CH-UA-Platform"] == '"Linux"'

    def test_version_mirrors_ua_not_default(self):
        hints = _client_hints_for("Mozilla/5.0 (Windows NT 10.0) Chrome/137.0.0.0")
        assert '"Chromium";v="137"' in hints["Sec-CH-UA"]

    def test_unknown_ua_gets_current_version(self):
        hints = _client_hints_for("SomeBot/1.0")
        assert '"Chromium";v="' in hints["Sec-CH-UA"]


class TestEgressIp:
    def test_returns_a_string_without_network(self):
        ip = _effective_egress_ip()
        assert isinstance(ip, str) and ip  # "unknown" at worst


# ── browse_to result: egress identity + UA override ───────────────


def _handlers(browser_pool, page, credential_store=None):
    config = ParousiaConfig()
    pool = MagicMock()
    pool.get_browser = AsyncMock(return_value=MagicMock(page=page))
    pool.proxy_for = MagicMock(return_value="")
    serializer = MagicMock()
    sdom = MagicMock()
    sdom.model_dump.return_value = {"url": "u", "status": 200}
    serializer.to_sdom.return_value = sdom
    return SpatialToolHandlers(config, pool, serializer, credential_store=credential_store)


class TestBrowseToEgress:
    def test_result_carries_egress_identity(self):
        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        page.content = AsyncMock(return_value="<html></html>")
        page.evaluate = AsyncMock(return_value=DEFAULT_CHROME_UA)
        handlers = _handlers(None, page)

        result = asyncio.run(
            handlers._handle_browse_to({"url": "https://site.example"}, "agent-x")
        )
        assert "egress" in result, result
        egress = result["egress"]
        assert egress["user_agent"] == DEFAULT_CHROME_UA
        assert egress["ip"]  # effective source IP (or "unknown")
        assert egress["proxy"] == "direct"

    def test_egress_reports_proxy_route(self):
        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        page.content = AsyncMock(return_value="<html></html>")
        page.evaluate = AsyncMock(return_value=DEFAULT_CHROME_UA)
        handlers = _handlers(None, page)
        handlers.browser_pool.proxy_for = MagicMock(
            return_value="http://egress:3128"
        )
        result = asyncio.run(
            handlers._handle_browse_to({"url": "https://site.example"}, "agent-x")
        )
        assert result["egress"]["proxy"] == "http://egress:3128"

    def test_egress_survives_evaluate_failure(self):
        """page.evaluate unavailable → falls back to the configured UA."""
        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        page.content = AsyncMock(return_value="<html></html>")
        page.evaluate = AsyncMock(side_effect=RuntimeError("detached"))
        handlers = _handlers(None, page)
        result = asyncio.run(
            handlers._handle_browse_to({"url": "https://site.example"}, "agent-x")
        )
        assert result["egress"]["user_agent"] == DEFAULT_CHROME_UA


class TestPerNavigationUaOverride:
    def _cdp_mock(self):
        page = MagicMock()
        cdp = MagicMock()
        cdp.send = AsyncMock(return_value=None)
        page.context.new_cdp_session = AsyncMock(return_value=cdp)
        page.set_extra_http_headers = AsyncMock(return_value=None)
        return page, cdp

    def test_override_sent_via_cdp_with_consistent_hints(self):
        page, cdp = self._cdp_mock()
        handlers = _handlers(None, page)
        override = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        )
        asyncio.run(handlers._apply_identity(page, override, 30000))
        cdp.send.assert_awaited_once()
        method, params = cdp.send.call_args.args
        assert method == "Emulation.setUserAgentOverride"
        assert params["userAgent"] == override
        # Consistent metadata: version + platform mirror the UA.
        assert params["platform"] == "macOS"
        meta = params["userAgentMetadata"]
        assert meta["platform"] == "macOS"
        assert meta["mobile"] is False
        brands = {b["brand"]: b["version"] for b in meta["brands"]}
        assert brands["Chromium"] == "137"
        assert brands["Google Chrome"] == "137"
        assert meta["fullVersion"] == "137.0.0.0"
        # Sec-CH-UA headers mirrored as extra headers.
        headers = page.set_extra_http_headers.call_args.args[0]
        assert headers["Sec-CH-UA"] == _client_hints_for(override)["Sec-CH-UA"]
        assert headers["Sec-CH-UA-Platform"] == '"macOS"'

    def test_no_override_no_cdp_call(self):
        page, cdp = self._cdp_mock()
        handlers = _handlers(None, page)
        asyncio.run(handlers._apply_identity(page, None, 30000))
        asyncio.run(handlers._apply_identity(page, "", 30000))
        cdp.send.assert_not_awaited()

    def test_cdp_unavailable_degrades_to_context_ua(self):
        page = MagicMock()
        page.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("no cdp"))
        page.set_extra_http_headers = AsyncMock(return_value=None)
        handlers = _handlers(None, page)
        # Must not raise — the context UA (launch-time) stands.
        asyncio.run(handlers._apply_identity(page, "UA/1.0", 30000))
        page.set_extra_http_headers.assert_awaited_once()

    def test_browse_to_applies_override_before_navigation(self):
        page, cdp = self._cdp_mock()
        page.goto = AsyncMock(return_value=None)
        page.content = AsyncMock(return_value="<html></html>")
        page.evaluate = AsyncMock(return_value="UA-Override/1.0")
        handlers = _handlers(None, page)
        result = asyncio.run(
            handlers._handle_browse_to(
                {"url": "https://site.example", "user_agent": "UA-Override/1.0"},
                "agent-x",
            )
        )
        # Override applied before goto (order matters for the request).
        assert page.set_extra_http_headers.await_count >= 1
        assert "egress" in result
        # Schema exposes the per-navigation override.
        schema = next(s for s in ALL_SPATIAL_SCHEMAS if s["name"] == "browse_to")
        assert "user_agent" in schema["inputSchema"]["properties"]


class TestSchemaRegistry:
    def test_fill_credentials_listed(self):
        names = {s["name"] for s in ALL_SPATIAL_SCHEMAS}
        assert "fill_credentials" in names
        assert "browse_to" in names and "interact" in names

    def test_browse_to_documented_ua_default(self):
        schema = next(s for s in ALL_SPATIAL_SCHEMAS if s["name"] == "browse_to")
        assert "user_agent" in schema["inputSchema"]["properties"]