"""MP-9: full fingerprint hardening ("stealth"), config-gated (issue #15).

Covers the DONE gate:
- config flag ``stealth_enabled`` (default False) in BOTH SpatialConfig
  shapes (pydantic ``parousia.config`` + the standalone dataclass);
- with ``stealth_enabled=False`` (default): the pool launches exactly as
  before — the PLAIN webdriver-suppression script is injected, the
  stealth script is NOT;
- with ``stealth_enabled=True``: the stealth script is injected INSTEAD
  (suppression script absent), and against REAL headless Chromium:
  * ``navigator.webdriver`` is **undefined** (not false),
  * ``navigator.plugins.length > 0`` (realistic entries),
  * ``window.chrome`` is present (with runtime),
  * the UA carries no ``Headless`` substring,
  * plus the other hardened signals: platform, history.length, WebGL
    UNMASKED vendor/renderer, and getParameter passthrough intact;
- ``browse_to`` results expose ``stealth`` (true/false).

NOTE: like ``tests/test_browser_pool.py`` / ``test_spatial_tools.py``,
the REAL-browser class below needs playwright + installed Chromium and
is skipped otherwise (pytest.importorskip); the mock-based gating tests
run everywhere.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from parousia.config import DEFAULT_CHROME_UA, ParousiaConfig
from parousia.spatial.browser_pool import (
    _STEALTH_SCRIPT,
    _WEBDRIVER_SUPPRESSION_SCRIPT,
    BrowserPoolManager,
    SpatialConfig,
)

# ── Config defaults: the flag exists, OFF, in BOTH shapes ────────


class TestStealthConfigDefaults:
    def test_dataclass_default_off(self):
        assert SpatialConfig().stealth_enabled is False

    def test_pydantic_default_off(self):
        assert ParousiaConfig().spatial.stealth_enabled is False

    def test_pydantic_yaml_roundtrip(self, tmp_path):
        """The flag is settable from a YAML file (the operator path)."""
        import yaml

        from parousia.config import load_config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "domain": "test.example.com",
                    "spatial": {"stealth_enabled": True},
                }
            )
        )
        cfg = load_config(cfg_file)
        assert cfg.spatial.stealth_enabled is True


# ── Gating (mocked playwright — runs everywhere) ────────────────


def _mock_browser():
    from unittest.mock import MagicMock

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
    return pw, browser, page


def _init_scripts(config, tmp_path):
    """Drive get_browser with mocked playwright; collect init scripts."""
    import dataclasses

    cfg = config
    if not hasattr(cfg, "model_copy"):
        cfg = dataclasses.replace(cfg, profile_dir=str(tmp_path))
    else:
        cfg = cfg.model_copy(update={"profile_dir": str(tmp_path)})
    manager = BrowserPoolManager(cfg)
    pw, _, _ = _mock_browser()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "parousia.spatial.browser_pool.async_playwright",
            lambda: pw,
            raising=False,
        )
        asyncio.run(manager.get_browser("agent-x"))
    return [c.args[0] for c in browser_add_init_script_calls(pw)]


def browser_add_init_script_calls(pw):
    """The add_init_script calls issued on the mocked context."""
    return (
        pw.chromium.launch_persistent_context.return_value.add_init_script.call_args_list
    )


class TestStealthGating:
    def test_default_injects_suppression_not_stealth(self, tmp_path):
        """stealth_enabled=False (default) → today's behavior exactly:
        the plain webdriver-suppression script, NOT the stealth script."""
        scripts = _init_scripts(SpatialConfig(), tmp_path)
        assert _WEBDRIVER_SUPPRESSION_SCRIPT in scripts
        assert _STEALTH_SCRIPT not in scripts

    def test_enabled_injects_stealth_not_suppression(self, tmp_path):
        """stealth_enabled=True → the stealth script replaces the plain
        suppression script (webdriver is undefined, not false — the
        suppression getter would keep it === false)."""
        import dataclasses

        cfg = dataclasses.replace(SpatialConfig(), stealth_enabled=True)
        scripts = _init_scripts(cfg, tmp_path)
        assert _STEALTH_SCRIPT in scripts
        assert _WEBDRIVER_SUPPRESSION_SCRIPT not in scripts

    def test_launch_kwargs_unchanged_by_flag(self, tmp_path):
        """The flag only selects the init script — launch kwargs (UA /
        viewport / locale / timezone / AutomationControlled flag) are
        identical either way (identity is MP-3, always on)."""
        import dataclasses

        kw_off = asyncio.run(
            _launch_kwargs(SpatialConfig(), tmp_path / "off")
        )
        kw_on = asyncio.run(
            _launch_kwargs(
                dataclasses.replace(SpatialConfig(), stealth_enabled=True),
                tmp_path / "on",
            )
        )
        kw_off.pop("user_data_dir")
        kw_on.pop("user_data_dir")
        assert kw_off == kw_on
        assert kw_off["user_agent"] == SpatialConfig().user_agent
        assert "--disable-blink-features=AutomationControlled" in kw_off["args"]


async def _launch_kwargs(config, tmp_path):
    pw, _, _ = _mock_browser()
    manager = BrowserPoolManager(_cfg_with_profile(config, tmp_path))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "parousia.spatial.browser_pool.async_playwright",
            lambda: pw,
            raising=False,
        )
        await manager.get_browser("agent-x")
    return pw.chromium.launch_persistent_context.call_args.kwargs


def _cfg_with_profile(config, tmp_path):
    import dataclasses

    if hasattr(config, "model_copy"):
        return config.model_copy(update={"profile_dir": str(tmp_path)})
    return dataclasses.replace(config, profile_dir=str(tmp_path))


# ── Real-browser gate (skipped without playwright + Chromium) ────


def _real_browser_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
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
class TestStealthAgainstRealBrowser:
    """The DONE gate, against real headless Chromium (like the pool)."""

    def _pool(self, tmp_path, stealth: bool) -> BrowserPoolManager:
        import dataclasses

        cfg = dataclasses.replace(
            SpatialConfig(), profile_dir=str(tmp_path / "profiles"),
            stealth_enabled=stealth,
        )
        return BrowserPoolManager(cfg)

    async def _signals(self, pool) -> dict:
        inst = await pool.get_browser("agent-gate")
        page = inst.page
        return await page.evaluate(
            """(() => ({
              webdriver: navigator.webdriver,
              webdriverIsUndefined: navigator.webdriver === undefined,
              chromePresent:
                typeof window.chrome === 'object' && window.chrome !== null,
              chromeRuntime: !!(window.chrome && window.chrome.runtime),
              pluginsLength: navigator.plugins.length,
              languages: navigator.languages,
              platform: navigator.platform,
              ua: navigator.userAgent,
              uaHasHeadless: /headless/i.test(navigator.userAgent),
              historyLength: window.history.length,
            }))()"""
        )

    async def _webgl(self, pool) -> dict:
        inst = await pool.get_browser("agent-gate")
        page = inst.page
        return await page.evaluate(
            """(() => {
              const c = document.createElement('canvas');
              const gl2 = c.getContext('webgl2');
              const gl = gl2 || c.getContext('webgl');
              if (!gl) return { webgl: null };
              const dbg = gl.getExtension('WEBGL_debug_renderer_info');
              return {
                webgl: true,
                vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
                renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
                versionOk:
                  typeof gl.getParameter(gl.VERSION) === 'string' &&
                  gl.getParameter(gl.VERSION).length > 0,
              };
            })()"""
        )

    @pytest.mark.asyncio
    async def test_done_gate_stealth_on(self, tmp_path):
        pool = self._pool(tmp_path, stealth=True)
        try:
            sig = await self._signals(pool)
            # ── the DONE gate, verbatim ──
            assert sig["webdriverIsUndefined"] is True, sig
            assert sig["pluginsLength"] > 0, sig
            assert sig["chromePresent"] is True, sig
            assert sig["uaHasHeadless"] is False, sig
            # ── the rest of the hardened surface ──
            assert sig["chromeRuntime"] is True, sig
            assert sig["platform"] == "Win32", sig
            assert sig["languages"] and sig["languages"][0] == "en-US", sig
            assert sig["historyLength"] == 2, sig
            assert sig["ua"] == DEFAULT_CHROME_UA, sig
            webgl = await self._webgl(pool)
            assert webgl["webgl"] is True, webgl
            assert webgl["vendor"] == "Google Inc. (NVIDIA)", webgl
            assert "GTX 1050" in (webgl["renderer"] or ""), webgl
            assert webgl["versionOk"] is True, webgl  # passthrough intact
        finally:
            await pool.shutdown_all()

    @pytest.mark.asyncio
    async def test_done_gate_stealth_off(self, tmp_path):
        """Default config: suppression script behavior — webdriver is
        FALSE (not undefined), and window.chrome is absent (the plain
        suppression script does not fake it)."""
        pool = self._pool(tmp_path, stealth=False)
        try:
            sig = await self._signals(pool)
            assert sig["webdriver"] is False, sig
            assert sig["uaHasHeadless"] is False, sig
            assert sig["pluginsLength"] > 0, sig  # suppression script does this
            assert sig["chromePresent"] is False, sig
        finally:
            await pool.shutdown_all()