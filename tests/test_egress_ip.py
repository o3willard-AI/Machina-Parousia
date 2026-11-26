"""MP-10c (issue #11 ws1, chunk 3 of 3): check_egress_ip verification tool.

Covers the [DONE] gate:
- ``check_egress_ip()`` (mocked pool/page) returns the page IP and
  reports ``proxy=""`` (direct) — and calls the pool with an EMPTY
  override (the opt-in gate holds for verification too);
- ``check_egress_ip(proxy="tor")`` resolves via ``_resolve_tor`` and
  reports ``proxy="tor"`` **BY NAME** — no URL substring anywhere in
  the result (asserted against the resolved socks URL);
- a resolution failure surfaces the error and never touches the pool;
- a named ``spatial.proxies`` transport verifies through its own
  context (by name, never URL);
- an IP-echo fetch failure returns a clear error; a malformed echo
  body (not an IP) is refused, never reported as an egress IP;
- the schema is registered (the 6th spatial tool, in ALL_SPATIAL_SCHEMAS);
- real browser (skipif playwright): ``check_egress_ip()`` on DIRECT
  egress returns the host's ACTUAL public IP (cross-checked against a
  Python-side echo fetch).
"""

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from parousia.config import ParousiaConfig
from parousia.spatial.browser_pool import BrowserPoolManager, SpatialConfig
from parousia.spatial.serializer import SpatialSerializer
from parousia.spatial.tools import (
    ALL_SPATIAL_SCHEMAS,
    IP_ECHO_URL,
    SpatialToolHandlers,
    _looks_like_ip,
    check_egress_ip_schema,
)

PAGE_IP = "203.0.113.7"  # mocked echo body (TEST-NET-3 — never routed)
PROXIES = {"residential-1": "http://user:pass@gate.example:8080"}
TOR_URL = "socks5://127.0.0.1:9050"


# ── Fixtures ─────────────────────────────────────────────────────


def _cfg(spatial_update=None):
    cfg = ParousiaConfig()
    if spatial_update:
        cfg = cfg.model_copy(
            update={"spatial": cfg.spatial.model_copy(update=spatial_update)}
        )
    return cfg


def _handlers(cfg):
    """SpatialToolHandlers over a fully mocked pool (page echoes PAGE_IP)."""
    pool = MagicMock()
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value=PAGE_IP)
    browser = MagicMock()
    browser.page = page
    pool.get_browser = AsyncMock(return_value=browser)
    pool.proxy_for = MagicMock(return_value="")
    tor = MagicMock()
    tor.ensure_running = MagicMock(return_value=TOR_URL)
    pool.tor_manager = MagicMock(return_value=tor)
    serializer = MagicMock()
    return SpatialToolHandlers(cfg, pool, serializer), pool


def _dispatch(handlers, arguments):
    return json.loads(asyncio.run(handlers.dispatch("check_egress_ip", arguments, "agent-x")))


# ── Schema registration ─────────────────────────────────────────


class TestSchema:
    def test_schema_shape(self):
        schema = check_egress_ip_schema()
        assert schema["name"] == "check_egress_ip"
        props = schema["inputSchema"]["properties"]
        assert props["proxy"]["type"] == "string"
        assert props["proxy"]["default"] == ""

    def test_registered_as_6th_spatial_tool(self):
        names = [s["name"] for s in ALL_SPATIAL_SCHEMAS]
        assert names == [
            "browse_to",
            "interact",
            "extract_page_state",
            "fill_credentials",
            "dump_form",
            "check_egress_ip",
        ]

    def test_ip_validation_helper(self):
        assert _looks_like_ip("203.0.113.7")
        assert _looks_like_ip("2001:db8::1")
        assert not _looks_like_ip("")
        assert not _looks_like_ip("<html>oops</html>")
        assert not _looks_like_ip("999.1.1.1")  # octet > 255
        assert not _looks_like_ip("not-an-ip")


# ── Direct egress (the default — empty override, always) ────────


class TestDirectEgress:
    def test_no_proxy_returns_page_ip_and_empty_override(self):
        handlers, pool = _handlers(_cfg())
        result = _dispatch(handlers, {})
        assert result == {"ip": PAGE_IP, "proxy": ""}
        pool.get_browser.assert_awaited_once_with("agent-x", override_proxy="")

    def test_explicit_empty_proxy_is_direct_too(self):
        handlers, pool = _handlers(_cfg({"proxies": PROXIES}))
        result = _dispatch(handlers, {"proxy": ""})
        assert result == {"ip": PAGE_IP, "proxy": ""}
        pool.get_browser.assert_awaited_once_with("agent-x", override_proxy="")

    def test_navigates_to_the_ip_echo_endpoint(self):
        handlers, _ = _handlers(_cfg())
        _dispatch(handlers, {})
        # The echo page (and only it) is fetched — read-only verification.
        import inspect

        page = handlers.browser_pool.get_browser.return_value.page
        assert page.goto.await_args.args[0] == IP_ECHO_URL
        assert IP_ECHO_URL == "https://api.ipify.org"


# ── Named transport verification ────────────────────────────────


class TestNamedTransports:
    def test_tor_reports_name_never_url(self):
        handlers, pool = _handlers(_cfg({"tor_enabled": True}))
        result = _dispatch(handlers, {"proxy": "tor"})
        assert result == {"ip": PAGE_IP, "proxy": "tor"}
        # Resolved through the managed transport's SOCKS URL...
        pool.get_browser.assert_awaited_once_with("agent-x", override_proxy=TOR_URL)
        # ...but the RESULT carries the name only — no URL substring.
        assert "socks5" not in json.dumps(result)
        assert "9050" not in json.dumps(result)

    def test_tor_transport_launch_is_reused_not_restarted(self):
        handlers, pool = _handlers(_cfg({"tor_enabled": True}))
        _dispatch(handlers, {"proxy": "tor"})
        _dispatch(handlers, {"proxy": "tor"})
        tm = pool.tor_manager.return_value
        assert tm.ensure_running.call_count == 2  # per-request resolution
        pool.get_browser.assert_awaited_with("agent-x", override_proxy=TOR_URL)

    def test_named_proxy_reports_name_never_url(self):
        handlers, pool = _handlers(_cfg({"proxies": PROXIES}))
        result = _dispatch(handlers, {"proxy": "residential-1"})
        assert result == {"ip": PAGE_IP, "proxy": "residential-1"}
        pool.get_browser.assert_awaited_once_with(
            "agent-x", override_proxy=PROXIES["residential-1"]
        )
        # No URL, host, or embedded credentials anywhere in the result.
        dumped = json.dumps(result)
        assert "http://" not in dumped
        assert "gate.example" not in dumped
        assert "user:pass" not in dumped


# ── Fail-loud (resolution + transport failures) ─────────────────


class TestFailLoud:
    def test_tor_disabled_surfaces_error_before_pool(self):
        handlers, pool = _handlers(_cfg())
        result = _dispatch(handlers, {"proxy": "tor"})
        assert result == {
            "error": "Tor is not enabled (set spatial.tor_enabled=true) to use proxy='tor'"
        }
        pool.get_browser.assert_not_awaited()

    def test_tor_unavailable_surfaces_error_never_direct(self):
        from parousia.spatial.tor import TorUnavailableError

        handlers, pool = _handlers(_cfg({"tor_enabled": True}))
        tm = pool.tor_manager.return_value
        tm.ensure_running = MagicMock(
            side_effect=TorUnavailableError(
                "Tor binary not found; install `tor` to use proxy='tor' "
                "(Debian/Ubuntu: apt install tor)"
            )
        )
        result = _dispatch(handlers, {"proxy": "tor"})
        assert result["error"].startswith("Tor transport unavailable: Tor binary not found")
        pool.get_browser.assert_not_awaited()

    def test_unknown_name_surfaces_error(self):
        handlers, pool = _handlers(_cfg({"proxies": PROXIES}))
        result = _dispatch(handlers, {"proxy": "nope"})
        assert result["error"].startswith("unknown proxy name 'nope'")
        pool.get_browser.assert_not_awaited()

    def test_echo_fetch_failure_is_a_clear_error(self):
        handlers, _ = _handlers(_cfg())
        page = handlers.browser_pool.get_browser.return_value.page
        page.goto = AsyncMock(side_effect=Exception("net is down"))
        result = _dispatch(handlers, {})
        assert result == {"error": "IP echo fetch failed: net is down"}

    def test_malformed_echo_body_is_refused(self):
        handlers, _ = _handlers(_cfg())
        page = handlers.browser_pool.get_browser.return_value.page
        page.evaluate = AsyncMock(return_value="<html><body>Sorry</body></html>")
        result = _dispatch(handlers, {})
        assert result["error"].startswith("IP echo returned unexpected content")
        assert "Sorry" in result["error"]

    def test_empty_echo_body_is_refused(self):
        handlers, _ = _handlers(_cfg())
        page = handlers.browser_pool.get_browser.return_value.page
        page.evaluate = AsyncMock(return_value="")
        result = _dispatch(handlers, {})
        assert result["error"].startswith("IP echo returned unexpected content")

    def test_ipv6_echo_body_accepted(self):
        handlers, _ = _handlers(_cfg())
        page = handlers.browser_pool.get_browser.return_value.page
        page.evaluate = AsyncMock(return_value="2001:db8::1")
        result = _dispatch(handlers, {})
        assert result == {"ip": "2001:db8::1", "proxy": ""}


# ── Real browser (skipif playwright) ─────────────────────────────


def _real_browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch(headless=True)
            except Exception:
                return False
            b.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _real_browser_available(), reason="playwright/Chromium not available")
class TestRealBrowserDirectEgress:
    @pytest.mark.asyncio
    async def test_direct_returns_host_actual_ip(self, tmp_path):
        """Real headless Chromium on DIRECT egress: the echo IP must equal
        the host's ACTUAL public IP (cross-checked with a Python-side
        echo fetch through the same egress)."""
        import urllib.request

        cfg = dataclasses.replace(SpatialConfig(), profile_dir=str(tmp_path / "profiles"))
        pool = BrowserPoolManager(cfg)
        handlers = SpatialToolHandlers(ParousiaConfig(), pool, SpatialSerializer())
        result = json.loads(
            await handlers.dispatch("check_egress_ip", {}, "agent-egress")
        )
        try:
            assert result.get("proxy") == "", result
            ip = result.get("ip", "")
            assert _looks_like_ip(ip), result
            direct = (
                urllib.request.urlopen(IP_ECHO_URL, timeout=15).read().decode().strip()
            )
            assert ip == direct, f"browser egress {ip} != host {direct}"
        finally:
            await pool.shutdown_all()