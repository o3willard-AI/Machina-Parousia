"""MP-13: v0.5.0 code fixes — install breaker + feature bugs.

Covers the auditor fixes against main `fc94337`:
- Fix 2: bare `serve` starts REST ONLY (no public MCP SSE by default);
  `--mcp-sse`/`--all` start SSE; `--mcp-host` defaults to loopback
  127.0.0.1 (operator opts into 0.0.0.0 explicitly).
- Fix 4: `setup --dkim --rotate` exists — wired to the DKIM helper's
  existing `rotate` parameter.
- Fix 5: `interact_schema` declares `value` and `key` (handler reads
  them for select/press) so strict schema validation stops stripping
  them; select/press dispatch works through a schema-shaped filter.
- Fix 6: `send_email` enforces the authenticated ACCOUNT's
  `rate_limit_per_hour` (fail-closed; default 20) —
  `config.rate_limits.per_agent_per_hour` is the fallback only when no
  account row exists.
Fixes 1 and 3 are covered by the clean-venv install gate (bs4 declared)
and tests/test_cli.py (rest_host/rest_port gone from ServerConfig).
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from parousia.auth.accounts import Account
from parousia.config import ParousiaConfig
from parousia.guard.mcp_server import _handle_send_email
from parousia.guard.rate_limiter import RateLimiter
from parousia.spatial.tools import SpatialToolHandlers, interact_schema


# ── Fix 2: serve defaults (loopback, opt-in SSE) ──────────────────


class TestServeDefaults:
    @pytest.fixture
    def recorded(self, monkeypatch):
        rec = {"uvicorn": [], "sse": []}

        def fake_uvicorn_run(*args, **kwargs):
            rec["uvicorn"].append(kwargs)

        async def fake_sse(host="0.0.0.0", port=8081):
            rec["sse"].append({"host": host, "port": port})

        # serve imports both lazily inside the function body:
        # `import uvicorn` + `uvicorn.run(...)`, and
        # `from parousia.guard.mcp_server import run_mcp_server_sse`.
        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
        monkeypatch.setattr(
            "parousia.guard.mcp_server.run_mcp_server_sse", fake_sse
        )
        return rec

    def _serve(self, *args):
        from parousia.cli.main import serve

        return CliRunner().invoke(serve, list(args))

    def test_mcp_host_flag_defaults_to_loopback(self):
        """Gate: --mcp-host default is 127.0.0.1 (not 0.0.0.0)."""
        from parousia.cli.main import serve

        opt = next(p for p in serve.params if p.name == "mcp_host")
        assert opt.default == "127.0.0.1"

    def test_bare_serve_starts_rest_only(self, recorded):
        """THE FIX: no flags → REST only. MCP SSE is NOT started (the
        public 0.0.0.0:8081 endpoint must never bind by default)."""
        result = self._serve()
        assert result.exit_code == 0, result.output
        assert len(recorded["uvicorn"]) == 1
        assert recorded["uvicorn"][0]["host"] == "127.0.0.1"
        assert recorded["uvicorn"][0]["port"] == 8080
        assert recorded["sse"] == [], "bare serve must NOT start MCP SSE"

    def test_mcp_sse_flag_starts_sse_loopback(self, recorded):
        result = self._serve("--mcp-sse")
        assert result.exit_code == 0, result.output
        assert recorded["sse"] == [{"host": "127.0.0.1", "port": 8081}]
        assert recorded["uvicorn"] == []

    def test_all_starts_rest_and_sse(self, recorded):
        result = self._serve("--all")
        assert result.exit_code == 0, result.output
        # REST runs in a daemon thread; give it a moment to record.
        for _ in range(50):
            if recorded["uvicorn"]:
                break
            time.sleep(0.02)
        assert len(recorded["uvicorn"]) == 1
        assert recorded["uvicorn"][0]["host"] == "127.0.0.1"
        assert recorded["sse"] == [{"host": "127.0.0.1", "port": 8081}]

    def test_explicit_public_host_passes_through(self, recorded):
        """Operator opt-in: --mcp-host 0.0.0.0 is honored (and still
        requires the explicit --mcp-sse/--all to bind at all)."""
        result = self._serve("--mcp-sse", "--mcp-host", "0.0.0.0")
        assert result.exit_code == 0, result.output
        assert recorded["sse"] == [{"host": "0.0.0.0", "port": 8081}]


# ── Fix 4: setup --dkim --rotate ───────────────────────────────────


class TestSetupRotate:
    def test_rotate_in_setup_help(self):
        """Gate: `setup --help` advertises --rotate."""
        from parousia.cli.main import setup

        result = CliRunner().invoke(setup, ["--help"])
        assert result.exit_code == 0
        assert "--rotate" in result.output

    def test_rotate_wired_to_helper(self, monkeypatch):
        """`setup --dkim --rotate` reaches the helper's rotate param."""
        from parousia.cli import main as cli_main

        calls = {}

        def fake_generate(domain, key_dir, selector, force=False, rotate=False):
            calls["rotate"] = rotate
            return "PUBLIC PEM"

        monkeypatch.setattr(
            "parousia.cli.dkim.generate_dkim_keys", fake_generate
        )
        monkeypatch.setattr(
            "parousia.cli.dns_records.format_dns_records",
            lambda *a, **kw: "records",
        )

        result = CliRunner().invoke(cli_main.setup, ["--dkim", "--rotate"])
        assert result.exit_code == 0, result.output
        assert calls["rotate"] is True

    def test_no_rotate_defaults_false(self, monkeypatch):
        from parousia.cli import main as cli_main

        calls = {}

        def fake_generate(domain, key_dir, selector, force=False, rotate=False):
            calls["rotate"] = rotate
            return "PUBLIC PEM"

        monkeypatch.setattr(
            "parousia.cli.dkim.generate_dkim_keys", fake_generate
        )
        monkeypatch.setattr(
            "parousia.cli.dns_records.format_dns_records",
            lambda *a, **kw: "records",
        )
        result = CliRunner().invoke(cli_main.setup, ["--dkim"])
        assert result.exit_code == 0, result.output
        assert calls["rotate"] is False


# ── Fix 5: interact schema declares value/key ─────────────────────


class TestInteractSchemaValueKey:
    def test_schema_declares_value_and_key(self):
        schema = interact_schema()
        props = schema["inputSchema"]["properties"]
        assert "value" in props, "select's value was stripped by strict clients"
        assert "key" in props, "press's key was stripped by strict clients"
        assert props["value"]["type"] == "string"
        assert props["key"]["type"] == "string"
        # Optional in the schema — required-ness is per-action (handler).
        assert "value" not in schema["inputSchema"].get("required", [])
        assert "key" not in schema["inputSchema"].get("required", [])

    def _handlers(self):
        page = MagicMock()
        for act in (
            "click", "type", "scroll_into_view", "select_option",
            "check", "uncheck", "hover", "press",
        ):
            setattr(page, act, AsyncMock())
        browser = MagicMock()
        browser.page = page
        pool = MagicMock()
        pool.get_browser = AsyncMock(return_value=browser)
        pool.proxy_for = MagicMock(return_value="")
        return SpatialToolHandlers(ParousiaConfig(), pool, MagicMock()), page

    def test_select_survives_schema_validation(self):
        """A STRICT client validates input against the schema and strips
        unknown properties — with the fix, `value` survives that filter
        and the dispatch reaches page.select_option with it."""
        handlers, page = self._handlers()
        schema = interact_schema()
        raw = {"id": "el", "action": "select", "value": "opt-2"}
        allowed = set(schema["inputSchema"]["properties"])
        strict = {k: v for k, v in raw.items() if k in allowed}
        assert strict == raw, "schema must not strip select's value"
        result = json.loads(
            asyncio.run(handlers.dispatch("interact", strict, "agent-x"))
        )
        assert result["success"] is True
        page.select_option.assert_awaited_with("#el", "opt-2")

    def test_press_survives_schema_validation(self):
        handlers, page = self._handlers()
        schema = interact_schema()
        raw = {"id": "el", "action": "press", "key": "Enter"}
        allowed = set(schema["inputSchema"]["properties"])
        strict = {k: v for k, v in raw.items() if k in allowed}
        assert strict == raw, "schema must not strip press's key"
        result = json.loads(
            asyncio.run(handlers.dispatch("interact", strict, "agent-x"))
        )
        assert result["success"] is True
        page.press.assert_awaited_with("#el", "Enter")


# ── Fix 6: per-account rate limit is authoritative ────────────────


class TestPerAccountRateLimit:
    def _account(self, limit):
        return Account(
            account_id="agent-x", api_key_hash="h", tier="free",
            status="active", rate_limit_per_hour=limit,
        )

    def _run(self, account, account_store, limit_config=100):
        import fakeredis

        redis = fakeredis.FakeRedis()
        limiter = RateLimiter(
            redis, per_agent_per_hour=limit_config, domain_per_day=500
        )
        config = ParousiaConfig()
        calls = []

        def fake_smtp(**kw):
            calls.append(kw)
            return "msg-id"

        import parousia.guard.mcp_server as m

        old = m._smtp_send
        m._smtp_send = fake_smtp
        try:
            results = [
                json.loads(
                    asyncio.run(
                        _handle_send_email(
                            {"to": "x@y.com", "subject": "s", "body": "b"},
                            config, limiter, redis,
                            account=account, account_store=account_store,
                        )
                    )[0].text
                )
                for _ in range(2)
            ]
        finally:
            m._smtp_send = old
        return results

    def test_account_limit_1_rejects_second_email(self, tmp_path):
        """THE FIX: an account with rate_limit_per_hour=1 rejects the 2nd
        email within the hour — even though the config fallback is 100."""
        acct = self._account(1)
        store = MagicMock()
        store.get_account.return_value = acct
        results = self._run(acct, store)
        first, second = results
        assert first["sent"] is True
        assert first["rate_limit_remaining"] == 0  # 1/hour spent
        assert second["sent"] is False
        assert second["error"] == "rate_limit_exceeded"

    def test_default_account_limit_is_20(self):
        """Gate: GET /account and the enforced limit agree — the account
        row default is 20 (fail-closed), not the config's 100."""
        acct = Account(account_id="agent-x", api_key_hash="h")
        assert acct.rate_limit_per_hour == 20

    def test_no_account_row_falls_back_to_config(self):
        """No account row (stdio 'default' sentinel, no auth) → the config
        fallback (per_agent_per_hour) governs — a limit-1 CONFIG still
        rejects the 2nd email."""
        store = MagicMock()
        store.get_account.return_value = None
        results = self._run(None, store, limit_config=1)
        first, second = results
        assert first["sent"] is True
        assert second["sent"] is False
        assert second["error"] == "rate_limit_exceeded"

    def test_ratemeter_check_per_agent_limit_override(self):
        import fakeredis

        limiter = RateLimiter(
            fakeredis.FakeRedis(), per_agent_per_hour=100, domain_per_day=500
        )
        # Override wins over the constructed default…
        allowed, remaining, _ = limiter.check("a", per_agent_limit=1)
        assert allowed is True and remaining == 0
        allowed, _, _ = limiter.check("a", per_agent_limit=1)
        assert allowed is False
        # …and without an override the constructed default applies.
        allowed, remaining, _ = limiter.check("b")
        assert allowed is True and remaining == 99