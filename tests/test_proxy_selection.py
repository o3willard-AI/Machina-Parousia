"""MP-10a (issue #11 ws1, chunk 1 of 3): agent-called transport
selection + the named proxy registry.

Covers the [DONE] gate:
- browse_to/interact with ``proxy=""`` (or omitted) NEVER sets a proxy —
  the OPT-IN gate; direct egress is the default and stays the default;
- a named proxy (from the operator's ``spatial.proxies`` menu) resolves
  to its URL and is used for THAT request's browser context;
- an unknown name errors clearly with the available list ("tor" is
  reserved for chunk 2 — it must error as unknown until then);
- the config ``spatial.proxies`` roundtrips through YAML;
- the operator mapping (``proxy_servers``/``proxy_default``, MP-3) is
  intact for the DEFAULT path;
- a real headless Chromium (skipif playwright) launches DIRECT with
  ``proxy=""`` (no proxy in the launch kwargs), and a named transport
  lands in the launch kwargs of its own separate persistent context.
"""

import asyncio
import dataclasses
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from parousia.config import ParousiaConfig, load_config
from parousia.spatial.browser_pool import BrowserPoolManager, SpatialConfig
from parousia.spatial.tools import (
    ALL_SPATIAL_SCHEMAS,
    SpatialToolHandlers,
    browse_to_schema,
    interact_schema,
)

PROXIES = {
    "residential-1": "http://user:pass@gate.example:8080",
    "mobile-1": "socks5://127.0.0.1:1080",
}


def _cfg(proxies=None):
    cfg = ParousiaConfig()
    if proxies is not None:
        cfg = cfg.model_copy(
            update={"spatial": cfg.spatial.model_copy(update={"proxies": proxies})}
        )
    return cfg


def _handlers(cfg):
    """SpatialToolHandlers over a fully mocked pool/serializer (the
    test_spatial_tools.py pattern: async get_browser -> mock browser)."""
    pool = MagicMock()
    page = MagicMock()
    page.content = AsyncMock(return_value="<html><body></body></html>")
    page.evaluate = AsyncMock(return_value="UA")
    page.goto = AsyncMock(return_value=None)
    # Every interact action is an awaited page call.
    for act in (
        "click", "type", "scroll_into_view", "select_option",
        "check", "uncheck", "hover", "press",
    ):
        setattr(page, act, AsyncMock())
    browser = MagicMock()
    browser.page = page
    pool.get_browser = AsyncMock(return_value=browser)
    pool.proxy_for = MagicMock(return_value="")
    serializer = MagicMock()
    sdom = MagicMock()
    sdom.model_dump.return_value = {"type": "sdom"}
    serializer.to_sdom.return_value = sdom
    return SpatialToolHandlers(cfg, pool, serializer), pool


def _dispatch(handlers, name, arguments, agent_id="agent-x"):
    return json.loads(asyncio.run(handlers.dispatch(name, arguments, agent_id)))


# ── Config: the named registry ───────────────────────────────────


class TestProxyRegistryConfig:
    def test_defaults_empty_both_shapes(self):
        assert ParousiaConfig().spatial.proxies == {}
        assert SpatialConfig().proxies == {}

    def test_yaml_roundtrip(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "domain": "test.example.com",
                    "spatial": {"proxies": PROXIES},
                }
            )
        )
        cfg = load_config(cfg_file)
        assert cfg.spatial.proxies == PROXIES

    def test_operator_mapping_untouched(self):
        """The MP-3 operator mapping still exists beside the menu."""
        cfg = _cfg()
        assert cfg.spatial.proxy_servers == {}
        assert cfg.spatial.proxy_default == ""
        sc = dataclasses.replace(
            SpatialConfig(), proxy_servers={"a": "http://p:1"}, proxy_default="http://d:2"
        )
        assert sc.proxy_servers == {"a": "http://p:1"}
        assert sc.proxy_default == "http://d:2"


# ── Resolution: the opt-in gate ─────────────────────────────────


class TestResolveProxy:
    def test_empty_is_direct(self):
        handlers, _ = _handlers(_cfg(PROXIES))
        for name in ("", "   ", None):
            url, err = handlers._resolve_proxy(name)
            assert url == ""
            assert err is None

    def test_named_resolves_to_url(self):
        handlers, _ = _handlers(_cfg(PROXIES))
        url, err = handlers._resolve_proxy("residential-1")
        assert url == PROXIES["residential-1"]
        assert err is None

    def test_unknown_name_errors_with_available_list(self):
        handlers, _ = _handlers(_cfg(PROXIES))
        url, err = handlers._resolve_proxy("nope")
        assert url == ""
        assert err == {
            "error": "unknown proxy name 'nope'; available: mobile-1, residential-1"
        }

    def test_tor_is_reserved_unknown(self):
        """'tor' is chunk 2's name — until it is registered there it
        errors as an unknown name, exactly like any other."""
        handlers, _ = _handlers(_cfg(PROXIES))
        url, err = handlers._resolve_proxy("tor")
        assert url == ""
        assert "unknown proxy name 'tor'" in err["error"]
        assert "available: mobile-1, residential-1" in err["error"]

    def test_empty_registry_lists_none(self):
        handlers, _ = _handlers(_cfg())
        url, err = handlers._resolve_proxy("x")
        assert url == ""
        assert err == {"error": "unknown proxy name 'x'; available: (none configured)"}

    def test_case_sensitive_name(self):
        handlers, _ = _handlers(_cfg(PROXIES))
        url, err = handlers._resolve_proxy("Residential-1")
        assert url == ""
        assert "unknown proxy name 'Residential-1'" in err["error"]


# ── Handlers: agent-called selection on browse_to / interact ────


class TestHandlerSelection:
    def test_schemas_expose_proxy(self):
        for schema in (browse_to_schema(), interact_schema()):
            props = schema["inputSchema"]["properties"]
            assert "proxy" in props, schema["name"]
            assert props["proxy"]["type"] == "string"
            assert props["proxy"]["default"] == ""
        names = [s["name"] for s in ALL_SPATIAL_SCHEMAS]
        assert "browse_to" in names and "interact" in names

    def test_browse_to_omitted_proxy_never_sets_proxy(self):
        """THE OPT-IN GATE: no `proxy` argument → override_proxy is ''
        (direct) — the pool is called with an EMPTY override."""
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com"})
        assert result["extracted"] is True
        pool.get_browser.assert_awaited_once_with("agent-x", override_proxy="")

    def test_browse_to_empty_proxy_never_sets_proxy(self):
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": ""})
        assert result["extracted"] is True
        pool.get_browser.assert_awaited_once_with("agent-x", override_proxy="")
        assert result["proxy"] == ""
        assert result["egress"]["proxy"] == "direct"

    def test_browse_to_named_proxy_used_for_request(self):
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(
            handlers, "browse_to", {"url": "https://example.com", "proxy": "residential-1"}
        )
        pool.get_browser.assert_awaited_once_with(
            "agent-x", override_proxy=PROXIES["residential-1"]
        )
        # The result reports the NAME (never the URL — it embeds creds).
        assert result["proxy"] == "residential-1"
        assert result["egress"]["proxy"] == "residential-1"
        assert PROXIES["residential-1"] not in json.dumps(result)

    def test_browse_to_unknown_name_errors_before_pool(self):
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(
            handlers, "browse_to", {"url": "https://example.com", "proxy": "nope"}
        )
        assert result == {"error": "unknown proxy name 'nope'; available: mobile-1, residential-1"}
        pool.get_browser.assert_not_awaited()

    def test_browse_to_tor_errors_until_chunk2(self):
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        assert "unknown proxy name 'tor'" in result["error"]
        pool.get_browser.assert_not_awaited()

    def test_interact_omitted_proxy_never_sets_proxy(self):
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(handlers, "interact", {"id": "el", "action": "click"})
        assert result["success"] is True
        pool.get_browser.assert_awaited_once_with("agent-x", override_proxy="")
        assert result["proxy"] == ""

    def test_interact_named_proxy_targets_transport_page(self):
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(
            handlers, "interact", {"id": "el", "action": "click", "proxy": "mobile-1"}
        )
        pool.get_browser.assert_awaited_once_with(
            "agent-x", override_proxy=PROXIES["mobile-1"]
        )
        assert result["proxy"] == "mobile-1"

    def test_interact_unknown_name_errors_before_pool(self):
        handlers, pool = _handlers(_cfg(PROXIES))
        result = _dispatch(handlers, "interact", {"id": "el", "action": "click", "proxy": "zzz"})
        assert result["error"].startswith("unknown proxy name 'zzz'")
        pool.get_browser.assert_not_awaited()

    def test_default_path_operator_mapping_reported_unchanged(self):
        """MP-3 behavior intact: no agent-called proxy → egress reports
        the OPERATOR mapping (proxy_for), not the menu."""
        handlers, pool = _handlers(_cfg(PROXIES))
        pool.proxy_for = MagicMock(return_value="http://operator-proxy:3128")
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com"})
        assert result["egress"]["proxy"] == "http://operator-proxy:3128"
        assert result["proxy"] == ""


# ── Pool: per-transport contexts, operator mapping intact ───────


def _mock_playwright():
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
    pw.start = AsyncMock(return_value=pw)
    pw.stop = AsyncMock()
    pw.chromium = MagicMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=browser)
    page.title = AsyncMock(return_value="about:blank")
    page.goto = AsyncMock(return_value=None)
    return pw


def _pool(cfg, tmp_path):
    cfg = dataclasses.replace(cfg, profile_dir=str(tmp_path / "profiles"))
    return BrowserPoolManager(cfg), _mock_playwright()


def _launch(pool, pw, agent, override=""):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "parousia.spatial.browser_pool.async_playwright",
            lambda: pw,
            raising=False,
        )
        instance = asyncio.run(pool.get_browser(agent, override_proxy=override))
    return instance, pw.chromium.launch_persistent_context.call_args.kwargs


class TestPoolPerTransportContexts:
    def test_pool_key_bare_agent_when_no_override(self):
        pool = BrowserPoolManager(SpatialConfig())
        assert pool._pool_key("a") == "a"
        assert pool._pool_key("a", "") == "a"
        assert pool._pool_key("a", "http://p:1") != "a"
        # Stable across calls (hash of the URL).
        assert pool._pool_key("a", "http://p:1") == pool._pool_key("a", "http://p:1")

    def test_default_context_launches_without_proxy(self):
        pool, pw = _pool(SpatialConfig(), _tmp())
        instance, kwargs = _launch(pool, pw, "agent-a")
        assert "proxy" not in kwargs
        assert instance.agent_id == "agent-a"
        assert pool._pool_key("agent-a") == "agent-a"

    def test_operator_mapping_applies_on_default_path(self):
        cfg = dataclasses.replace(
            SpatialConfig(),
            proxy_servers={"agent-a": "http://op:8080"},
            proxy_default="",
        )
        pool, pw = _pool(cfg, _tmp())
        _, kwargs = _launch(pool, pw, "agent-a")
        assert kwargs["proxy"] == {"server": "http://op:8080"}
        _, kwargs_default_agent = _launch(pool, pw, "agent-b")
        assert "proxy" not in kwargs_default_agent

    def test_override_launches_fresh_context_with_proxy(self):
        pool, pw = _pool(SpatialConfig(), _tmp())
        url = PROXIES["residential-1"]
        instance, kwargs = _launch(pool, pw, "agent-a", override=url)
        assert kwargs["proxy"] == {"server": url}
        # Separate pool key + profile dir — NOT the agent's default dir.
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        assert instance.agent_id == f"agent-a__px-{digest}"
        assert instance.profile_dir.name == f"agent-a__px-{digest}"

    def test_override_wins_over_operator_mapping(self):
        cfg = dataclasses.replace(
            SpatialConfig(), proxy_default="http://operator:3128"
        )
        pool, pw = _pool(cfg, _tmp())
        _, kwargs = _launch(pool, pw, "agent-a", override="http://named:9")
        assert kwargs["proxy"] == {"server": "http://named:9"}

    def test_transports_coexist_default_untouched(self):
        """Default context + two named transports → three separate cached
        contexts; re-requesting a transport reuses ITS context."""
        pool, pw = _pool(SpatialConfig(), _tmp())
        _launch(pool, pw, "agent-a")
        _launch(pool, pw, "agent-a", override=PROXIES["residential-1"])
        _launch(pool, pw, "agent-a", override=PROXIES["mobile-1"])
        keys = set(pool._browsers.keys())
        assert len(keys) == 3
        assert "agent-a" in keys
        # Same transport again → cache hit, no new launch.
        launches_before = pw.chromium.launch_persistent_context.call_count
        _launch(pool, pw, "agent-a", override=PROXIES["residential-1"])
        assert pw.chromium.launch_persistent_context.call_count == launches_before
        # Default context never inherited a proxy.
        default_kwargs = None
        for call in pw.chromium.launch_persistent_context.call_args_list:
            if "proxy" not in call.kwargs:
                default_kwargs = call.kwargs
        assert default_kwargs is not None


def _tmp():
    import tempfile
    return __import__("pathlib").Path(tempfile.mkdtemp())


# ── Real browser (skipif playwright missing) ─────────────────────


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
    async def test_proxy_empty_launches_direct_no_proxy_set(self, tmp_path):
        """Real headless Chromium: proxy='' → the launch kwargs carry NO
        proxy (direct egress), even with a menu configured."""
        cfg = dataclasses.replace(
            SpatialConfig(), profile_dir=str(tmp_path / "profiles"), proxies=PROXIES
        )
        pool = BrowserPoolManager(cfg)
        recorded = {}
        orig = BrowserPoolManager._launch_kwargs

        def spy(self, agent_id, override_proxy=""):
            recorded["override"] = override_proxy
            recorded["kwargs"] = orig(self, agent_id, override_proxy)
            return recorded["kwargs"]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(BrowserPoolManager, "_launch_kwargs", spy)
            instance = await pool.get_browser("agent-real", override_proxy="")
            page = instance.page
            await page.goto("about:blank")
        assert recorded["override"] == ""
        assert "proxy" not in recorded["kwargs"]
        assert instance.agent_id == "agent-real"
        await pool.shutdown_all()

    @pytest.mark.asyncio
    async def test_named_transport_lands_in_real_launch_kwargs(self, tmp_path):
        """Real headless Chromium launched THROUGH the named transport's
        proxy setting (launch only — nothing is fetched through it)."""
        cfg = dataclasses.replace(
            SpatialConfig(), profile_dir=str(tmp_path / "profiles"), proxies=PROXIES
        )
        pool = BrowserPoolManager(cfg)
        url = PROXIES["residential-1"]
        instance = await pool.get_browser("agent-real", override_proxy=url)
        assert instance.agent_id != "agent-real"
        assert instance.agent_id.startswith("agent-real__px-")
        await pool.shutdown_all()