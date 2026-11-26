"""MP-10b (issue #11 ws1, chunk 2 of 3): managed Tor transport.

Covers the [DONE] gate:
- ``proxy="tor"`` + ``tor_enabled=true`` launches Tor (``--SOCKSPort``,
  own ``--DataDirectory``, ``--RunAsDaemon 0``), waits for the SOCKS
  port, and routes the request through ``socks5://127.0.0.1:<port>``
  (chunk 1's per-transport context);
- a subsequent ``proxy="tor"`` request REUSES the process (no second
  launch);
- ``tor_enabled=false`` errors explicitly;
- a missing ``tor`` binary errors LOUDLY (and never yields a direct
  override);
- bootstrap timeout errors loudly (process killed);
- ``tor`` never resolves to direct egress or an empty override;
- real-browser (skipif playwright) ``proxy="tor"`` with
  ``tor_enabled=false`` returns the explicit error without touching
  the pool.

The ``tor`` binary is NOT installed on this machine — every launch
path here is fully mocked (``subprocess.Popen`` / ``shutil.which`` /
socket bootstrap), so the suite stays hermetic and deterministic.
"""

import asyncio
import dataclasses
import json
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from parousia.config import ParousiaConfig, load_config
from parousia.spatial import tor as tor_mod
from parousia.spatial.browser_pool import BrowserPoolManager, SpatialConfig
from parousia.spatial.tor import (
    TorBootstrapError,
    TorManager,
    TorUnavailableError,
)
from parousia.spatial.tools import SpatialToolHandlers

PROXIES = {"residential-1": "http://user:pass@gate.example:8080"}


# ── Config ───────────────────────────────────────────────────────


class TestTorConfig:
    def test_defaults_both_shapes(self):
        assert ParousiaConfig().spatial.tor_enabled is False
        assert ParousiaConfig().spatial.tor_socks_port == 9050
        assert SpatialConfig().tor_enabled is False
        assert SpatialConfig().tor_socks_port == 9050

    def test_yaml_roundtrip(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "domain": "test.example.com",
                    "spatial": {"tor_enabled": True, "tor_socks_port": 9150},
                }
            )
        )
        cfg = load_config(cfg_file)
        assert cfg.spatial.tor_enabled is True
        assert cfg.spatial.tor_socks_port == 9150


# ── TorManager (mocked binary/Popen/socket) ──────────────────────


def _tor_cfg(**kw):
    base = {"tor_socks_port": 9050, "profile_dir": "/tmp/parousia-test-profiles"}
    base.update(kw)
    return dataclasses.replace(SpatialConfig(), **base)


def _mock_which(path="/usr/bin/tor"):
    return patch("parousia.spatial.tor.shutil.which", return_value=path)


def _mock_popen():
    proc = MagicMock()
    proc.poll = MagicMock(return_value=None)  # alive forever unless changed
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = MagicMock(return_value=0)
    popen = MagicMock(return_value=proc)
    return popen, proc


def _port_accepts(value=True):
    return patch("parousia.spatial.tor.socket.create_connection", new=_fake_connect(value))


class _fake_connect:
    """Stand-in for socket.create_connection with a settable outcome."""

    def __init__(self, accepting):
        self._accepting = accepting

    def __call__(self, *a, **kw):
        if self._accepting:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=None)
            cm.__exit__ = MagicMock(return_value=False)
            return cm
        raise OSError("refused")


class TestTorManager:
    def test_ensure_running_launches_and_bootstraps(self, tmp_path):
        popen, proc = _mock_popen()
        with _mock_which(), patch("parousia.spatial.tor.subprocess.Popen", popen), _port_accepts(True):
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            url = mgr.ensure_running()
        assert url == "socks5://127.0.0.1:9050"
        argv = popen.call_args.args[0]
        assert argv[0] == "/usr/bin/tor"
        assert "--SOCKSPort" in argv and "9050" in argv
        assert "--RunAsDaemon" in argv and "0" in argv
        # Own data directory under the Parousia data dir (profile parent).
        di = argv.index("--DataDirectory")
        assert argv[di + 1] == str(mgr.data_dir)
        from pathlib import Path

        assert mgr.data_dir == Path(str(tmp_path / "profiles")).parent / "tor"

    def test_second_call_reuses_process(self, tmp_path):
        popen, proc = _mock_popen()
        with _mock_which(), patch("parousia.spatial.tor.subprocess.Popen", popen), _port_accepts(True):
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            first = mgr.ensure_running()
            second = mgr.ensure_running()
        assert first == second == "socks5://127.0.0.1:9050"
        assert popen.call_count == 1, "reuse: no second launch"

    def test_missing_binary_errors_loudly(self, tmp_path):
        with _mock_which(None), _port_accepts(True):
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            with pytest.raises(TorUnavailableError) as exc:
                mgr.ensure_running()
        assert "install `tor`" in str(exc.value)
        assert mgr._process is None, "no process created"

    def test_bootstrap_timeout_errors_loudly_and_kills(self, tmp_path):
        popen, proc = _mock_popen()
        with _mock_which(), patch("parousia.spatial.tor.subprocess.Popen", popen), _port_accepts(False), \
             patch.object(tor_mod, "BOOTSTRAP_TIMEOUT_SECONDS", 0.2), \
             patch.object(tor_mod, "BOOTSTRAP_POLL_SECONDS", 0.01):
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            with pytest.raises(TorBootstrapError) as exc:
                mgr.ensure_running()
        assert "did not bootstrap a SOCKS listener on 127.0.0.1:9050" in str(exc.value)
        proc.terminate.assert_called()  # timed-out process is terminated

    def test_dead_process_restarts_on_next_call(self, tmp_path):
        popen, proc = _mock_popen()
        with _mock_which(), patch("parousia.spatial.tor.subprocess.Popen", popen), _port_accepts(True):
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            mgr.ensure_running()
            # The process died since (crash / OOM kill). The RELAUNCH must
            # get a fresh process object — not the dead mock.
            proc.poll = MagicMock(return_value=1)
            _, fresh_proc = _mock_popen()
            popen.return_value = fresh_proc
            url = mgr.ensure_running()
        assert url == "socks5://127.0.0.1:9050"
        assert popen.call_count == 2, "dead process is relaunched, not reused"

    def test_stop_terminates(self, tmp_path):
        popen, proc = _mock_popen()
        with _mock_which(), patch("parousia.spatial.tor.subprocess.Popen", popen), _port_accepts(True):
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            mgr.ensure_running()
            mgr.stop()
        proc.terminate.assert_called_once()
        assert mgr._process is None
        # stop() is safe when nothing runs.
        mgr.stop()

    def test_data_dir_failure_is_loud(self, tmp_path):
        # A data dir path that cannot be created (occupied by a FILE)
        # fails loudly, never silently direct.
        blocker = tmp_path / "tor"
        blocker.write_text("in the way")
        with _mock_which():
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            assert mgr.data_dir == blocker
            with pytest.raises(TorUnavailableError) as exc:
                mgr.ensure_running()
        assert "cannot create Tor data directory" in str(exc.value)

    def test_spawn_failure_is_loud(self, tmp_path):
        with _mock_which(), patch("parousia.spatial.tor.subprocess.Popen", side_effect=OSError("no exec")), \
             _port_accepts(True):
            mgr = TorManager(_tor_cfg(profile_dir=str(tmp_path / "profiles")))
            with pytest.raises(TorUnavailableError) as exc:
                mgr.ensure_running()
        assert "failed to start tor" in str(exc.value)


# ── Pool ownership (lifecycle matches the pool's) ────────────────


class TestPoolOwnership:
    def test_tor_manager_lazy_and_stable(self):
        pool = BrowserPoolManager(SpatialConfig())
        assert pool._tor_manager is None, "not constructed eagerly"
        mgr = pool.tor_manager()
        assert isinstance(mgr, TorManager)
        assert pool.tor_manager() is mgr, "same instance on subsequent calls"

    def test_shutdown_all_stops_tor(self):
        pool = BrowserPoolManager(SpatialConfig())
        mgr = MagicMock()
        pool._tor_manager = mgr
        asyncio.run(pool.shutdown_all())
        mgr.stop.assert_called_once()

    def test_shutdown_without_tor_is_safe(self):
        pool = BrowserPoolManager(SpatialConfig())
        asyncio.run(pool.shutdown_all())  # no tor manager — no error


# ── Resolution + handlers ────────────────────────────────────────


def _handlers(cfg, pool=None):
    pool = pool or MagicMock()
    page = MagicMock()
    page.content = AsyncMock(return_value="<html><body></body></html>")
    page.evaluate = AsyncMock(return_value="UA")
    page.goto = AsyncMock(return_value=None)
    for act in ("click", "type", "scroll_into_view", "select_option",
                "check", "uncheck", "hover", "press"):
        setattr(page, act, AsyncMock())
    browser = MagicMock()
    browser.page = page
    pool.get_browser = AsyncMock(return_value=browser)
    pool.proxy_for = MagicMock(return_value="")
    pool.tor_manager = MagicMock(return_value=MagicMock())
    serializer = MagicMock()
    sdom = MagicMock()
    sdom.model_dump.return_value = {"type": "sdom"}
    serializer.to_sdom.return_value = sdom
    return SpatialToolHandlers(cfg, pool, serializer), pool


def _dispatch(handlers, name, arguments, agent_id="agent-x"):
    return json.loads(asyncio.run(handlers.dispatch(name, arguments, agent_id)))


def _cfg(spatial_update=None):
    cfg = ParousiaConfig()
    if spatial_update:
        cfg = cfg.model_copy(
            update={"spatial": cfg.spatial.model_copy(update=spatial_update)}
        )
    return cfg


class TestTorResolution:
    def test_tor_disabled_errors_explicitly(self):
        handlers, pool = _handlers(_cfg({"proxies": PROXIES}))
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        assert result == {"error": "Tor is not enabled (set spatial.tor_enabled=true) to use proxy='tor'"}
        pool.tor_manager.assert_not_called()  # never touched when disabled
        pool.get_browser.assert_not_awaited()

    def test_tor_disabled_interact_too(self):
        handlers, pool = _handlers(_cfg())
        result = _dispatch(handlers, "interact", {"id": "el", "action": "click", "proxy": "tor"})
        assert "Tor is not enabled" in result["error"]
        pool.get_browser.assert_not_awaited()

    def test_tor_enabled_launches_and_routes(self):
        cfg = _cfg({"tor_enabled": True, "tor_socks_port": 9050})
        handlers, pool = _handlers(cfg)
        tm = pool.tor_manager.return_value
        tm.ensure_running = MagicMock(return_value="socks5://127.0.0.1:9050")
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        tm.ensure_running.assert_called_once()
        pool.get_browser.assert_awaited_once_with(
            "agent-x", override_proxy="socks5://127.0.0.1:9050"
        )
        assert result["extracted"] is True
        # Reported by NAME (chunk 1 rule: never the URL).
        assert result["proxy"] == "tor"
        assert result["egress"]["proxy"] == "tor"

    def test_tor_enabled_interact_routes_through_transport(self):
        cfg = _cfg({"tor_enabled": True})
        handlers, pool = _handlers(cfg)
        tm = pool.tor_manager.return_value
        tm.ensure_running = MagicMock(return_value="socks5://127.0.0.1:9150")
        result = _dispatch(handlers, "interact", {"id": "el", "action": "click", "proxy": "tor"})
        pool.get_browser.assert_awaited_once_with(
            "agent-x", override_proxy="socks5://127.0.0.1:9150"
        )
        assert result["proxy"] == "tor"

    def test_tor_unavailable_errors_never_direct(self):
        cfg = _cfg({"tor_enabled": True})
        handlers, pool = _handlers(cfg)
        tm = pool.tor_manager.return_value
        tm.ensure_running = MagicMock(
            side_effect=TorUnavailableError(
                "Tor binary not found; install `tor` to use proxy='tor' "
                "(Debian/Ubuntu: apt install tor)"
            )
        )
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        assert result["error"].startswith("Tor transport unavailable: Tor binary not found")
        pool.get_browser.assert_not_awaited()  # never a direct fallback

    def test_tor_bootstrap_failure_errors_never_direct(self):
        cfg = _cfg({"tor_enabled": True})
        handlers, pool = _handlers(cfg)
        tm = pool.tor_manager.return_value
        tm.ensure_running = MagicMock(
            side_effect=TorBootstrapError(
                "Tor did not bootstrap a SOCKS listener on 127.0.0.1:9050 within 30s"
            )
        )
        result = _dispatch(handlers, "interact", {"id": "el", "action": "click", "proxy": "tor"})
        assert "Tor transport unavailable" in result["error"]
        assert "bootstrap" in result["error"]
        pool.get_browser.assert_not_awaited()

    def test_tor_empty_url_refused(self):
        """Belt-and-braces: even a broken transport returning '' must
        not resolve tor to a direct/empty override."""
        cfg = _cfg({"tor_enabled": True})
        handlers, pool = _handlers(cfg)
        tm = pool.tor_manager.return_value
        tm.ensure_running = MagicMock(return_value="")
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        assert result["error"] == (
            "Tor transport unavailable: no SOCKS URL (refusing direct egress)"
        )
        pool.get_browser.assert_not_awaited()

    def test_tor_reserved_over_menu_entry(self):
        """A operator menu entry named 'tor' cannot shadow the managed
        transport (reserved name semantics)."""
        cfg = _cfg({"tor_enabled": False, "proxies": {"tor": "http://not-tor:1"}})
        handlers, pool = _handlers(cfg)
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        assert "Tor is not enabled" in result["error"]
        pool.get_browser.assert_not_awaited()

    def test_custom_port_from_config(self):
        cfg = _cfg({"tor_enabled": True, "tor_socks_port": 9150})
        handlers, pool = _handlers(cfg)
        tm = pool.tor_manager.return_value
        tm.ensure_running = MagicMock(return_value="socks5://127.0.0.1:9150")
        _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        pool.get_browser.assert_awaited_once_with(
            "agent-x", override_proxy="socks5://127.0.0.1:9150"
        )

    def test_other_names_unaffected(self):
        handlers, pool = _handlers(_cfg({"proxies": PROXIES}))
        _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "residential-1"})
        pool.tor_manager.assert_not_called()
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "zzz"})
        assert "unknown proxy name 'zzz'" in result["error"]


# ── Real-browser gate (skipif playwright) ────────────────────────


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
class TestTorRealBrowserGate:
    def test_tor_disabled_explicit_error_pool_untouched(self, tmp_path):
        """Real pool: proxy="tor" with tor_enabled=false returns the
        explicit error and NEVER touches the pool (no browser, no tor
        subprocess — the tor binary is absent on this machine, so any
        accidental launch would fail loudly anyway)."""
        cfg = dataclasses.replace(
            SpatialConfig(),
            profile_dir=str(tmp_path / "profiles"),
            tor_enabled=False,
        )
        pool = BrowserPoolManager(cfg)
        handlers = SpatialToolHandlers(
            _cfg({"tor_enabled": False}), pool, _real_serializer()
        )
        result = _dispatch(handlers, "browse_to", {"url": "https://example.com", "proxy": "tor"})
        assert result == {"error": "Tor is not enabled (set spatial.tor_enabled=true) to use proxy='tor'"}
        assert pool._browsers == {}, "no browser launched"
        assert pool._tor_manager is None, "no tor manager constructed"


def _real_serializer():
    from parousia.spatial.serializer import SpatialSerializer

    return SpatialSerializer()


@pytest.mark.asyncio
async def test_shutdown_teardown_integration():
    """Full pool → tor_manager → stop() chain with a mocked manager:
    constructing the manager then tearing the pool down stops it."""
    pool = BrowserPoolManager(SpatialConfig())
    with _mock_which(None):
        mgr = pool.tor_manager()
    assert isinstance(mgr, TorManager)
    pool._tor_manager = MagicMock()
    await pool.shutdown_all()
    pool._tor_manager.stop.assert_called_once()