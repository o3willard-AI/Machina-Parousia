"""MP-11 (issue #13): the read side of the Parousia memory layer.

`MemoryQuery` — semantic recall of recorded facts, wrapping the mem0 SDK
(``Memory.search`` / ``Memory.get_all``) over the SAME lazy client the
``MemoryRecorder`` owns. It never builds a second embedder/Qdrant client:
the server constructs one ``MemoryRecorder`` and hands it to
``MemoryQuery``, so both sides share the lock-protected singleton built by
``MemoryRecorder._get_memory``.

Account scoping (the hard requirement): every read is scoped to
``user_id = user_id_prefix + agent_id`` — the AUTHENTICATED agent (the
MCP dispatch resolves the account; the tool schemas carry NO user_id/
agent_id argument). An agent reads its own facts and nothing else;
cross-agent access is refused by construction because there is no path
from tool input to another user's scope.

Fail soft: reads respect the recorder's circuit breaker (a backend known
down short-circuits to a clean error) and wrap Qdrant/mem0 failures in
``MemoryReadError`` — a clean, non-leaking error, never a 500 with a stack.
"""

import logging
import threading
from typing import Any, List, Optional

from parousia.memory.config import Mem0Config
from parousia.memory.recorder import MemoryRecorder

logger = logging.getLogger("parousia.memory")

# Read-side defaults (mem0 SDK defaults top_k=20; a presence-recall tool
# has no use for 20 hits — 5 focused facts is the useful page).
DEFAULT_TOP_K = 5
MAX_TOP_K = 50


class MemoryReadError(Exception):
    """A memory read failed (Qdrant/mem0 error) — surfaced as a clean
    tool error, never a 500 that leaks the stack."""


class MemoryUnavailableError(MemoryReadError):
    """The memory backend is known down (recorder circuit breaker open)."""


class MemoryQuery:
    """Read side: semantic search + profile over recorded facts.

    Shares the ``MemoryRecorder``'s lazy ``mem0.Memory`` singleton —
    constructing this class never spins up a second embedder or Qdrant
    client. (With no recorder, it lazily builds its own client from the
    same ``Mem0Config`` — the standalone/CLI path.)
    """

    def __init__(
        self,
        recorder: Optional[MemoryRecorder] = None,
        config: Optional[Mem0Config] = None,
    ):
        self._recorder = recorder
        self._config = config or getattr(recorder, "_config", None) or Mem0Config.from_file()
        self._memory = None
        self._memory_lock = threading.Lock()

    # ── user scope ──────────────────────────────────────────────

    def user_id_for(self, agent_id: str) -> str:
        """The mem0 user_id for an agent — `parousia-{agent_id}`.

        Derived ONLY from the authenticated agent_id handed to the tool
        dispatch (never from tool input). Same composition the recorder
        uses on the write side, so reads and writes share one namespace.
        """
        return f"{self._config.user_id_prefix}{agent_id}"

    # ── client ──────────────────────────────────────────────────

    def _get_memory(self):
        """The shared mem0.Memory client.

        With a recorder: its lock-protected lazy singleton (one client for
        the whole server — reads and writes). Without: mirrors the
        recorder's lazy init.
        """
        if self._recorder is not None:
            return self._recorder._get_memory()
        with self._memory_lock:
            if self._memory is not None:
                return self._memory
            from mem0 import Memory

            self._memory = Memory.from_config(self._config.to_mem0_dict())
            return self._memory

    def _check_breaker(self) -> None:
        """Fail soft when the backend is known down.

        Reuses the recorder's circuit-breaker state (the write side has
        already concluded the backend is failing); a read during the
        cooldown short-circuits to a clean error instead of hanging on a
        dead Qdrant for the TCP timeout.
        """
        if self._recorder is not None and self._recorder._is_breaker_open():
            raise MemoryUnavailableError(
                "memory backend temporarily unavailable (circuit breaker open)"
            )

    # ── reads ───────────────────────────────────────────────────

    def search(self, query: str, user_id: str, top_k: int = DEFAULT_TOP_K) -> List[dict]:
        """Semantic search of ONE user's facts → [{memory, score}, ...].

        mem0ai 2.2.1: ``Memory.search(query, filters={"user_id": ...},
        top_k=...)`` returns ``{"results": [{id, memory, score, ...}]}``.
        Mapped to lean ``{memory, score}`` dicts — the payload stays the
        fact text and relevance only (no ids/metadata).
        """
        query = (query or "").strip()
        if not query:
            return []
        top_k = max(1, min(int(top_k or DEFAULT_TOP_K), MAX_TOP_K))
        self._check_breaker()
        memory = self._get_memory()
        try:
            results = memory.search(
                query, filters={"user_id": user_id}, top_k=top_k
            )
        except MemoryReadError:
            raise
        except Exception as exc:
            raise MemoryReadError(f"memory search failed: {exc}") from exc
        return self._map_search(results)

    def profile(self, user_id: str) -> List[dict]:
        """All facts for ONE user → [{memory, created_at?}, ...].

        mem0ai 2.2.1: ``Memory.get_all(filters={"user_id": ...})``
        returns ``{"results": [{id, memory, created_at?, ...}]}``. Mapped
        to lean ``{memory}`` dicts (+ ``created_at`` when present) — the
        "what have I done" dump.
        """
        self._check_breaker()
        memory = self._get_memory()
        try:
            results = memory.get_all(filters={"user_id": user_id})
        except MemoryReadError:
            raise
        except Exception as exc:
            raise MemoryReadError(f"memory profile failed: {exc}") from exc
        return self._map_profile(results)

    # ── result mapping ──────────────────────────────────────────

    @staticmethod
    def _unwrap_results(results: Any) -> list:
        """mem0 2.2.1 returns {"results": [...]}; tolerate a bare list."""
        if isinstance(results, dict):
            return results.get("results") or []
        if isinstance(results, list):
            return results
        return []

    @classmethod
    def _map_search(cls, results: Any) -> List[dict]:
        out = []
        for r in cls._unwrap_results(results):
            if not isinstance(r, dict):
                continue
            memory = r.get("memory")
            if memory is None:
                continue
            score = r.get("score")
            out.append(
                {
                    "memory": str(memory),
                    **({"score": float(score)} if isinstance(score, (int, float)) else {}),
                }
            )
        return out

    @classmethod
    def _map_profile(cls, results: Any) -> List[dict]:
        out = []
        for r in cls._unwrap_results(results):
            if not isinstance(r, dict):
                continue
            memory = r.get("memory")
            if memory is None:
                continue
            entry = {"memory": str(memory)}
            created = r.get("created_at")
            if created:
                entry["created_at"] = str(created)
            out.append(entry)
        return out