from dataclasses import dataclass, field
from pathlib import Path
import atexit
import asyncio
import hashlib
import shutil
import os
import time
import json
import signal
from typing import Optional

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

@dataclass
class SpatialConfig:
    chromium_path: str = "/usr/bin/chromium-browser"
    profile_dir: str = "/var/lib/parousia/browsers"
    idle_timeout_seconds: int = 300
    max_instances: int = 10
    launch_args: list = field(default_factory=lambda: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--headless=new"])
    # ── MP-3 (part A): identity & egress control (issue #16). Kept in
    # sync with parousia.config.SpatialConfig (the canonical pydantic
    # config used by the MCP server); this dataclass is the standalone/
    # legacy shape used by the CLI. ──
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    )
    viewport_width: int = 1280
    viewport_height: int = 800
    locale: str = "en-US"
    timezone_id: str = "America/Los_Angeles"
    # agent_id -> proxy URL; default egress proxy; "" = direct (off).
    proxy_servers: dict = field(default_factory=dict)
    proxy_default: str = ""
    # MP-10a: named proxy registry — name -> proxy URL, a MENU of
    # available transports (never a routing rule). Kept in sync with
    # parousia.config.SpatialConfig (the canonical pydantic config).
    proxies: dict = field(default_factory=dict)
    # MP-9: full fingerprint hardening — opt-in, OFF by default. Kept in
    # sync with parousia.config.SpatialConfig (the canonical pydantic
    # config); see the comment there.
    stealth_enabled: bool = False

# MP-3: suppress the automation-controlled signals on every page. Blink's
# AutomationControlled feature is disabled at launch (a launch arg, below);
# this init script additionally normalizes the JS-visible surface.
_WEBDRIVER_SUPPRESSION_SCRIPT = """
(() => {
  // navigator.webdriver: the canonical headless tell (set by Blink's
  // automation; --disable-blink-features=AutomationControlled already
  // suppresses most of it — this pins the JS view to `false`).
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => false, configurable: true,
    });
    // Normal-looking languages (matches the configured en-US locale).
    Object.defineProperty(navigator, 'languages', {
      get: () => ['en-US', 'en'], configurable: true,
    });
    // A non-empty plugins array (headless reports zero plugins).
    Object.defineProperty(navigator, 'plugins', {
      get: () => {
        const arr = [1, 2, 3, 4, 5].map((i) => ({ name: `Plugin ${i}` }));
        arr.item = (i) => arr[i] || null;
        arr.namedItem = (n) => arr.find((p) => p.name === n) || null;
        return arr;
      },
      configurable: true,
    });
  } catch (_) { /* page overrode it — best effort */ }
})();

"""

# ── MP-9 (issue #15): FULL fingerprint hardening — config-gated behind
# SpatialConfig.stealth_enabled (opt-in, OFF by default). Covers the
# fingerprint surface the plain suppression script does not touch:
#   - navigator.webdriver → UNDEFINED (not false — headless detection
#     commonly tests === undefined after defineProperty tricks)
#   - window.chrome → present with a minimal realistic shape (runtime et al)
#   - navigator.plugins/languages → non-empty + realistic
#   - navigator.platform → desktop value matching the UA ("Win32")
#   - WebGL UNMASKED_VENDOR/RENDERER → realistic desktop GPU strings (not
#     headless SwiftShader); all other getParameter calls pass through
#   - window.history.length → small non-zero
# Deliberately NOT here (per the brief's scope decision): canvas/audio
# noise fingerprinting — the listed JS-surface signals only.
# Verified live against real headless Chromium before shipping.
_STEALTH_SCRIPT = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined, configurable: true,
    });
  } catch (_) {}
  try {
    if (!window.chrome || !window.chrome.runtime) {
      window.chrome = {
        runtime: {},
        loadTimes: function loadTimes() {},
        csi: function csi() {},
        app: {},
      };
    }
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => {
        const mk = (name) => {
          const p = {
            name, filename: 'pdfium.pdf',
            description: 'Portable Document Format', length: 1,
          };
          p[0] = { type: 'application/pdf', suffixes: 'pdf', description: 'PDF' };
          return p;
        };
        const arr = [
          mk('PDF Viewer'),
          mk('Chrome PDF Viewer'),
          mk('Chromium PDF Viewer'),
          mk('Microsoft Edge PDF Viewer'),
          mk('WebKit built-in PDF'),
        ];
        arr.item = (i) => arr[i] || null;
        arr.namedItem = (n) => arr.find((p) => p.name === n) || null;
        return arr;
      },
      configurable: true,
    });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['en-US', 'en'], configurable: true,
    });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'platform', {
      get: () => 'Win32', configurable: true,
    });
  } catch (_) {}
  try {
    const VENDOR = 0x9245;    // UNMASKED_VENDOR_WEBGL
    const RENDERER = 0x9246;  // UNMASKED_RENDERER_WEBGL
    const fakeVendor = 'Google Inc. (NVIDIA)';
    const fakeRenderer =
      'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 ' +
      'Direct3D11 vs_5_0 ps_5_0, D3D11)';
    const patch = (proto) => {
      if (!proto || !proto.getParameter) return;
      const orig = proto.getParameter;
      proto.getParameter = function getParameter(param) {
        if (param === VENDOR) return fakeVendor;
        if (param === RENDERER) return fakeRenderer;
        return orig.call(this, param);
      };
    };
    patch(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
    patch(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);
  } catch (_) {}
  try {
    Object.defineProperty(window.history, 'length', {
      get: () => 2, configurable: true,
    });
  } catch (_) {}
})();
"""

class ProfileInUseError(Exception):
    """Raised when a profile is already in use by another agent."""
    pass

class BrowserUnavailableError(Exception):
    """Raised when a browser instance cannot be launched."""
    pass

@dataclass
class BrowserInstance:
    """Represents a single browser instance with its associated data.

    Note on the ``browser`` / ``context`` fields: Playwright's
    ``launch_persistent_context`` returns a *BrowserContext* (there is no
    separate Browser object for a persistent context). Both ``browser`` and
    ``context`` therefore alias the same persistent context — callers should
    use ``context`` for semantic clarity (e.g. ``context.new_page()``). The
    parent Browser is reachable via ``context.browser``.
    """
    agent_id: str
    playwright: object
    browser: object
    context: object
    page: object
    profile_dir: Path
    launched_at: float
    last_used_at: float
    pid: int
    # MP-3: the identity this browser presents (effective UA after any
    # per-navigation override is applied by the tools layer, not here).
    user_agent: str = ""

    def is_alive(self) -> bool:
        """Check if the browser is still connected and alive.

        ``self.browser`` / ``self.context`` hold the persistent *BrowserContext*;
        ``BrowserContext`` has no ``is_connected()`` method, so liveness is read
        off the parent Browser via ``context.browser``.
        """
        try:
            browser = self.context.browser
            return browser is not None and browser.is_connected()
        except Exception:
            return False

class BrowserPoolManager:
    """Manages a pool of Chromium browser instances with persistent profiles.

    All Playwright interaction is async (``playwright.async_api``): the pool is
    driven from the Parousia MCP server, which runs inside an asyncio event
    loop. The synchronous API cannot be used there.

    Self-healing: ``get_browser`` re-validates a cached instance's liveness on
    every call and transparently relaunches if the browser has died, so a
    crashed/stuck Chromium does not poison the in-memory cache until a service
    restart.
    """

    # Upper bound on how long a single browser close/stop may take. A stuck
    # Playwright driver node never responds, so without this a cleanup could
    # hang indefinitely (e.g. atexit shutdown).
    CLOSE_TIMEOUT_SECONDS = 10.0

    def __init__(self, config: SpatialConfig):
        self.config = config
        self._browsers = {}  # agent_id -> BrowserInstance
        self._lock_files = {}  # agent_id -> lock file path

        # Register shutdown handler. `shutdown_all` is async, so register a
        # sync trampoline that drives it with asyncio.run() at exit.
        atexit.register(self._atexit_shutdown)

    def _atexit_shutdown(self):
        """Best-effort sync trampoline for the async `shutdown_all`."""
        try:
            asyncio.run(self.shutdown_all())
        except Exception:
            pass  # interpreter shutdown — browser child processes are OS-cleaned anyway

    async def _close_instance(self, agent_id: str):
        """Close one browser instance and clear its lock, if present.

        Safe to call for an agent with no cached instance. Close/stop are
        wrapped in a timeout so a stuck Playwright driver can't hang cleanup.
        """
        browser_instance = self._browsers.pop(agent_id, None)
        if browser_instance is not None:
            try:
                await asyncio.wait_for(browser_instance.browser.close(), timeout=self.CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
            try:
                await asyncio.wait_for(browser_instance.playwright.stop(), timeout=self.CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass

        lock_file = self._lock_files.pop(agent_id, None)
        if lock_file is not None and lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

    def _pool_key(self, agent_id: str, override_proxy: str = "") -> str:
        """MP-10a: the pool/profile key for a browser context.

        No ``override_proxy`` → the bare ``agent_id`` (today's behavior,
        byte-for-byte — the agent's DEFAULT context). An agent-CALLED
        named transport (resolved name->URL by the tools layer) gets its
        OWN persistent context keyed by agent + a short stable hash of
        the proxy URL: a profile dir is locked to one running Chromium
        instance, and the transport's session state (cookies etc.) stays
        isolated per (agent, proxy).
        """
        if not override_proxy:
            return agent_id
        digest = hashlib.sha256(override_proxy.encode("utf-8")).hexdigest()[:12]
        return f"{agent_id}__px-{digest}"

    def _launch_kwargs(self, agent_id: str, override_proxy: str = "") -> dict:
        """MP-3 (part A): per-agent launch kwargs for the identity + egress.

        Applies the configured identity (UA / viewport / locale / timezone)
        and the egress proxy selection. Called from ``get_browser`` only, so
        the identity is consistent for the life of the persistent context.

        MP-10a: ``override_proxy`` — a non-empty resolved proxy URL from the
        agent-CALLED ``proxy`` argument (a name from the ``spatial.proxies``
        menu). Non-empty → it OVERRIDES the operator mapping
        (``proxy_for``) for this fresh context. Empty (the default, when
        the agent did not ask) → the operator mapping applies exactly as
        before; a request with no ``proxy`` argument NEVER sets a proxy
        beyond the operator's own mapping.
        """
        cfg = self.config
        args = list(getattr(cfg, "launch_args", []) or [])
        # Webdriver suppression at the Blink level: the flag that stops
        # Chrome from flipping automation-controlled signals. Appended
        # (config launch_args may already carry it).
        flag = "--disable-blink-features=AutomationControlled"
        if flag not in args:
            args.append(flag)

        kwargs: dict = {
            "user_data_dir": None,  # set by caller (profile_dir)
            "headless": True,
            "args": args,
        }
        ua = getattr(cfg, "user_agent", "")
        if ua:
            kwargs["user_agent"] = ua
        width = int(getattr(cfg, "viewport_width", 0) or 0)
        height = int(getattr(cfg, "viewport_height", 0) or 0)
        if width > 0 and height > 0:
            kwargs["viewport"] = {"width": width, "height": height}
        if getattr(cfg, "locale", ""):
            kwargs["locale"] = cfg.locale
        if getattr(cfg, "timezone_id", ""):
            kwargs["timezone_id"] = cfg.timezone_id
        if override_proxy:
            # MP-10a: the agent CALLED this transport by name — it wins
            # over the operator mapping for this fresh context.
            kwargs["proxy"] = {"server": override_proxy}
        else:
            proxy = self.proxy_for(agent_id)
            if proxy:
                kwargs["proxy"] = {"server": proxy}
        return kwargs

    def proxy_for(self, agent_id: str) -> str:
        """MP-3: the egress proxy URL for an agent ("" = direct, default off).

        Config-driven per-agent selection: ``proxy_servers[agent_id]`` if
        mapped, else ``proxy_default`` ("" = direct egress).
        """
        servers = getattr(self.config, "proxy_servers", None) or {}
        explicit = servers.get(agent_id)
        if explicit:
            return explicit
        return getattr(self.config, "proxy_default", "") or ""

    async def get_browser(self, agent_id: str, override_proxy: str = "") -> BrowserInstance:
        """Get a browser instance for the given agent ID.

        Re-validates liveness of a cached instance and relaunches if it has
        died, so callers never receive a stale/poisoned browser.

        MP-10a (issue #11 ws1, chunk 1): ``override_proxy`` — an
        agent-CALLED, per-REQUEST transport override (a proxy URL resolved
        by NAME from the ``spatial.proxies`` menu by the tools layer).
        Non-empty → a separate persistent context is launched through
        that proxy, keyed by (agent, proxy) — see ``_pool_key``. Empty
        (the default — the agent sent no ``proxy`` argument) → the
        agent's DEFAULT context, byte-for-byte today's behavior with the
        operator mapping (``proxy_for``) intact.
        """
        key = self._pool_key(agent_id, override_proxy)
        if key in self._browsers:
            browser_instance = self._browsers[key]
            if browser_instance.is_alive():
                browser_instance.last_used_at = time.time()
                return browser_instance
            # Cached browser has died (crashed Chromium / stuck driver) —
            # close it and fall through to a fresh launch.
            await self._close_instance(key)

        # Enforce max_instances
        if len(self._browsers) >= self.config.max_instances:
            raise RuntimeError(f"Max instances ({self.config.max_instances}) reached")

        # Check if profile is locked
        profile_dir = Path(self.config.profile_dir) / key
        lock_file = profile_dir / "profile.lock"

        # Check for existing lock file
        if lock_file.exists():
            try:
                with open(lock_file, 'r') as f:
                    lock_data = json.load(f)

                pid = lock_data.get('pid')
                lock_agent_id = lock_data.get('agent_id')
                timestamp = lock_data.get('timestamp', 0)

                # Check if the lock is for this agent
                if lock_agent_id == key:
                    # Lock is for this agent, check if process is still alive
                    try:
                        os.kill(pid, 0)  # This will raise OSError if process doesn't exist
                        # Process exists, raise error
                        raise ProfileInUseError(f"Profile for agent {key} is already in use by PID {pid}")
                    except OSError:
                        # Process doesn't exist, clear the stale lock
                        pass
                else:
                    # Lock is for a different agent
                    try:
                        os.kill(pid, 0)  # Check if process exists
                        # Process exists, raise error
                        raise ProfileInUseError(f"Profile for agent {key} is already in use by PID {pid}")
                    except OSError:
                        # Process doesn't exist, clear the stale lock and proceed
                        pass

            except (json.JSONDecodeError, KeyError):
                # Invalid lock file, clear it
                pass

        # Clear any stale lock files
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass  # If we can't remove it, continue anyway

        # Create profile directory if it doesn't exist
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Create new lock file
        lock_data = {
            'pid': os.getpid(),
            'agent_id': key,
            'timestamp': time.time()
        }

        try:
            with open(lock_file, 'w') as f:
                json.dump(lock_data, f)
            self._lock_files[key] = lock_file
        except Exception:
            # If we can't create the lock file, we can't proceed safely
            raise ProfileInUseError(f"Could not create lock file for agent {key}")

        # Try to launch browser up to 3 times
        for attempt in range(3):
            try:
                playwright = await async_playwright().start()
                # MP-3 (part A): identity + egress kwargs per agent
                # (UA/viewport/locale/timezone from config; proxy selection
                # config-driven, default off). MP-10a: an agent-CALLED
                # named transport (override_proxy) replaces the operator
                # mapping for this context.
                launch_kwargs = self._launch_kwargs(agent_id, override_proxy)
                launch_kwargs["user_data_dir"] = str(profile_dir)
                browser = await playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )

                # Health check - create a page and make sure it's working
                try:
                    page = browser.pages[0] if browser.pages else await browser.new_page()
                    await page.goto("about:blank", timeout=3000)
                    title = await page.title()
                    # If we got here without exception, the browser is healthy
                    break
                except Exception:
                    # Health check failed, try again
                    try:
                        await browser.close()
                        await playwright.stop()
                    except Exception:
                        pass  # Ignore cleanup errors

            except Exception as e:
                if attempt < 2:  # Don't sleep on last attempt
                    time.sleep(1)
                else:
                    raise BrowserUnavailableError(f"Failed to launch browser after {attempt + 1} attempts: {str(e)}")

        # MP-3 (part A): webdriver suppression. The init script applies to
        # pages created AFTER it is registered, so close any pre-existing
        # page (the health-check page) and create a fresh one — the agent's
        # persistent page then reports navigator.webdriver === false.
        # MP-9 (issue #15): full fingerprint hardening is OPT-IN
        # (config.stealth_enabled, default False). Enabled → the stealth
        # script (webdriver → undefined, window.chrome, platform, WebGL,
        # history.length) replaces the plain suppression script. Disabled
        # (the default) → today's behavior, byte-for-byte.
        stealth = bool(getattr(self.config, "stealth_enabled", False))
        try:
            await browser.add_init_script(
                _STEALTH_SCRIPT if stealth else _WEBDRIVER_SUPPRESSION_SCRIPT
            )
        except Exception:
            pass  # best effort — the launch flag already did the heavy lift
        for existing in list(getattr(browser, "pages", [])):
            try:
                await existing.close()
            except Exception:
                pass
        page = await browser.new_page()

        # Create the BrowserInstance
        browser_instance = BrowserInstance(
            agent_id=key,
            playwright=playwright,
            browser=browser,
            context=browser,
            page=page,
            profile_dir=profile_dir,
            launched_at=time.time(),
            last_used_at=time.time(),
            pid=os.getpid(),
            user_agent=getattr(self.config, "user_agent", "") or "",
        )

        self._browsers[key] = browser_instance
        return browser_instance

    def release_browser(self, agent_id: str):
        """Release a browser instance for the given agent ID, marking it as idle."""
        if agent_id in self._browsers:
            self._browsers[agent_id].last_used_at = time.time()

    async def _idle_cleanup(self):
        """Close browsers that have been idle longer than idle_timeout_seconds."""
        current_time = time.time()
        to_remove = []

        for agent_id, browser_instance in self._browsers.items():
            if current_time - browser_instance.last_used_at > self.config.idle_timeout_seconds:
                to_remove.append(agent_id)

        for agent_id in to_remove:
            await self._close_instance(agent_id)

    async def shutdown_all(self):
        """Close all browser instances."""
        for agent_id in list(self._browsers.keys()):
            await self._close_instance(agent_id)

        # Clear any remaining lock files (e.g. locks whose browser was never
        # launched, or leftover entries not tracked in _browsers).
        for lock_file in list(self._lock_files.values()):
            try:
                if lock_file.exists():
                    lock_file.unlink()
            except Exception:
                pass
        self._lock_files.clear()

    def status(self) -> dict:
        """Get status information about all browsers."""
        result = {}
        current_time = time.time()

        for agent_id, browser_instance in self._browsers.items():
            profile_size_mb = 0
            try:
                # Calculate profile size
                if browser_instance.profile_dir.exists():
                    profile_size_mb = sum(f.stat().st_size for f in browser_instance.profile_dir.rglob('*') if f.is_file()) / (1024 * 1024)
            except Exception:
                pass

            idle_seconds = current_time - browser_instance.last_used_at

            result[agent_id] = {
                'state': 'idle' if idle_seconds > self.config.idle_timeout_seconds else 'active',
                'profile_size_mb': profile_size_mb,
                'idle_seconds': idle_seconds
            }

        return result

    async def cleanup_profile(self, agent_id: str):
        """Remove the profile directory for an agent."""
        profile_dir = Path(self.config.profile_dir) / agent_id

        # Close browser if it exists (also clears its lock)
        await self._close_instance(agent_id)

        # Remove any lock file that may still be present
        lock_file = profile_dir / "profile.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

        # Remove the entire profile directory
        if profile_dir.exists():
            try:
                shutil.rmtree(profile_dir)
            except Exception:
                pass
