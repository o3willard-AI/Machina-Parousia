from dataclasses import dataclass, field
from pathlib import Path
import atexit
import asyncio
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

    async def get_browser(self, agent_id: str) -> BrowserInstance:
        """Get a browser instance for the given agent ID.

        Re-validates liveness of a cached instance and relaunches if it has
        died, so callers never receive a stale/poisoned browser.
        """
        if agent_id in self._browsers:
            browser_instance = self._browsers[agent_id]
            if browser_instance.is_alive():
                browser_instance.last_used_at = time.time()
                return browser_instance
            # Cached browser has died (crashed Chromium / stuck driver) —
            # close it and fall through to a fresh launch.
            await self._close_instance(agent_id)

        # Enforce max_instances
        if len(self._browsers) >= self.config.max_instances:
            raise RuntimeError(f"Max instances ({self.config.max_instances}) reached")

        # Check if profile is locked
        profile_dir = Path(self.config.profile_dir) / agent_id
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
                if lock_agent_id == agent_id:
                    # Lock is for this agent, check if process is still alive
                    try:
                        os.kill(pid, 0)  # This will raise OSError if process doesn't exist
                        # Process exists, raise error
                        raise ProfileInUseError(f"Profile for agent {agent_id} is already in use by PID {pid}")
                    except OSError:
                        # Process doesn't exist, clear the stale lock
                        pass
                else:
                    # Lock is for a different agent
                    try:
                        os.kill(pid, 0)  # Check if process exists
                        # Process exists, raise error
                        raise ProfileInUseError(f"Profile for agent {agent_id} is already in use by PID {pid}")
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
            'agent_id': agent_id,
            'timestamp': time.time()
        }

        try:
            with open(lock_file, 'w') as f:
                json.dump(lock_data, f)
            self._lock_files[agent_id] = lock_file
        except Exception:
            # If we can't create the lock file, we can't proceed safely
            raise ProfileInUseError(f"Could not create lock file for agent {agent_id}")

        # Try to launch browser up to 3 times
        for attempt in range(3):
            try:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=True,
                    args=self.config.launch_args
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

        # Create the BrowserInstance
        browser_instance = BrowserInstance(
            agent_id=agent_id,
            playwright=playwright,
            browser=browser,
            context=browser,
            page=page,
            profile_dir=profile_dir,
            launched_at=time.time(),
            last_used_at=time.time(),
            pid=os.getpid()
        )

        self._browsers[agent_id] = browser_instance
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
