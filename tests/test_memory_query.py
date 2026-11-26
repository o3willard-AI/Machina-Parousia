"""MP-11 (issue #13): memory read tools — mem0_search / mem0_profile.

Covers the [DONE] gate:
- search returns relevant facts for the RIGHT user (a seeded fact is
  retrieved by a related query);
- mem0_profile returns the full list;
- CROSS-AGENT ISOLATION — querying as agent A never returns agent B's
  facts: the mem0 user_id is derived from the AUTHENTICATED agent_id
  (never from tool input — the schemas carry no user_id/agent_id
  argument; a forged user_id in the arguments is ignored);
- empty-memory and read-error paths are clean (no unhandled exception);
- the read side SHARES the recorder's lazy mem0 singleton (no second
  embedder/Qdrant client) and respects the recorder's circuit breaker;
- MCP wiring: both tools are listed on the REAL server (25 tools) and
  dispatch through the real registered CallToolRequest handler — and a
  memory READ is never itself recorded.

mem0ai pinned in the venv: 2.2.1 — search/get_all take
``filters={"user_id": ...}`` and return ``{"results": [...]}`` (the
signatures are exercised against a fake with the same shape; no network
is dialed — Qdrant is not reachable from dev/CI).
"""

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

import parousia.memory.recorder as recorder_mod
from parousia.memory.config import Mem0Config
from parousia.memory.query import MemoryQuery
from parousia.memory.recorder import MemoryRecorder
from parousia.memory.tools import (
    ALL_MEMORY_SCHEMAS,
    DEFAULT_TOP_K,
    MAX_TOP_K,
    MemoryQueryHandlers,
    mem0_profile_schema,
    mem0_search_schema,
)


# ── Fake mem0.Memory (same call/return shape as mem0ai 2.2.1) ────


class FakeMemory:
    """In-memory stand-in for mem0.Memory: honors filters={"user_id"},
    scores by token overlap (so a related query retrieves the seed)."""

    instances = []

    def __init__(self, facts=None):
        self.facts = facts if facts is not None else {}
        # user_id -> [{id, memory, score?, created_at?}, ...]
        self.search_calls = []
        self.get_all_calls = []
        FakeMemory.instances.append(self)

    @classmethod
    def from_config(cls, config_dict):
        return cls()

    # -- mem0 2.2.1 signatures --
    def add(self, messages, *, user_id=None, agent_id=None, infer=True, **kw):
        fact = messages[0]["content"]
        self.facts.setdefault(user_id, []).append(
            {"id": f"f{len(self.facts[user_id])}", "memory": fact,
             "created_at": "2026-11-26T00:00:00Z"}
        )
        return {"results": [{"id": "f", "memory": fact}]}

    def search(self, query, *, filters=None, top_k=20, threshold=0.1, **kw):
        self.search_calls.append({"query": query, "filters": dict(filters or {}),
                                   "top_k": top_k})
        uid = (filters or {}).get("user_id")
        rows = self.facts.get(uid, [])
        qtokens = set(query.lower().split())
        scored = []
        for row in rows:
            ftokens = set(row["memory"].lower().replace(".", " .").split())
            overlap = len(qtokens & ftokens)
            scored.append((overlap, row))
        scored.sort(key=lambda p: -p[0])
        results = [
            {**row, "score": 0.9 - 0.01 * i}
            for i, (overlap, row) in enumerate(scored[:top_k])
            if overlap > 0
        ]
        return {"results": results}

    def get_all(self, *, filters=None, top_k=20, **kw):
        self.get_all_calls.append({"filters": dict(filters or {})})
        uid = (filters or {}).get("user_id")
        return {"results": list(self.facts.get(uid, []))}


@pytest.fixture
def fake_mem0(monkeypatch):
    """Patch mem0.Memory (the lazy import inside the recorder) BEFORE any
    client is built; returns the fake instance the server will get."""
    FakeMemory.instances = []
    fake = FakeMemory()
    monkeypatch.setattr("mem0.Memory", FakeMemory)
    return fake


def _seed(fake):
    """Two agents' worth of facts (isolation in both directions)."""
    fake.facts["parousia-agent-a"] = [
        {"id": "a1", "memory": 'Sent email to team@example.com: "rollout plan".',
         "created_at": "2026-11-25T10:00:00Z"},
        {"id": "a2", "memory": 'Scheduled "standup" for 2026-11-27T09:00:00 (high flexibility).',
         "created_at": "2026-11-25T11:00:00Z"},
    ]
    fake.facts["parousia-agent-b"] = [
        {"id": "b1", "memory": 'Sent email to ceo@corp.com: "acquisition offer".',
         "created_at": "2026-11-25T12:00:00Z"},
    ]


def _handlers(fake, monkeypatch):
    """Real MemoryQuery + handlers over a REAL recorder with the fake SDK.
    ``Memory.from_config`` (the lazy import inside the recorder) returns
    the seeded instance — no second client is ever built."""
    monkeypatch.setattr("mem0.Memory", FakeMemory)
    monkeypatch.setattr(
        FakeMemory, "from_config", classmethod(lambda cls, cfg: fake)
    )
    recorder = MemoryRecorder(config=Mem0Config())
    query = MemoryQuery(recorder)
    return MemoryQueryHandlers(query), query, recorder


def _dispatch(handlers, name, arguments, agent_id="agent-a"):
    return json.loads(asyncio.run(handlers.dispatch(name, arguments, agent_id)))


# ── Schemas ──────────────────────────────────────────────────────


class TestSchemas:
    def test_search_schema_shape(self):
        schema = mem0_search_schema()
        assert schema["name"] == "mem0_search"
        props = schema["inputSchema"]["properties"]
        assert props["query"]["type"] == "string"
        assert props["top_k"]["default"] == DEFAULT_TOP_K
        assert schema["inputSchema"]["required"] == ["query"]

    def test_schemas_have_no_user_id_argument(self):
        """ACCOUNT-SCOPED BY CONSTRUCTION: no user_id/agent_id input —
        the mem0 scope is derived from the authenticated account only."""
        for schema in (mem0_search_schema(), mem0_profile_schema()):
            props = schema["inputSchema"]["properties"]
            assert "user_id" not in props, schema["name"]
            assert "agent_id" not in props, schema["name"]

    def test_all_memory_schemas(self):
        assert [s["name"] for s in ALL_MEMORY_SCHEMAS] == ["mem0_search", "mem0_profile"]


# ── MemoryQuery (SDK wrapper) ────────────────────────────────────


class TestMemoryQuery:
    def test_search_retrieves_seeded_fact_by_related_query(self, fake_mem0, monkeypatch):
        _seed(fake_mem0)
        monkeypatch.setattr(
            FakeMemory, "from_config", classmethod(lambda cls, cfg: fake_mem0)
        )
        recorder = MemoryRecorder(config=Mem0Config())
        query = MemoryQuery(recorder)
        results = query.search("email team rollout", "parousia-agent-a", top_k=5)
        assert results, "related query retrieved nothing"
        assert any("team@example.com" in r["memory"] for r in results)
        for r in results:
            assert set(r) == {"memory", "score"}
            assert isinstance(r["score"], float)

    def test_profile_returns_full_list(self, fake_mem0, monkeypatch):
        _seed(fake_mem0)
        monkeypatch.setattr(
            FakeMemory, "from_config", classmethod(lambda cls, cfg: fake_mem0)
        )
        query = MemoryQuery(MemoryRecorder(config=Mem0Config()))
        facts = query.profile("parousia-agent-a")
        assert len(facts) == 2
        assert facts[0]["memory"].startswith("Sent email to team@example.com")
        assert "created_at" in facts[0]

    def test_user_id_composition(self):
        query = MemoryQuery(MemoryRecorder(config=Mem0Config()))
        assert query.user_id_for("agent-a") == "parousia-agent-a"

    def test_shares_recorder_lazy_singleton(self, fake_mem0):
        """One embedder/Qdrant client for the whole server: the query
        reuses the recorder's lock-protected lazy instance."""
        recorder = MemoryRecorder(config=Mem0Config())
        query = MemoryQuery(recorder)
        first = query._get_memory()
        again = query._get_memory()
        via_recorder = recorder._get_memory()
        assert first is again is via_recorder
        # And no SECOND client was built for the query (the last client
        # constructed is the shared one).
        assert FakeMemory.instances[-1] is first

    def test_respects_circuit_breaker(self, fake_mem0):
        recorder = MemoryRecorder(config=Mem0Config())
        recorder._consecutive_failures = 5
        recorder._breaker_open_until = time.monotonic() + 60
        query = MemoryQuery(recorder)
        with pytest.raises(Exception) as exc:
            query.search("anything", "parousia-agent-a")
        assert "circuit breaker" in str(exc.value)

    def test_read_error_is_clean(self, monkeypatch):
        class Boom(FakeMemory):
            def search(self, *a, **kw):
                raise RuntimeError("qdrant exploded")

        monkeypatch.setattr("mem0.Memory", Boom)
        monkeypatch.setattr(Boom, "from_config", classmethod(lambda cls, cfg: Boom()))
        query = MemoryQuery(MemoryRecorder(config=Mem0Config()))
        with pytest.raises(Exception) as exc:
            query.search("q", "parousia-agent-a")
        assert "memory search failed" in str(exc.value)
        assert "Traceback" not in str(exc.value)


# ── Handlers: account scoping + isolation ────────────────────────


class TestHandlers:
    def test_search_as_agent_a_returns_own_facts_only(self, monkeypatch):
        fake = FakeMemory()
        _seed(fake)
        handlers, _, _ = _handlers(fake, monkeypatch)
        result = _dispatch(handlers, "mem0_search", {"query": "email team rollout"})
        assert result["count"] == len(result["results"]) >= 1
        memories = " | ".join(r["memory"] for r in result["results"])
        assert "team@example.com" in memories
        # Agent B's facts are NOT here (isolation).
        assert "ceo@corp.com" not in memories

    def test_profile_full_list_and_isolation(self, monkeypatch):
        fake = FakeMemory()
        _seed(fake)
        handlers, _, _ = _handlers(fake, monkeypatch)
        result = _dispatch(handlers, "mem0_profile", {})
        assert result["count"] == 2
        assert all("ceo@corp.com" not in f["memory"] for f in result["facts"])

    def test_user_id_derived_from_authenticated_agent_not_input(self, monkeypatch):
        """The hard requirement: a forged user_id in the TOOL ARGUMENTS
        must be ignored — the scope comes only from the authenticated
        agent_id handed to dispatch."""
        fake = FakeMemory()
        _seed(fake)
        handlers, _, _ = _handlers(fake, monkeypatch)
        result = _dispatch(
            handlers, "mem0_search",
            {"query": "acquisition offer", "user_id": "parousia-agent-b",
             "agent_id": "parousia-agent-b"},
            agent_id="agent-a",
        )
        # Scoped to agent-a: the b-only fact is invisible despite the forgery.
        assert all("acquisition" not in r["memory"] for r in result["results"])
        assert fake.search_calls[-1]["filters"]["user_id"] == "parousia-agent-a"

    def test_reverse_isolation_agent_b_never_sees_a(self, monkeypatch):
        fake = FakeMemory()
        _seed(fake)
        handlers, _, _ = _handlers(fake, monkeypatch)
        result = _dispatch(handlers, "mem0_profile", {}, agent_id="agent-b")
        assert result["count"] == 1
        assert "ceo@corp.com" in result["facts"][0]["memory"]
        assert all("team@example.com" not in f["memory"] for f in result["facts"])

    def test_empty_memory_is_clean(self, monkeypatch):
        fake = FakeMemory()
        handlers, _, _ = _handlers(fake, monkeypatch)
        s = _dispatch(handlers, "mem0_search", {"query": "anything"})
        assert s == {"results": [], "count": 0}
        p = _dispatch(handlers, "mem0_profile", {})
        assert p == {"facts": [], "count": 0}

    def test_empty_query_is_a_clean_error(self, monkeypatch):
        handlers, _, _ = _handlers(FakeMemory(), monkeypatch)
        assert _dispatch(handlers, "mem0_search", {"query": "   "}) == {
            "error": "query is required"
        }

    def test_backend_error_is_clean_never_500(self, monkeypatch):
        class Boom(FakeMemory):
            def get_all(self, *a, **kw):
                raise RuntimeError("qdrant exploded")

        import mem0

        old = mem0.Memory
        mem0.Memory = Boom
        try:
            handlers, _, _ = _handlers(Boom(), monkeypatch)
            result = _dispatch(handlers, "mem0_profile", {})
            assert "error" in result
            assert "qdrant exploded" in result["error"]
            assert "Traceback" not in result["error"]
        finally:
            mem0.Memory = old

    def test_breaker_open_is_a_clean_error(self, monkeypatch):
        handlers, _, recorder = _handlers(FakeMemory(), monkeypatch)
        recorder._consecutive_failures = 5
        recorder._breaker_open_until = time.monotonic() + 60
        result = _dispatch(handlers, "mem0_search", {"query": "x"})
        assert "error" in result
        assert "circuit breaker" in result["error"]
        p = _dispatch(handlers, "mem0_profile", {})
        assert "circuit breaker" in p["error"]

    def test_top_k_clamped(self, monkeypatch):
        fake = FakeMemory()
        _seed(fake)
        handlers, _, _ = _handlers(fake, monkeypatch)
        _dispatch(handlers, "mem0_search", {"query": "email", "top_k": 999})
        assert fake.search_calls[-1]["top_k"] == MAX_TOP_K
        _dispatch(handlers, "mem0_search", {"query": "email", "top_k": 0})
        assert fake.search_calls[-1]["top_k"] == DEFAULT_TOP_K

    def test_unknown_memory_tool(self, monkeypatch):
        handlers, _, _ = _handlers(FakeMemory(), monkeypatch)
        result = _dispatch(handlers, "mem0_remember", {})
        assert result == {"error": "Unknown memory tool: mem0_remember"}


# ── MCP wiring: real server, real dispatch ───────────────────────


class TestMcpServerWiring:
    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        """The test_mcp_memory_integration pattern: real _build_server with
        heavy infra faked; drive the REAL registered handlers."""
        import mcp.types as types
        import parousia.guard.mcp_server as mcp_mod
        from parousia.config import ParousiaConfig
        from parousia.inbox.inbox_store import InboxStore
        from parousia.spatial.tools import SpatialToolHandlers

        config = ParousiaConfig(domain="agents.test.com")
        monkeypatch.setattr(mcp_mod, "load_config", lambda: config)

        import fakeredis

        monkeypatch.setattr(
            "parousia.guard.mcp_server.redis_lib.Redis", lambda **kw: fakeredis.FakeRedis()
        )

        def _fake_temporal_db():
            from parousia.temporal.db import TemporalDB

            db = TemporalDB(db_path=":memory:")
            db.connect()
            db.create_tables()
            return db

        monkeypatch.setattr(mcp_mod, "TemporalDB", lambda *a, **kw: _fake_temporal_db())
        inbox = InboxStore(str(tmp_path / "inbox.db"))
        monkeypatch.setattr(
            "parousia.inbox.inbox_store.InboxStore", lambda *a, **kw: inbox
        )

        # Recorder: capture record calls (and prove reads are NOT recorded).
        calls = []

        async def _noop_async(*a, **kw):
            return None

        def _record(self, tool, args, result, agent_id, sponsor=""):
            calls.append((tool, args, result, agent_id))

        from parousia.memory.recorder import MemoryRecorder

        monkeypatch.setattr(MemoryRecorder, "record_tool_call", _record)
        monkeypatch.setattr(mcp_mod, "_handle_send_email", _noop_async)

        async def _fake_spatial(self, name, arguments, agent_id):
            return json.dumps({"url": arguments.get("url", ""), "loaded": True})

        monkeypatch.setattr(SpatialToolHandlers, "dispatch", _fake_spatial)

        # mem0 SDK → FakeMemory, with from_config returning the SEEDED
        # instance (the lazy import inside MemoryRecorder picks this up).
        fake = FakeMemory()
        _seed(fake)
        # The stdio sentinel agent (no auth in this fixture) — dispatch
        # derives user_id "parousia-default" from it.
        fake.facts["parousia-default"] = list(fake.facts["parousia-agent-a"])
        monkeypatch.setattr("mem0.Memory", FakeMemory)
        monkeypatch.setattr(
            FakeMemory, "from_config", classmethod(lambda cls, cfg: fake)
        )

        server, _account_store = mcp_mod._build_server()

        async def dispatch(name, arguments):
            handler = server.request_handlers[types.CallToolRequest]
            res = await handler(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(name=name, arguments=arguments)
                )
            )
            # The lowlevel server wraps returns: ServerResult →
            # CallToolResult(content=[TextContent, ...]) — unwrap to the
            # plain content list (the shape handle_call_tool returns).
            if hasattr(res, "root"):
                return res.root.content
            return res

        async def list_tools():
            handler = server.request_handlers[types.ListToolsRequest]
            result = await handler(
                types.ListToolsRequest(params=types.PaginatedRequestParams())
            )
            return [t.name for t in result.root.tools]

        return dispatch, list_tools, calls, fake

    @pytest.mark.asyncio
    async def test_both_tools_listed_count_25(self, wired):
        _, list_tools, _, _ = wired
        names = await list_tools()
        assert "mem0_search" in names and "mem0_profile" in names
        assert len(names) == 25, names  # 23 + 2 memory read tools
        assert len(set(names)) == len(names)  # no duplicates

    @pytest.mark.asyncio
    async def test_real_dispatch_search_and_profile(self, wired):
        dispatch, _, calls, fake = wired
        result = json.loads((await dispatch("mem0_search", {"query": "email team rollout"}))[0].text)
        assert result["count"] >= 1
        assert any("team@example.com" in r["memory"] for r in result["results"])
        # Scoped to the stdio sentinel agent (no auth in this fixture).
        assert fake.search_calls[-1]["filters"]["user_id"] == "parousia-default"
        # Empty query → clean error through the real dispatch.
        err = json.loads((await dispatch("mem0_search", {"query": ""}))[0].text)
        assert "error" in err

    @pytest.mark.asyncio
    async def test_memory_reads_are_never_recorded(self, wired):
        dispatch, _, calls, _ = wired
        await dispatch("mem0_search", {"query": "email"})
        await dispatch("mem0_profile", {})
        # A write-side tool IS recorded (the contrast case)...
        await dispatch("browse_to", {"url": "https://example.com"})
        recorded = [c[0] for c in calls]
        assert "mem0_search" not in recorded
        assert "mem0_profile" not in recorded
        assert "browse_to" in recorded

    @pytest.mark.asyncio
    async def test_unknown_tool_still_errors(self, wired):
        dispatch, _, _, _ = wired
        result = json.loads((await dispatch("mem0_remember", {}))[0].text)
        assert "Unknown tool" in result["error"]