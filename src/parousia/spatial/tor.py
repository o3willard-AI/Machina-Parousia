"""MP-10b (issue #11 ws1, chunk 2 of 3): managed Tor transport.

`TorManager` owns the `tor` subprocess — the self-hosted free-fallback
transport, registered as the reserved name ``"tor"`` in the
agent-called proxy selection (chunk 1).

Hard requirement — OPT-IN, AGENT-CALLED, FAIL-LOUD:
- Tor is NEVER started automatically: only an explicit ``proxy="tor"``
  request reaches this module, and only when ``spatial.tor_enabled`` is
  set (the tools layer enforces the flag; ``ensure_running`` is the
  only launcher).
- Every failure (binary missing, data dir unusable, subprocess spawn
  error, bootstrap timeout) raises a clear `TorError` subclass — NEVER
  a silent fallback to direct egress. The agent must not believe it is
  proxied when it isn't.

Purpose boundary: legitimate, low-volume, agent-scoped operation — one
account per purpose. Tor is opt-in and agent-called; direct egress is
the default and stays the default.

Lifecycle: a `TorManager` is owned by `BrowserPoolManager` (constructed
lazily via ``pool.tor_manager()``), so the subprocess lifecycle matches
the pool's — ``shutdown_all`` / atexit teardown stops it.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional


class TorError(Exception):
    """Base class for Tor transport failures.

    Every Tor failure is explicit — the tools layer converts these into
    a clear tool error; nothing here ever yields a direct-egress URL.
    """


class TorUnavailableError(TorError):
    """Tor cannot be used at all (binary missing / cannot spawn)."""


class TorBootstrapError(TorError):
    """Tor was launched but its SOCKS listener did not come up."""


# Bootstrap: how long to wait for the SOCKS port to accept connections
# after launch (and how often to poll). Tor typically needs a few
# seconds to bootstrap its circuits.
BOOTSTRAP_TIMEOUT_SECONDS = 30.0
BOOTSTRAP_POLL_SECONDS = 0.25


class TorManager:
    """Manages the `tor` subprocess as the ``"tor"`` transport backend.

    A single shared process serves every ``proxy="tor"`` request (the
    per-request isolation lives in the browser pool's per-transport
    persistent contexts — chunk 1's ``_pool_key``).
    """

    def __init__(self, config):
        self._port = int(getattr(config, "tor_socks_port", 9050) or 9050)
        # Own --DataDirectory under the Parousia data dir (the parent of
        # the browser profile dir), so tor never touches a global state dir.
        profile_dir = Path(
            getattr(config, "profile_dir", "/var/lib/parousia/browsers")
        )
        self._data_dir = profile_dir.parent / "tor"
        self._process: Optional[subprocess.Popen] = None

    @property
    def socks_url(self) -> str:
        """The transport URL chunk 1's selection routes through."""
        return f"socks5://127.0.0.1:{self._port}"

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def _port_accepting(self) -> bool:
        """True when the SOCKS port accepts a TCP connection (bootstrap)."""
        try:
            with socket.create_connection(("127.0.0.1", self._port), timeout=1.0):
                return True
        except OSError:
            return False

    def _ensure_data_dir(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise TorUnavailableError(
                f"cannot create Tor data directory {self._data_dir}: {exc}"
            ) from exc

    def ensure_running(self) -> str:
        """Lazily launch tor and return ``socks5://127.0.0.1:<port>``.

        Reuses the running process on subsequent calls. FAIL-LOUD on
        every failure path (see module docstring); never a direct-egress
        fallback.
        """
        # Reuse: alive process + accepting port → the running transport.
        if (
            self._process is not None
            and self._process.poll() is None
            and self._port_accepting()
        ):
            return self.socks_url

        # A dead/crashed process from an earlier run must not be reused.
        if self._process is not None and self._process.poll() is not None:
            self._process = None

        binary = shutil.which("tor")
        if binary is None:
            raise TorUnavailableError(
                "Tor binary not found; install `tor` to use proxy='tor' "
                "(Debian/Ubuntu: apt install tor)"
            )

        self._ensure_data_dir()
        try:
            self._process = subprocess.Popen(
                [
                    binary,
                    "--SOCKSPort",
                    str(self._port),
                    "--DataDirectory",
                    str(self._data_dir),
                    "--RunAsDaemon",
                    "0",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._process = None
            raise TorUnavailableError(f"failed to start tor: {exc}") from exc

        deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break  # process died → fall through to the loud failure
            if self._port_accepting():
                return self.socks_url
            time.sleep(BOOTSTRAP_POLL_SECONDS)

        exit_code = self._process.poll()
        self.stop()
        raise TorBootstrapError(
            f"Tor did not bootstrap a SOCKS listener on 127.0.0.1:{self._port} "
            f"within {BOOTSTRAP_TIMEOUT_SECONDS:g}s"
            + (f" (tor exited with code {exit_code})" if exit_code is not None else "")
        )

    def stop(self) -> None:
        """Terminate the managed tor subprocess (pool teardown path)."""
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except Exception:
            pass  # teardown is best-effort; the OS reaps orphans anyway